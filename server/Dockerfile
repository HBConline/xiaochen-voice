# xiaochen-voice server：语音管线（ASR/意图/TTS/MCP）
# 构建：docker build -t ghcr.io/hbconline/xiaochen-voice-server:v0.1 .
# 模型不打进镜像，跑的时候挂载（见 docker-compose.yml）
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# OCI 来源标签：GHCR 据此自动关联仓库（放末尾保构建缓存）
LABEL org.opencontainers.image.source="https://github.com/HBConline/xiaochen-voice"

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv/app

WORKDIR /srv/app
EXPOSE 8899
CMD ["python", "wakeword_server.py"]
