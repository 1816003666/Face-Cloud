FROM python:3.12-slim

WORKDIR /app

# 优先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["python", "run.py"]
