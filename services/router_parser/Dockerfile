FROM python:3.11-slim

# Copia todo el proyecto (ajusta si quieres solo la carpeta services)
WORKDIR /app

COPY services/ ./services/
COPY services/router_parser/requirements.txt ./services/router_parser/requirements.txt
RUN pip install --no-cache-dir -r ./services/router_parser/requirements.txt

# Opcional: asegura que Python vea /app en el PYTHONPATH
ENV PYTHONPATH="/app"

WORKDIR /app/services/router_parser

CMD ["python", "app.py"]
