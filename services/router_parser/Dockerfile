FROM python:3.11-slim

WORKDIR /app

COPY services/router_parser/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common


# Copiar todo el paquete router_parser
COPY services/router_parser /app/router_parser

# Ejecutar como módulo para habilitar imports relativos
CMD ["python", "-m", "router_parser.app"]
