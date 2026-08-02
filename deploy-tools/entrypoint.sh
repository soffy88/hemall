#!/bin/sh
# Hemall 容器 entrypoint
# 以 root 启动，修复挂载数据卷的属主（Docker volume 挂载后属主为 root），
# 然后以 root 启动 gunicorn——gunicorn 通过 user/group 配置自动降权 worker。

set -e

echo "[entrypoint] fixing data dir ownership..."
chown -R hemall:hemall /app/var/omodul_output /app/logs 2>/dev/null || true
chown -R hemall:hemall /home/hemall 2>/dev/null || true

echo "[entrypoint] starting gunicorn (worker 将降权为 hemall): $*"
exec "$@"
