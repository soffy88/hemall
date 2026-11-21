#!/bin/sh
# Hemall 容器 entrypoint
#
# 两条启动路径都要支持:
#
# 1. compose (默认, root 启动): 先把挂载进来的数据卷属主从 root 修成 hemall,
#    再以 root 启动 gunicorn master —— gunicorn 读 conf 里的 user/group 后
#    对每个 worker 调 setgid/setuid 完成降权。
# 2. Kubernetes (runAsNonRoot, uid 10001 启动): 此时 chown 必然 EPERM，
#    必须跳过而不是失败退出。属主改由 securityContext 的 fsGroup 负责
#    (见 charts/hemal/templates/deployment.yaml)。
#
# 历史上这里是无条件 chown + `|| true`，非 root 启动时日志里仍打着
# "fixing data dir ownership..."，让人误以为属主已修 —— 现按 uid 分支。

set -e

if [ "$(id -u)" = "0" ]; then
    echo "[entrypoint] running as root: fixing data dir ownership before dropping to hemall"
    chown -R hemall:hemall /app/var/omodul_output /app/logs 2>/dev/null || true
    chown -R hemall:hemall /home/hemall 2>/dev/null || true
    echo "[entrypoint] starting gunicorn as root master (workers drop to hemall): $*"
else
    echo "[entrypoint] running as uid $(id -u): skipping chown (needs root), relying on image/fsGroup ownership: $*"
fi

exec "$@"
