# Build context is this directory (set Railway's "Root Directory" to
# kakao_legal_bot). Keeps the image off the 1GB monorepo.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    DATA_DIR=/data \
    PERSONA_PATH=/app/kakao_legal_bot/persona.md \
    CORPUS_DIR=/app/kakao_legal_bot/corpus

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/kakao_legal_bot/

# Railway mounts a volume here; without one, history is lost on redeploy.
VOLUME ["/data"]

EXPOSE 8000

# Railway injects $PORT. Shell form so it expands.
CMD uvicorn kakao_legal_bot.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers
