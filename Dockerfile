# Hemall Backend Dockerfile
# Phase 0: 多阶段构建，生产镜像 <200MB，非 root 运行

# ── 构建阶段 ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# uv 需要 Git 才能从锁定的公开提交安装 3O 依赖；项目依赖均使用
# Linux wheels，不把完整编译工具链带进 clean runner。
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global http.version HTTP/1.1 \
    && git config --global http.lowSpeedLimit 0 \
    && git config --global http.lowSpeedTime 600

# 安装 uv (快速 Python 包管理)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# 复制项目文件；3O 依赖由 pyproject 的固定 Git 提交解析，不能依赖 sibling 目录。
COPY pyproject.toml uv.lock ./
COPY app/ ./app/

# 用 uv lock 安装（pip 不读取 [tool.uv.sources]，会把内部 3O 包错误地
# 当成 PyPI 包）。
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_CONCURRENT_DOWNLOADS=4
RUN uv venv /opt/venv && \
    uv sync --frozen --no-dev --extra observability && \
    uv pip install --python /opt/venv/bin/python --no-cache gunicorn

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
