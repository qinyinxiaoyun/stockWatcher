FROM python:3.12-slim AS runtime

ARG BUILD_SHA=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BUILD_SHA=${BUILD_SHA} \
    DATA_DIR=/data \
    DATABASE_PATH=/data/stock-watch.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY stock_watch ./stock_watch
RUN pip install --no-cache-dir ".[server]"

RUN mkdir -p /data /app/verification \
    && chown -R 10001:10001 /data /app/verification
USER 10001:10001

EXPOSE 8000
CMD ["gunicorn", "stock_watch.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "120", "--worker-tmp-dir", "/tmp", "--access-logfile", "-", "--error-logfile", "-"]
