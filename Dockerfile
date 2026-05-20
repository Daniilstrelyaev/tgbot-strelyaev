FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Создаём непривилегированного пользователя (безопасность)
RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt .
RUN python -m pip install --upgrade pip && pip install -r requirements.txt

COPY --chown=app:app app.py .

USER app

EXPOSE 8080

CMD ["sh", "-c", "exec gunicorn app:app \
    --bind 0.0.0.0:${PORT:-8080} \
    --workers 1 \
    --worker-class gthread \
    --threads 4 \
    --timeout 30 \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --max-requests 1000 \
    --max-requests-jitter 50 \
    --log-level info \
    --access-logfile -"]
