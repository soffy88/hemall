#!/usr/bin/env bash
# Hemall 蓝绿部署脚本（mall.sxueji.com live 栈）
#
# 蓝槽位 = 现有 api/web（hemal-api-1 / hemal-web-1）；绿槽位 = docker-compose.
# bluegreen.yml 的 api-green/web-green（hemal-api-green / hemal-web-green）。
# Caddy 上游由 HEMALL_API_CONTAINER / HEMALL_WEB_CONTAINER 环境变量决定，
# 切换只重建 caddy（无状态反代，数秒），旧槽位停机保留以便一键回滚。
#
# 用法（在部署目录，如 /data/soffy/wt/hemal-deploy）：
#   scripts/deploy-blue-green.sh \
#     --backend-image registry.sxueji.com/hemall/backend:0.6.0-prod \
#     --frontend-image registry.sxueji.com/hemall/frontend:0.6.0-prod
#   scripts/deploy-blue-green.sh --rollback   # 切回上一槽位（不换镜像）
#   scripts/deploy-blue-green.sh --cleanup    # 删除已停机的旧槽位容器
#
# 安全语义：
#   - 新槽位 /health/ready 不通过 → 中止，流量不动，脚本非零退出。
#   - 切换后经 Caddy 本地口（127.0.0.1:8092）复检 /health/live。
#   - 旧槽位只 stop 不 rm：回滚 = 再跑一次 --rollback。

set -euo pipefail

PROJECT="hemal"
COMPOSE_BASE=(-f docker-compose.mall-old.yml -f docker-compose.deploy.yml)
COMPOSE_BG=(-f docker-compose.mall-old.yml -f docker-compose.deploy.yml -f docker-compose.bluegreen.yml)
FLIP_FILE="docker-compose.flip.yml"
CADDY_LOCAL="http://127.0.0.1:8092"

BACKEND_IMAGE=""
FRONTEND_IMAGE=""
MODE="deploy"

while [ $# -gt 0 ]; do
  case "$1" in
    --backend-image) BACKEND_IMAGE="$2"; shift 2 ;;
    --frontend-image) FRONTEND_IMAGE="$2"; shift 2 ;;
    --rollback) MODE="rollback"; shift ;;
    --cleanup) MODE="cleanup"; shift ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

dc() { docker compose -p "$PROJECT" "$@"; }

current_color() {
  # 从运行中的 caddy 容器 env 读当前上游：含 green 即绿槽位，否则蓝槽位。
  local up
  up="$(docker inspect hemal-caddy-1 --format '{{range .Config.Env}}{{println .}}{{end}}' \
        | grep '^HEMALL_API_CONTAINER=' | cut -d= -f2- || true)"
  case "$up" in
    *green*) echo "green" ;;
    *) echo "blue" ;;
  esac
}

wait_ready() {
  # $1=api 容器名：在容器内用 python 探 /health/ready（镜像自带 python）。
  local container="$1" i
  for i in $(seq 1 30); do
    if docker exec "$container" python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)" \
      >/dev/null 2>&1; then
      echo "  $container /health/ready OK"
      return 0
    fi
    sleep 2
  done
  echo "  $container 60s 内未就绪" >&2
  return 1
}

flip_caddy() {
  # $1=目标 api 容器名, $2=目标 web 容器名
  cat > "$FLIP_FILE" <<EOF
# 由 deploy-blue-green.sh 生成：Caddy 上游切换（蓝绿）。勿手改，重跑脚本即覆盖。
services:
  caddy:
    environment:
      HEMALL_API_CONTAINER: $1
      HEMALL_WEB_CONTAINER: $2
EOF
  dc "${COMPOSE_BASE[@]}" -f "$FLIP_FILE" up -d caddy
  # 经 Caddy 复检
  for i in $(seq 1 15); do
    if curl -fsS "$CADDY_LOCAL/health/live" >/dev/null 2>&1; then
      echo "  Caddy 已切至 $1 / $2 且 /health/live OK"
      return 0
    fi
    sleep 2
  done
  echo "  切换后 Caddy /health/live 未通过" >&2
  return 1
}

case "$MODE" in
  cleanup)
    docker stop hemal-api-green hemal-web-green 2>/dev/null || true
    docker rm hemal-api-green hemal-web-green 2>/dev/null || true
    echo "已清理绿槽位容器（蓝槽位 hemal-api-1/hemal-web-1 不受影响）"
    exit 0
    ;;
  rollback)
    CUR="$(current_color)"
    if [ "$CUR" = "green" ]; then
      echo "当前绿槽位 → 切回蓝槽位"
      docker start hemal-api-1 hemal-web-1 >/dev/null 2>&1 || true
      wait_ready hemal-api-1
      flip_caddy hemal-api-1 hemal-web-1
      docker stop hemal-api-green hemal-web-green || true
    else
      echo "当前蓝槽位 → 切回绿槽位"
      docker start hemal-api-green hemal-web-green >/dev/null 2>&1 || true
      wait_ready hemal-api-green
      flip_caddy hemal-api-green hemal-web-green
      docker stop hemal-api-1 hemal-web-1 || true
    fi
    echo "回滚完成。旧槽位已停机保留，确认无误后可 --cleanup。"
    exit 0
    ;;
  deploy)
    [ -n "$BACKEND_IMAGE" ] && [ -n "$FRONTEND_IMAGE" ] || {
      echo "deploy 模式需 --backend-image 与 --frontend-image" >&2; exit 2; }
    CUR="$(current_color)"
    echo "当前槽位: $CUR"
    if [ "$CUR" = "blue" ]; then
      echo "拉起绿槽位（新镜像）…"
      BACKEND_IMAGE="$BACKEND_IMAGE" FRONTEND_IMAGE="$FRONTEND_IMAGE" \
        dc "${COMPOSE_BG[@]}" up -d api-green web-green
      wait_ready hemal-api-green
      flip_caddy hemal-api-green hemal-web-green
      docker stop hemal-api-1 hemal-web-1 || true
      echo "发布完成：流量在绿槽位（$BACKEND_IMAGE）。蓝槽位已停机保留，回滚：$0 --rollback"
    else
      echo "拉起蓝槽位（新镜像，经 override 注入镜像，勿改 deploy.yml 的钉版）…"
      cat > docker-compose.images.yml <<EOF
# 由 deploy-blue-green.sh 生成：蓝槽位镜像 override。
services:
  api:
    image: $BACKEND_IMAGE
  web:
    image: $FRONTEND_IMAGE
EOF
      dc "${COMPOSE_BASE[@]}" -f docker-compose.images.yml up -d api web
      wait_ready hemal-api-1
      flip_caddy hemal-api-1 hemal-web-1
      docker stop hemal-api-green hemal-web-green || true
      echo "发布完成：流量在蓝槽位（$BACKEND_IMAGE）。绿槽位已停机保留，回滚：$0 --rollback"
    fi
    ;;
esac
