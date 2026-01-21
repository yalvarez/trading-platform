FROM python:3.10-slim

WORKDIR /app/services

COPY services ./

RUN apt-get update && apt-get install -y --no-install-recommends git gcc build-essential libffi-dev
RUN pip install --no-cache-dir -r trade_orchestrator/requirements.txt
RUN pip install --no-cache-dir mt5linux==0.1.9

# Ejecutar como paquete desde el directorio de servicios
CMD ["python", "-m", "trade_orchestrator.app"]