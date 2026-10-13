# clearnode-iot-bridge — 网桥专属镜像 (不共享 hemall 镜像/Dockerfile)
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY handlers.py bridge_main.py ./

# 死信队列落盘目录 (SQLite)
RUN mkdir -p /data
ENV DLQ_PATH=/data/dlq.sqlite

CMD ["python", "bridge_main.py"]
