FROM python:3.11-slim
WORKDIR /app
COPY services/trade_orchestrator/requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends git
RUN pip install --no-cache-dir -r requirements.txt
COPY services/common ./common
COPY services/trade_orchestrator/app.py .
COPY services/router_parser/gb_filters.py .
COPY services/router_parser/torofx_filters.py .
CMD ["python", "app.py"]