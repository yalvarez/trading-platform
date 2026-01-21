FROM python:3.10-slim

WORKDIR /app

COPY services/trade_orchestrator/requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends git gcc build-essential libffi-dev
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir mt5linux==0.1.9

COPY services/common ./common

COPY services/trade_orchestrator ./trade_orchestrator

# Ejecutar como paquete para soportar imports relativos
CMD ["python", "-m", "trade_orchestrator.app"]