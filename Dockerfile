FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg pkg-config gcc \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir faster-whisper boto3 awslambdaric

WORKDIR /var/task
COPY handler.py .

ENTRYPOINT ["/usr/local/bin/python", "-m", "awslambdaric"]
CMD ["handler.handler"]
