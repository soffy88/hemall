# Hemall Backend Dockerfile
# Phase 0: 多阶段构建，生产镜像 <200MB，非 root 运行

# ── 构建阶段 ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# 安装构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv (快速 Python 包管理)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# 复制项目文件
COPY pyproject.toml .
COPY app/ ./app/

# 安装依赖到虚拟环境
RUN uv venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -e ".[observability]" && \
    /opt/venv/bin/pip install --no-cache-dir gunicorn

# ── 生产阶段 ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS production

# 创建非 root 用户
RUN useradd --create-home --shell /bin/bash hemall

WORKDIR /app

# 从构建阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制应用代码
COPY app/ ./app/

# 设置权限
RUN chown -R hemall:hemall /app

# 切换到非 root 用户
USER hemall

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')" || exit 1

# 启动命令 (生产用 gunicorn + uvicorn workers)
CMD ["gunicorn", "app.main:app", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--capture-output"]
