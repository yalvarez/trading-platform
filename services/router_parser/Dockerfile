FROM python:3.11-slim

WORKDIR /app

COPY services/router_parser/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common

COPY services/router_parser/app.py .
COPY services/router_parser/parsers_base.py .
COPY services/router_parser/gb_filters.py .
COPY services/router_parser/torofx_filters.py .
COPY services/router_parser/parsers_daily_signal.py .
COPY services/router_parser/parsers_goldbro_fast.py .
COPY services/router_parser/parsers_goldbro_scalp.py .
COPY services/router_parser/parsers_goldbro_long.py .
COPY services/router_parser/parsers_torofx.py .

CMD ["python", "app.py"]