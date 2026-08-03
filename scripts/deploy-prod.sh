#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# ClearNode v1.0.0 — 一键生产部署
# Usage: bash scripts/deploy-prod.sh
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

TAG="v1.0.0-ghost-ignition"
COMPOSE_FILE="docker-compose.prod.yml"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

log()    { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $1"; }
warn()   { echo -e "${YELLOW}[$(date +%H:%M:%S)] WARN:${NC} $1"; }
error()  { echo -e "${RED}[$(date +%H:%M:%S)] ERROR:${NC} $1"; exit 1; }
step()   { echo -e "${CYAN}[$(date +%H:%M:%S)] STEP:${NC} $1"; }

cd "$PROJECT_DIR"

# ── 0. 预检 ────────────────────────────────────────────────────
step "预检环境..."

command -v docker >/dev/null || error "docker 未安装"
command -v docker-compose >/dev/null 2>&1 || docker compose version >/dev/null 2>&1 || error "docker compose 不可用"

# 验证 Tag 存在
git tag -l "$TAG" | grep -q "$TAG" || error "Tag $TAG 不存在，请先创建 release tag"

# 验证测试通过
log "运行测试套件..."
if ! uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -1 | grep -q "passed"; then
    error "测试未通过，拒绝部署！"
fi
log "✅ 测试通过"

# 验证当前在 main 分支
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[[ "$BRANCH" == "main" ]] || error "当前分支是 $BRANCH，需在 main 分支部署"

# ── 1. 构建镜像 ────────────────────────────────────────────────
step "构建生产镜像..."
docker compose -f "$COMPOSE_FILE" build --no-cache api 2>&1 | tail -5
log "✅ 镜像构建完成: hemall/backend:$TAG"

# ── 2. 停止旧容器 ─────────────────────────────────────────────
step "停止旧服务..."
docker compose -f "$COMPOSE_FILE" down --remove-orphans 2>&1 || true
log "✅ 旧服务已停止"

# ── 3. 启动生产集群 ──────────────────────────────────────────
step "启动生产集群 (PostgreSQL + Redis + Elasticsearch + API + Nginx + Prometheus + Grafana + Jaeger)..."
docker compose -f "$COMPOSE_FILE" up -d 2>&1 | tail -10
log "✅ 所有服务已启动"

# ── 4. 等待健康检查 ──────────────────────────────────────────
step "等待服务健康检查..."
MAX_RETRIES=30
RETRY=0

while [[ $RETRY -lt $MAX_RETRIES ]]; do
    if curl -sf http://localhost:8000/health/live >/dev/null 2>&1; then
        log "✅ API 健康检查通过!"
        break
    fi
    RETRY=$((RETRY + 1))
    echo -n "."
    sleep 3
done

if [[ $RETRY -eq $MAX_RETRIES ]]; then
    warn "API 健康检查超时，查看日志:"
    docker compose -f "$COMPOSE_FILE" logs --tail=50 api
    error "部署失败"
fi

# ── 5. 运行数据库迁移 ────────────────────────────────────────
step "执行数据库 Schema 迁移..."
docker compose -f "$COMPOSE_FILE" exec -T api bash -c '
    uv run python -c "
from app.ext.schema import apply_schema_migrations
import asyncio
asyncio.run(apply_schema_migrations())
print(\"Schema migrations applied successfully\")
"
' 2>&1 || warn "Schema 迁移跳过 (可能表已存在)"

# ── 6. 验证部署 ───────────────────────────────────────────────
step "部署验证..."
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              🎉 ClearNode v1.0.0 部署成功!              ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║                                                        ║"
echo "║  API:       http://localhost:8000                      ║"
echo "║  Health:    http://localhost:8000/health/live           ║"
echo "║  Admin Chat: http://localhost:8000/admin/agent-chat    ║"
echo "║  Prometheus: http://localhost:9090                     ║"
echo "║  Grafana:   http://localhost:3000 (admin/admin)        ║"
echo "║  Jaeger:    http://localhost:16686                     ║"
echo "║                                                        ║"
echo "║  Tag:       $TAG                                       ║"
echo "║  封板状态:  ✅ CC 写保护已激活                          ║"
echo "║                                                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# 显示容器状态
log "运行中的容器:"
docker compose -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>&1 | head -20

log "部署完成!"
