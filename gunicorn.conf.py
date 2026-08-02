# Gunicorn 生产配置文件

# Worker 进程数 = (CPU 核心数 * 2) + 1
workers = 4

# 使用 Uvicorn worker 类以获得最佳性能
worker_class = "uvicorn.workers.UvicornWorker"

# 每个 worker 的最大并发连接数
worker_connections = 1000

# 在处理指定数量的请求后重启 worker 进程，防止内存泄漏
max_requests = 1000
max_requests_jitter = 50

# 请求超时时间（秒）
timeout = 30

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