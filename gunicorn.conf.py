# Gunicorn 生产配置文件
#
# 容量参数从环境变量读取（charts/hemal 与各 compose 都会注入）。此前这里是
# 硬编码的 `workers = 4` / `timeout = 30`，运维在 values.yaml 里改
# GUNICORN_WORKERS 改多少遍都不生效 —— 而 Settings 是 extra="ignore"，
# 不报错也不告警，属于典型的"声明了等于没有"。
#
# 命名遵循 Settings 的 HEMALL_ 前缀约定，避免同一个环境变量名在一处被
# 当成应用配置、在另一处被当成 gunicorn 配置的歧义。

import os


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """读取正整数环境变量；缺失/非法时回落默认值而不是让服务起不来。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[gunicorn.conf] {name}={raw!r} 不是整数，回落默认 {default}")
        return default
    if value < minimum:
        print(f"[gunicorn.conf] {name}={value} 小于最小值 {minimum}，回落默认 {default}")
        return default
    return value


# Worker 进程数默认 (CPU 核心数 * 2) + 1，此处由 HEMALL_GUNICORN_WORKERS 覆盖
workers = _env_int("HEMALL_GUNICORN_WORKERS", 4)

# 请求超时时间（秒）
timeout = _env_int("HEMALL_GUNICORN_TIMEOUT", 30)

# 使用 Uvicorn worker 类以获得最佳性能
worker_class = "uvicorn.workers.UvicornWorker"

# 每个 worker 的最大并发连接数
worker_connections = 1000

# 在处理指定数量的请求后重启 worker 进程，防止内存泄漏
max_requests = 1000
max_requests_jitter = 50


# Keep-Alive 连接保持时间（秒）
keepalive = 5

# 预加载应用以减少内存占用并提高性能
preload_app = True

# Worker 临时目录
worker_tmp_dir = "/dev/shm"

# 日志级别
loglevel = "info"

# 访问日志格式
accesslog = "-"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# 错误日志
errorlog = "-"
capture_output = True

# Daemon 模式（生产环境通常由进程管理器控制，不建议开启）
daemon = False

# PID 文件
pidfile = "/tmp/gunicorn.pid"

# 用户和组（生产环境建议使用非 root 用户）
# 容器内以 root 启动 gunicorn master，worker 自动降权为 hemall
user = "hemall"
group = "hemall"

# 绑定地址
bind = "0.0.0.0:8000"

# Worker 进程名前缀
proc_name = "hemall"