"""
aws-lambda-whisper-adaptor
Deepgram and OpenAI compatible speech-to-text on AWS Lambda using faster-whisper.
https://github.com/gabrielkoo/aws-lambda-whisper-adaptor
"""
import json
import base64
import email
import os
import shutil
import logging
import uuid
import boto3
from faster_whisper import WhisperModel

logger = logging.getLogger()
logger.setLevel(logging.INFO)

API_SECRET = os.environ.get('API_SECRET', '')
HF_MODEL_REPO = os.environ.get('HF_MODEL_REPO', 'openai/whisper-large-v3-turbo')
MODEL_SLUG = HF_MODEL_REPO.replace('/', '--')
EFS_MODELS_ROOT = '/mnt/whisper-models'
EFS_MODEL_DIR = f'{EFS_MODELS_ROOT}/{MODEL_SLUG}'
S3_BUCKET = os.environ.get('MODEL_S3_BUCKET', '')
S3_PREFIX = f'models/{MODEL_SLUG}/'
MODEL_MARKER = f'{EFS_MODELS_ROOT}/.ready-{MODEL_SLUG}'
WHISPER_LANGUAGE = os.environ.get('WHISPER_LANGUAGE') or None  # None = auto-detect
WHISPER_COMPUTE_TYPE = os.environ.get('WHISPER_COMPUTE_TYPE', 'int8')


def is_model_valid(model_dir):
    for f in ['model.bin', 'vocabulary.json', 'tokenizer.json', 'config.json']:
        if not os.path.exists(os.path.join(model_dir, f)):
            return False
    try:
        with open(os.path.join(model_dir, 'config.json')) as f:
            cfg = json.load(f)
        if cfg.get('num_mel_bins', 128) < 128:
            return False
    except Exception:
        return False
    return True


def bootstrap_model():
    if os.path.exists(MODEL_MARKER) and not is_model_valid(EFS_MODEL_DIR):
        logger.warning("Model on EFS is incomplete, re-bootstrapping...")
        os.unlink(MODEL_MARKER)
    if os.path.exists(MODEL_MARKER):
        logger.info("Model on EFS, loading...")
        return WhisperModel(EFS_MODEL_DIR, device='cpu', compute_type=WHISPER_COMPUTE_TYPE)

    logger.info(f"Bootstrapping {HF_MODEL_REPO} from S3 to EFS...")
    os.makedirs(EFS_MODEL_DIR, exist_ok=True)
    s3 = boto3.client('s3')
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX):
        for obj in page.get('Contents', []):
            key = obj['Key']
            rel = key[len(S3_PREFIX):]
            if not rel:
                continue
            local_path = os.path.join(EFS_MODEL_DIR, rel)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            logger.info(f"S3 → EFS: {rel}")
            s3.download_file(S3_BUCKET, key, local_path)

    open(MODEL_MARKER, 'w').close()
    logger.info("Bootstrap complete.")
    return WhisperModel(EFS_MODEL_DIR, device='cpu', compute_type=WHISPER_COMPUTE_TYPE)


MODEL = bootstrap_model()
logger.info("Model ready.")


def parse_multipart(body_bytes, content_type_header):
    """Extract audio bytes and optional fields from multipart/form-data."""
    raw = f'Content-Type: {content_type_header}\r\n\r\n'.encode() + body_bytes
    msg = email.message_from_bytes(raw)
    audio_bytes, audio_content_type, language = None, None, None
    for part in msg.walk():
        cd = part.get('Content-Disposition', '')
        if 'name="file"' in cd:
            audio_bytes = part.get_payload(decode=True)
            audio_content_type = part.get_content_type()
        elif 'name="language"' in cd:
            language = part.get_payload(decode=False).strip()
    return audio_bytes, audio_content_type, language


