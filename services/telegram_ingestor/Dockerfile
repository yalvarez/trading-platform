FROM python:3.11-slim
WORKDIR /app
COPY services/telegram_ingestor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/common ./common
COPY services/telegram_ingestor/app.py .
CMD ["python", "app.py"]