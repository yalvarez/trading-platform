FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ../common ./common
COPY app.py gb_filters.py torofx_filters.py ./
CMD ["python", "app.py"]