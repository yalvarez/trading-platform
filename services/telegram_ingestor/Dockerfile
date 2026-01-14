FROM python:3.11-slim
WORKDIR /app
COPY services/telegram_ingestor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/common ./common
COPY services/telegram_ingestor/notify_api.py .
COPY services/telegram_ingestor/app.py .
COPY services/telegram_ingestor/telegram_ingestor.session .
COPY services/telegram_ingestor/telegram_ingestor_api.session .
COPY services/telegram_ingestor/supervisord.conf ./supervisord.conf

# Ejecuta API HTTP y proceso de ingesta en paralelo
RUN pip install --no-cache-dir supervisor
CMD ["supervisord", "-c", "./supervisord.conf"]