FROM python:3.11-slim

WORKDIR /app

COPY services/router_parser/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common

COPY services/router_parser/app.py .
COPY services/router_parser/gb_filters.py .
COPY services/router_parser/torofx_filters.py .

CMD ["python", "app.py"]