FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ../common ./common
COPY app.py mt5_client.py mt5_executor.py trade_manager.py ./
CMD ["python", "app.py"]
