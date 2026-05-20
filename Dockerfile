FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# FIX #4: --worker-class gthread указан явно для корректной работы потоков
# 1 worker + 4 threads: keepalive запускается ровно 1 раз,
# при этом до 4 одновременных webhook-запросов обрабатываются параллельно
CMD gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --workers 1 \
    --worker-class gthread \
    --threads 4 \
    --timeout 30 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile -
