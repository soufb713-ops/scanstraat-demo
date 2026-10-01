FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 tesseract-ocr tesseract-ocr-nld cron \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN mkdir -p /keys /data /scans
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN rm -rf data .env .venv __pycache__ .vault-key .backup-key backups tests
ENV SCAN_DATA=/data SCAN_INBOX=/scans VAULT_KEY_FILE=/keys/vault-key BACKUP_KEY_FILE=/keys/backup-key PYTHONUNBUFFERED=1
EXPOSE 5000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % __import__('os').environ.get('PORT','5000'))"
CMD ["sh", "-c", "exec gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT:-5000} --timeout 600 app:app"]
