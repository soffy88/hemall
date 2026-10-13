#!/bin/bash
# 统一验证入口 (CI 防回归门禁)
# 用法: ./scripts/validate/all.sh
# 依次运行: Caddyfile、Helm Chart、Docker 构建验证

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

log() { echo "[validate-all] $*"; }
die() { echo "[validate-all] ERROR: $*" >&2; exit 1; }

cd "$REPO_ROOT"

log "开始全量验证..."

# 1. Caddyfile 验证
log "========================================"
log "1/3 Caddyfile 验证"
log "========================================"
"$REPO_ROOT/scripts/validate/caddyfile.sh" || die "Caddyfile 验证失败"

# 2. Helm Chart 验证
log "========================================"
log "2/3 Helm Chart 验证"
log "========================================"
"$REPO_ROOT/scripts/validate/helm.sh" || die "Helm Chart 验证失败"

# 3. Docker 构建验证
log "========================================"
log "3/3 Docker 构建验证"
log "========================================"
"$REPO_ROOT/scripts/validate/docker_build.sh" || die "Docker 构建验证失败"

log "========================================"
log "✅ 所有验证通过！"
log "========================================"