def transcribe(audio_bytes, content_type, language=None):
    ext = content_type.split('/')[-1].split(';')[0].strip() or 'ogg'
    tmp_path = f"/tmp/{uuid.uuid4()}.{ext}"
    try:
        with open(tmp_path, 'wb') as f:
            f.write(audio_bytes)
        lang = language or WHISPER_LANGUAGE
        segments, info = MODEL.transcribe(tmp_path, beam_size=5, language=lang)
        transcript = ' '.join(seg.text.strip() for seg in segments)
        return transcript, info.duration
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def handle_list_models():
    """GET /v1/models — list all ready models on EFS."""
    models = []
    if os.path.isdir(EFS_MODELS_ROOT):
        for entry in os.scandir(EFS_MODELS_ROOT):
            if not entry.is_dir():
                continue
            slug = entry.name
            marker = os.path.join(EFS_MODELS_ROOT, f'.ready-{slug}')
            if not os.path.exists(marker):
                continue
            # reverse slug back to HF repo id (e.g. openai--whisper-large-v3-turbo → openai/whisper-large-v3-turbo)
            model_id = slug.replace('--', '/', 1)
            models.append({
                'id': model_id,
                'object': 'model',
                'created': int(entry.stat().st_mtime),
                'owned_by': model_id.split('/')[0],
            })
    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({'object': 'list', 'data': models}),
    }


def handle_delete_model(model_id):
    """DELETE /v1/models/{owner}/{model} — remove a model from EFS."""
    slug = model_id.replace('/', '--')
    model_dir = os.path.join(EFS_MODELS_ROOT, slug)
    marker = os.path.join(EFS_MODELS_ROOT, f'.ready-{slug}')

    if not os.path.isdir(model_dir):
        return {'statusCode': 404, 'body': json.dumps({'error': f'Model not found: {model_id}'})}

    if os.path.abspath(model_dir) == os.path.abspath(EFS_MODEL_DIR):
        return {'statusCode': 409, 'body': json.dumps({'error': 'Cannot delete the currently loaded model'})}

    try:
        shutil.rmtree(model_dir)
        if os.path.exists(marker):
            os.unlink(marker)
        logger.info(f"Deleted model from EFS: {model_id}")
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'id': model_id, 'object': 'model', 'deleted': True}),
        }
    except Exception as e:
        logger.error(f"Failed to delete {model_id}: {e}")
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}


def handler(event, context):
    if API_SECRET:
        headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
        if headers.get('authorization', '') != f'Token {API_SECRET}':
            return {'statusCode': 401, 'body': json.dumps({'error': 'Unauthorized'})}

    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    method = (event.get('requestContext') or {}).get('http', {}).get('method', 'POST').upper()
    content_type_header = headers.get('content-type', 'audio/ogg')
    content_type = content_type_header.split(';')[0].strip()
    path = event.get('rawPath', '/v1/listen')
    query = event.get('queryStringParameters') or {}

    # Model management endpoints
    if path == '/v1/models' and method == 'GET':
        return handle_list_models()

    if path.startswith('/v1/models/') and method == 'DELETE':
        model_id = path[len('/v1/models/'):]
        return handle_delete_model(model_id)

    body = event.get('body') or ''
    audio_bytes = base64.b64decode(body) if event.get('isBase64Encoded') else (
        body.encode() if isinstance(body, str) else body
    )

    language = query.get('language')  # query param for both endpoints

    if path == '/v1/audio/transcriptions' or 'multipart/form-data' in content_type:
        audio_bytes, content_type, form_language = parse_multipart(audio_bytes, content_type_header)
        if not audio_bytes:
            return {'statusCode': 400, 'body': json.dumps({'error': 'No audio file in request'})}
        content_type = content_type or 'audio/mpeg'
        language = language or form_language  # form field overrides query param

    logger.info(f"{path} | {content_type} | {len(audio_bytes)} bytes | lang={language or WHISPER_LANGUAGE or 'auto'}")

    try:
        transcript, duration = transcribe(audio_bytes, content_type, language)
        logger.info(f"Transcript: {transcript[:80]}")

        if path == '/v1/audio/transcriptions':
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'text': transcript}),
            }

        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'metadata': {
                    'request_id': context.aws_request_id,
                    'channels': 1, 'duration': duration,
                    'model': 'whisper-large-v3-turbo',
                },
                'results': {'channels': [{'alternatives': [{
                    'transcript': transcript, 'confidence': 0.99, 'words': [],
                }]}]},
            }),
        }
    except Exception as e:
        logger.error(f"Error: {e}")
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}
