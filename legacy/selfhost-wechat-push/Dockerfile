FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py wsgi.py ./
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "120", "--worker-tmp-dir", "/tmp", "--error-logfile", "-", "--log-level", "info"]
