#!/bin/bash
# Caddyfile 验证脚本 (CI 防回归门禁)
# 用法: ./scripts/validate/caddyfile.sh
# 依赖: docker (用于运行 caddy validate)

set -euo pipefail

CADDYFILE="${1:-Caddyfile}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

log() { echo "[caddyfile-validate] $*"; }
die() { echo "[caddyfile-validate] ERROR: $*" >&2; exit 1; }

cd "$REPO_ROOT"

[ -f "$CADDYFILE" ] || die "Caddyfile 不存在: $CADDYFILE"

log "验证 Caddyfile: $CADDYFILE"

# 使用 docker 运行 caddy validate (避免宿主机需要安装 caddy)
docker run --rm \
    -v "$REPO_ROOT/$CADDYFILE:/etc/caddy/Caddyfile" \
    caddy:2.11.4-alpine \
    caddy validate --config /etc/caddy/Caddyfile \
    || die "Caddyfile 语法验证失败"

log "✅ Caddyfile 语法验证通过"

# 额外检查: 关键安全配置存在
check_config() {
    local pattern="$1"
    local desc="$2"
    if grep -q "$pattern" "$CADDYFILE"; then
        log "✅ 发现配置: $desc"
    else
        die "缺少关键配置: $desc (模式: $pattern)"
    fi
}

# 检查关键路由前缀 (防漂移)
check_config 'payments' '支付回调路由'
check_config 'ext' '硬件 webhook 路由'
check_config 'ai' 'AI 助手路由'
check_config 'eventsourcing' '事件溯源路由'
check_config 'ws' 'WebSocket 路由'

# 检查生产安全限制
check_config 'respond "Not Found" 404' '/docs 和 /openapi.json 关闭'
check_config 'remote_ip private_ranges' '/metrics 仅内网访问'

# 检查上游容器名环境变量化
check_config '\$HEMALL_API_CONTAINER' 'API 容器名环境变量'
check_config '\$HEMALL_WEB_CONTAINER' 'Web 容器名环境变量'

log "✅ 所有关键配置检查通过"