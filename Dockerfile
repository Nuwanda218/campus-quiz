FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py manage.py backup.py ./
COPY templates ./templates
COPY data ./data
RUN mkdir -p /app/instance /app/backups

EXPOSE 8000

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "45", "--keep-alive", "10", "--max-requests", "3000", "--max-requests-jitter", "300", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
