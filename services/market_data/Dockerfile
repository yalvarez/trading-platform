FROM python:3.9-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
COPY services/market_data/requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends git
RUN pip install --no-cache-dir -r requirements.txt
COPY services/common ./common
COPY services/market_data/app.py .
CMD ["python", "app.py"]