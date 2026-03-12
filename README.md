# aws-lambda-whisper-adaptor

Self-hosted speech-to-text on AWS Lambda using [faster-whisper](https://github.com/SYSTRAN/faster-whisper), with **Deepgram** and **OpenAI** compatible APIs.

## Features

- 🎙️ **Deepgram-compatible** `/v1/listen` endpoint
- 🤖 **OpenAI-compatible** `/v1/audio/transcriptions` endpoint
- 📋 **Model management** — list and delete EFS models via API
- 📦 **EFS-backed model** — fast cold starts (~29s with int8)
- 💰 **Pay-per-use** — scales to zero, no idle costs
- 🔄 **Self-bootstrapping** — Lambda downloads model from S3 to EFS on first cold start
- 🔧 **Any HuggingFace model** — configure via `HF_MODEL_REPO` env var

## Architecture

```
Request → Lambda Function URL
               ↓
          Lambda (VPC)
               ↓ cold start: S3 → EFS (once per model)
              EFS ←── S3 ←── HuggingFace
                     (sync-model workflow, run once per model)
```

No NAT Gateway needed — uses a free S3 VPC Gateway Endpoint.

## Prerequisites

- AWS account with permissions to create Lambda, EFS, S3, IAM, ECR, and VPC resources
- VPC with at least one private subnet (Lambda runs in VPC for EFS access)
- AWS CLI v2 configured
- Docker (for building custom images)
- GitHub account (for CI/CD via Actions)

## Cost (us-west-2, approximate)

| Resource | Cost |
|----------|------|
| EFS storage | ~$0.30/GB-month (~$0.23/month for 780MB int8 model) |
| S3 storage | ~$0.02/month |
| Lambda | Pay-per-use (~$0.000167/s at 10GB) |
| S3 VPC Gateway Endpoint | **Free** |
| NAT Gateway | **Not needed** |

## Quick Start

### Option 1: Use pre-built image

```bash
docker pull ghcr.io/gabrielkoo/aws-lambda-whisper-adaptor:latest
```

Use this image URI when creating your Lambda function.

### Option 2: Build your own

Fork this repo and set up GitHub Actions variables (see below).

## Setup

### 1. IAM Roles

#### Lambda Execution Role

```bash
# Create role
aws iam create-role \
  --role-name whisper-adaptor-lambda \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

# VPC access + CloudWatch Logs
aws iam attach-role-policy \
  --role-name whisper-adaptor-lambda \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole

# S3 read + EFS mount permissions
aws iam put-role-policy \
  --role-name whisper-adaptor-lambda \
  --policy-name whisper-adaptor-policy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:ListBucket"],
        "Resource": [
          "arn:aws:s3:::your-whisper-models",
          "arn:aws:s3:::your-whisper-models/*"
        ]
      },
      {
        "Effect": "Allow",
        "Action": [
          "elasticfilesystem:ClientMount",
          "elasticfilesystem:ClientWrite",
          "elasticfilesystem:ClientRootAccess"
        ],
        "Resource": "arn:aws:elasticfilesystem:REGION:ACCOUNT_ID:file-system/fs-xxxxxxxx"
      }
    ]
  }'
```

#### Deploy Role (GitHub Actions OIDC)

```bash
# One-time per AWS account: create OIDC provider for GitHub Actions
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# Create deploy role (replace YOUR_GITHUB_USERNAME and ACCOUNT_ID)
aws iam create-role \
  --role-name whisper-adaptor-deploy \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:YOUR_GITHUB_USERNAME/aws-lambda-whisper-adaptor:*"
        },
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        }
      }
    }]
  }'

# Attach ECR push + Lambda update permissions
aws iam put-role-policy \
  --role-name whisper-adaptor-deploy \
  --policy-name deploy-policy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:CreateRepository"
        ],
        "Resource": "*"
      },
      {
        "Effect": "Allow",
        "Action": [
          "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration",
          "lambda:GetFunction",
          "lambda:GetFunctionConfiguration"
        ],
        "Resource": "arn:aws:lambda:REGION:ACCOUNT_ID:function:*whisper*"
      }
    ]
  }'
```

### 2. Infrastructure

```bash
# S3 bucket for model storage
aws s3 mb s3://your-whisper-models --region us-west-2

# EFS filesystem (note the FileSystemId)
aws efs create-file-system --region us-west-2

# EFS mount targets (one per subnet in your VPC)
aws efs create-mount-target \
  --file-system-id fs-xxxxxxxx \
  --subnet-id subnet-xxxxxxxx \
  --security-groups sg-xxxxxxxx

# EFS access point
aws efs create-access-point \
  --file-system-id fs-xxxxxxxx \
  --posix-user "Uid=0,Gid=0" \
  --root-directory "Path=/whisper-models,CreationInfo={OwnerUid=0,OwnerGid=0,Permissions=755}"

# S3 VPC Gateway Endpoint (free — allows Lambda to reach S3 without NAT)
aws ec2 create-vpc-endpoint \
  --vpc-id vpc-xxxxxxxx \
  --service-name com.amazonaws.us-west-2.s3 \
  --route-table-ids rtb-xxxxxxxx
```

### 3. Lambda Function

```bash
aws lambda create-function \
  --function-name whisper-adaptor \
  --package-type Image \
  --code ImageUri=ghcr.io/gabrielkoo/aws-lambda-whisper-adaptor:latest \
  --role arn:aws:iam::YOUR_ACCOUNT:role/whisper-adaptor-lambda \
  --memory-size 10240 \
  --timeout 900 \
  --vpc-config SubnetIds=subnet-xxx,SecurityGroupIds=sg-xxx \
  --file-system-configs Arn=arn:aws:elasticfilesystem:REGION:ACCOUNT_ID:access-point/fsap-xxx,LocalMountPath=/mnt/whisper-models \
  --environment "Variables={HF_MODEL_REPO=openai/whisper-large-v3-turbo,MODEL_S3_BUCKET=your-whisper-models,API_SECRET=your-secret}"
```

> **Memory note:** 10GB is required — `whisper-large-v3-turbo` (int8) needs ~2.8GB for the model alone, plus headroom for audio processing.

### 4. Function URL

```bash
aws lambda create-function-url-config \
  --function-name whisper-adaptor \
  --auth-type NONE

# Allow public invocation
aws lambda add-permission \
  --function-name whisper-adaptor \
  --statement-id FunctionURLAllowPublicAccess \
  --action lambda:InvokeFunctionUrl \
  --principal "*" \
  --function-url-auth-type NONE
```

> **Auth note:** `auth-type NONE` means AWS IAM is not used for auth. Instead, the handler validates `Authorization: Token <secret>` against the `API_SECRET` env var. Set a strong random value.

### 5. Sync Model to S3

Run the `sync-model` GitHub Actions workflow, or manually:

```bash
pip install huggingface_hub ctranslate2 transformers torch

# HuggingFace-format models (e.g. openai/*) — convert to CTranslate2 int8
ct2-transformers-converter \
  --model openai/whisper-large-v3-turbo \
  --output_dir /tmp/model \
  --quantization int8 --force
aws s3 sync /tmp/model s3://your-whisper-models/models/openai--whisper-large-v3-turbo/

# Pre-converted CTranslate2 models (e.g. Systran/*) — download as-is
python -c "
from huggingface_hub import snapshot_download
snapshot_download('Systran/faster-distil-whisper-large-v3', local_dir='/tmp/model')
"
aws s3 sync /tmp/model s3://your-whisper-models/models/Systran--faster-distil-whisper-large-v3/
```

> **Tip:** Use the `sync-model` workflow with `quantization=int8` for HuggingFace-format models, or `quantization=none` for pre-converted CTranslate2 models (Systran, etc.).

## GitHub Actions Variables

Set these in `Settings → Secrets and variables → Variables`:

| Variable | Description | Example |
|----------|-------------|---------|
| `AWS_DEPLOY_ROLE_ARN` | OIDC deploy role ARN (from step 1) | `arn:aws:iam::123456789:role/whisper-adaptor-deploy` |
| `AWS_REGION` | AWS region | `us-west-2` |
| `ECR_REPOSITORY` | ECR repository name | `aws-lambda-whisper-adaptor` |
| `MODEL_S3_BUCKET` | S3 bucket for model storage | `my-whisper-models` |
| `LAMBDA_FUNCTION_NAME` | Lambda function name | `whisper-adaptor` |
| `LAMBDA_FUNCTION_NAME_INT8` | (Optional) separate int8 Lambda | `whisper-adaptor-int8` |

## API

### OpenAI compatible

```bash
curl -X POST https://<function-url>/v1/audio/transcriptions \
  -H "Authorization: Token <secret>" \
  -F "file=@audio.ogg"
```

```json
{"text": "transcript here"}
```

### Deepgram compatible

```bash
curl -X POST https://<function-url>/v1/listen \
  -H "Authorization: Token <secret>" \
  -H "Content-Type: audio/ogg" \
  --data-binary @audio.ogg
```

### Model management (non-standard)

List all models currently on EFS:

```bash
curl https://<function-url>/v1/models \
  -H "Authorization: Token <secret>"
```

```json
{
  "object": "list",
  "data": [
    {"id": "openai/whisper-large-v3-turbo", "object": "model", "created": 1234567890, "owned_by": "openai"},
    {"id": "Systran/faster-distil-whisper-large-v3", "object": "model", "created": 1234567890, "owned_by": "Systran"}
  ]
}
```

Delete a model from EFS (the currently loaded model returns 409):

```bash
curl -X DELETE https://<function-url>/v1/models/Systran/faster-distil-whisper-large-v3 \
  -H "Authorization: Token <secret>"
```

```json
{"id": "Systran/faster-distil-whisper-large-v3", "object": "model", "deleted": true}
```

## Configuration

| Env Var | Description | Default |
|---------|-------------|---------|
| `HF_MODEL_REPO` | HuggingFace model repo | `openai/whisper-large-v3-turbo` |
| `MODEL_S3_BUCKET` | S3 bucket for model storage | — |
| `API_SECRET` | Auth token (optional but recommended) | — |
| `WHISPER_COMPUTE_TYPE` | CTranslate2 compute type | `int8` |
| `WHISPER_LANGUAGE` | Force language (e.g. `en`, `yue`) | auto-detect |

## Recommended Models

Use the `sync-model` workflow to download and convert models.

| `HF_MODEL_REPO` | Source | Size (int8) | EFS Cold Start | Warm (2.5s audio) | sync-model `quantization` | Notes |
|-----------------|--------|-------------|----------------|-------------------|---------------------------|-------|
| `openai/whisper-large-v3-turbo` | ✅ Official OpenAI | ~780MB | ~60s | **~10s** ✅ | `int8` | **Recommended** |
| `openai/whisper-large-v3` | ✅ Official OpenAI | ~1.6GB | ~82s | ~15s | `int8` | Higher accuracy |
| `Systran/faster-whisper-large-v3` | ✅ Official SYSTRAN | ~1.6GB | ~52s | ~13s | `none` | Pre-converted CTranslate2; 6GB memory |
| `Systran/faster-distil-whisper-large-v3` | ✅ Official SYSTRAN | ~700MB | ~25s | ~10s | `none` | Pre-converted CTranslate2; good accuracy/speed balance |

> **Tip:** Pass `language` in your request to cut response time roughly in half. Without it, Whisper runs language detection on every request (~22s). With `language=yue` (Cantonese), it drops to ~10s.

## Known Issues

**`float16` compute type fails on CPU Lambda**

CTranslate2 requires GPU for float16. Setting `WHISPER_COMPUTE_TYPE=float16` crashes the Lambda:
```
ValueError: Requested float16 compute type, but the target device or backend do not support efficient float16 computation.
```
Use `int8` — CTranslate2 quantizes FP16 weights at load time with minimal accuracy loss.

**`/tmp` is too small for local model downloads**

Lambda's `/tmp` has ~856MB free in practice. Downloading models locally before S3 upload requires a directory with sufficient space (e.g. `~/whisper-models/`).

## Why not SageMaker Serverless or Bedrock?

**SageMaker Serverless:** 6GB memory limit. `whisper-large-v3-turbo` (INT8) needs ~2.8GB minimum — leaving almost no headroom for audio processing. Cold starts are 60-90s with no EFS equivalent for model caching. Cost is higher for sporadic workloads.

**Bedrock Marketplace:** Whisper Large V3 Turbo is available, with auto-scaling including scale-to-zero. But scale-to-zero means SageMaker cold starts when traffic resumes — measured in **minutes**, not seconds. Keeping minimum 1 instance means paying for idle time 24/7. Either way, not suitable for sporadic personal use.

## License

MIT
