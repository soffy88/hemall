# clearnode-iot-bridge — 网桥专属镜像 (不共享 hemall 镜像/Dockerfile)
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY handlers.py bridge_main.py ./

# 死信队列落盘目录 (SQLite)
RUN mkdir -p /data
ENV DLQ_PATH=/data/dlq.sqlite

CMD ["python", "bridge_main.py"]
