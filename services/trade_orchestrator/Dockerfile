FROM python:3.10-slim

WORKDIR /app

COPY services ./services

RUN apt-get update && apt-get install -y --no-install-recommends git gcc build-essential libffi-dev
RUN pip install --no-cache-dir -r services/trade_orchestrator/requirements.txt
RUN pip install --no-cache-dir mt5linux==0.1.9

CMD ["python", "-m", "services.trade_orchestrator.app"]