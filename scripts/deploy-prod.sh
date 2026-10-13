#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Hemall 生产环境一键部署 (docker compose 路径)
# Usage: bash scripts/deploy-prod.sh
#
# 审计 P1-4 修正的三个点，改动时请一并保持：
#   1. 探活**不能**打宿主机的 localhost:8000 —— prod compose 刻意不发布 api
#      端口（只经 nginx 暴露），那个口永远没人监听。改为在 api 容器内 curl，
#      并额外从宿主经 nginx 走一遍，验证真实入口链路。
#   2. 不再有"执行数据库 Schema 迁移"这一步 —— 它调用的
#      `apply_schema_migrations` 在代码库中不存在，运行时容器里也没有 uv，
#      失败还被 `|| warn` 吞掉。真实入口是 `app/bootstrap.py:init_db()`，
#      由 lifespan 幂等执行并用 advisory lock 串行化。这里改为**真实断言**：
#      调 init_db + 查库确认关键表存在，失败即让部署失败。
#   3. 收尾输出不得打印与实际不符的信息（ClearNode 品牌、admin/admin 口令、
#      已过期的硬编码 tag）。
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# 发布 tag：默认取 Chart 的 appVersion，可用 HEMALL_DEPLOY_TAG 覆盖。
# 原来这里硬编码 `v1.0.0-ghost-ignition`（早已过期的 tag），发新版本时
# 部署脚本推送的却是历史版本号，与实际镜像对不上。
_chart_appversion() {
    sed -n 's/^appVersion: *//p' charts/hemal/Chart.yaml | head -1 | tr -d '"[:space:]'
}
TAG="${HEMALL_DEPLOY_TAG:-$(_chart_appversion)}"
COMPOSE_FILE="docker-compose.prod.yml"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 探活时打到的关键表：这些由本项目的 ensure_* bootstrap 建出来。
# 部署脚本据此发现"应用跑起来了但 schema 是残的"这种情况 —— 原来那个假的
# 迁移步骤从不失败，也就从不告诉任何人。
SCHEMA_TABLES=(payment_intent payment_session payment_outbox audit_log event_store stock_movement order_status_history inventory_reservation)

