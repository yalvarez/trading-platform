FROM python:3.11-slim

WORKDIR /app

COPY services/ ./services/
COPY services/telegram_ingestor/requirements.txt ./services/telegram_ingestor/requirements.txt
RUN pip install --no-cache-dir -r ./services/telegram_ingestor/requirements.txt

ENV PYTHONPATH="/app"

WORKDIR /app/services/telegram_ingestor

RUN pip install --no-cache-dir supervisor
COPY supervisord.conf ./supervisord.conf

CMD ["supervisord", "-c", "./supervisord.conf"]