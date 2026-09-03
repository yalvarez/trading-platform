FROM python:3.10-slim

WORKDIR /app

COPY services ./services

RUN apt-get update && apt-get install -y --no-install-recommends gcc build-essential
RUN pip install --no-cache-dir -r services/trade_api/requirements.txt

CMD ["uvicorn", "services.trade_api.app:app", "--host", "0.0.0.0", "--port", "8100"]