log()    { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $1"; }
warn()   { echo -e "${YELLOW}[$(date +%H:%M:%S)] WARN:${NC} $1"; }
error()  { echo -e "${RED}[$(date +%H:%M:%S)] ERROR:${NC} $1" >&2; exit 1; }
step()   { echo -e "${CYAN}[$(date +%H:%M:%S)] STEP:${NC} $1"; }

cd "$PROJECT_DIR"

[ -n "$TAG" ] || error "无法确定发布 tag：请设置 HEMALL_DEPLOY_TAG"

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# ── 0. 预检 ────────────────────────────────────────────────────
step "预检环境..."

command -v docker >/dev/null || error "docker 未安装"
docker compose version >/dev/null 2>&1 || error "docker compose 不可用"
[ -f "$COMPOSE_FILE" ] || error "找不到 $COMPOSE_FILE"

# ── 1. 构建镜像 ────────────────────────────────────────────────
step "构建生产镜像 (tag: $TAG)..."
compose build api
log "✅ 镜像构建完成"

# ── 2. 停止旧容器 ─────────────────────────────────────────────
step "停止旧服务..."
# down 失败要看见：静默 continue 会让新旧两个版本同时在跑
compose down --remove-orphans || error "停止旧服务失败（可能有容器仍被占用）"
log "✅ 旧服务已停止"

# ── 3. 启动生产集群 ──────────────────────────────────────────
step "启动生产集群 (PostgreSQL + Redis + Elasticsearch + API + Nginx + Alertmanager + Prometheus + Grafana + Jaeger)..."
compose up -d
log "✅ 所有服务已启动"

# ── 4. 等待健康检查 ──────────────────────────────────────────
# 注意端口：prod compose 不发布 api 端口（compose 注释：只经 nginx 暴露，
# 不要把 worker 端口挂到宿主）。宿主机能打到的是 nginx 的 80/443。
#   (a) 容器内 curl localhost:8000 —— 权威判据：应用进程本身是否健康
#   (b) 宿主经 nginx 443      —— 真实入口链路（TLS + 反代）是否通
step "等待 API 健康检查 (容器内直连)..."
MAX_RETRIES="${HEMALL_DEPLOY_HEALTH_RETRIES:-30}"
RETRY_INTERVAL="${HEMALL_DEPLOY_HEALTH_INTERVAL:-3}"
RETRY=0
API_HEALTHY=0

while [ "$RETRY" -lt "$MAX_RETRIES" ]; do
    if compose exec -T api curl -sf http://localhost:8000/health/live >/dev/null 2>&1; then
        API_HEALTHY=1
        log "✅ API 容器内健康检查通过!"
        break
    fi
    RETRY=$((RETRY + 1))
    printf '.'
    sleep "$RETRY_INTERVAL"
done

if [ "$API_HEALTHY" -ne 1 ]; then
    echo
    warn "容器内健康检查超时，API 日志:"
    compose logs --tail=50 api
    error "部署失败：API 未在 $((MAX_RETRIES * RETRY_INTERVAL)) 秒内通过 /health/live"
fi

# 经 nginx 验一遍真实入口（域名由 compose 必填注入，缺则无法验 TLS）
API_SERVER_NAME="${HEMALL_API_SERVER_NAME:-}"
if [ -n "$API_SERVER_NAME" ]; then
    step "等待入口健康检查 (经 nginx https://$API_SERVER_NAME/health/live)..."
    ENTRY_RETRY=0
    ENTRY_OK=0
    while [ "$ENTRY_RETRY" -lt "$MAX_RETRIES" ]; do
        # --resolve 把域名指到本机 nginx，同时保留正确的 SNI 与证书校验。
        # 故意不加 --insecure：证书有问题正是这里该抓出来的。
        if curl -sf --resolve "${API_SERVER_NAME}:443:127.0.0.1" \
            "https://${API_SERVER_NAME}/health/live" >/dev/null 2>&1; then
            ENTRY_OK=1
            log "✅ 入口 (nginx) 健康检查通过!"
            break
        fi
        ENTRY_RETRY=$((ENTRY_RETRY + 1))
        printf '.'
        sleep "$RETRY_INTERVAL"
    done
    if [ "$ENTRY_OK" -ne 1 ]; then
        echo
        warn "入口健康检查超时，nginx 日志:"
        compose logs --tail=50 nginx
        error "部署失败：nginx 入口 https://${API_SERVER_NAME}/health/live 未通过（TLS 或反代有问题）"
    fi
else
    warn "未设置 HEMALL_API_SERVER_NAME，跳过入口（nginx/TLS）健康检查。"
    warn "一键部署请同时导出 HEMALL_API_SERVER_NAME（须与证书 CN/SAN 一致）。"
fi

# ── 5. 验证数据库 schema 真的就绪 ─────────────────────────────
# 原先这里叫"执行数据库 Schema 迁移"，执行的是
# `uv run python -c "from app.ext.schema import apply_schema_migrations"`
# —— 运行时镜像没有 uv，该函数在代码库里也不存在，失败还被 `|| warn` 吞掉。
#
# 真实情况：schema 由 app/bootstrap.py:init_db() 在 lifespan 里幂等创建，
# 并用 PostgreSQL advisory lock 串行化（并发 worker 不会互相踩）。
# 所以这里不需要"迁移"，只需要**断言**它确实建好了 —— 断言失败必须让
# 部署失败，否则部署脚本报告成功而线上库表残缺。
step "验证数据库 schema 就绪..."
compose exec -T api python - <<'PY' || error "Schema 验证失败：应用 bootstrap 未能确保 schema 完整"
import asyncio

from app.bootstrap import init_db
from app.config import Settings

pool, task = asyncio.run(init_db(Settings()))
if task is not None:
    task.cancel()
assert pool is not None, "init_db 返回的连接池为 None：数据库不可达"
pool.close()
print("bootstrap ok")
PY

# 查库确认关键表真的存在（而不是"跑了个函数就算过"）
CHECK_SQL="SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename IN ($(printf "'%s'," "${SCHEMA_TABLES[@]}" | sed 's/,$//'));"
TABLES_FOUND=$(compose exec -T postgres sh -c \
    "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"$CHECK_SQL\"" | tr -d '[:space:]')
TABLES_WANTED=${#SCHEMA_TABLES[@]}

if ! [ "$TABLES_FOUND" -eq "$TABLES_WANTED" ] 2>/dev/null; then
    error "Schema 验证失败：只找到 $TABLES_FOUND/$TABLES_WANTED 张关键表（缺: ${SCHEMA_TABLES[*]}）"
fi
log "✅ Schema 验证通过 ($TABLES_FOUND/$TABLES_WANTED 张关键表)"

# ── 6. 验证部署 ───────────────────────────────────────────────
step "部署验证..."
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              🎉 Hemall 部署成功                        ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  API 入口:  https://\${HEMALL_API_SERVER_NAME:-<未设置>}        ║"
echo "║  Health:    https://<域名>/health/live                    ║"
echo "║  Tag:       $TAG"
echo "║"
echo "║  仅本机回环可达的运维面板 (compose 绑定 127.0.0.1):        ║"
echo "║    Prometheus   http://127.0.0.1:9090                    ║"
echo "║    Alertmanager http://127.0.0.1:9093                    ║"
echo "║    Grafana      http://127.0.0.1:3000  (口令见           ║"
echo "║                 GF_SECURITY_ADMIN_PASSWORD，compose 强制自定义)║"
echo "║    Jaeger       http://127.0.0.1:16686                   ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

log "运行中的容器:"
compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"

log "部署完成! (tag: $TAG)"
