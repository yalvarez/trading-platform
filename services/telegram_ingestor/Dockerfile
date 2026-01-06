FROM python:3.11-slim
WORKDIR /app
COPY services/telegram_ingestor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/common ./common
COPY services/telegram_ingestor/app.py .
COPY services/telegram_ingestor/telegram_ingestor.session .

COPY services/telegram_ingestor/app_http.py .

# Arranca ambos procesos: el cliente de Telegram y el servidor HTTP
CMD ["sh", "-c", "python app.py & uvicorn app_http:app --host 0.0.0.0 --port 8000"]