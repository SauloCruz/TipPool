FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8377 \
    DATA_DIR=/data \
    DB_PATH=/data/tippool.sqlite3

WORKDIR /app

RUN addgroup --system app && \
    adduser --system --ingroup app --home /app app && \
    mkdir -p /data && \
    chown -R app:app /app /data

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app app ./app
COPY --chown=app:app engine ./engine
COPY --chown=app:app static ./static
COPY --chown=app:app LICENSE README.md ./

USER app
EXPOSE 8377
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8377\")}/healthz', timeout=3).read()"

CMD ["python", "-m", "app.serve"]
