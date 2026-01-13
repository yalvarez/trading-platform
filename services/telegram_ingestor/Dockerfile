FROM python:3.11-slim
WORKDIR /app
COPY services/telegram_ingestor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/common ./common
COPY services/telegram_ingestor/notify_api.py .
COPY services/telegram_ingestor/telegram_ingestor.session .

# Solo expone la API HTTP funcional para notificaciones Telegram
CMD ["uvicorn", "notify_api:app", "--host", "0.0.0.0", "--port", "8000"]