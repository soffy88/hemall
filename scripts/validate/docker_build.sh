#!/bin/bash
# Docker 构建验证脚本 (CI 防回归门禁)
# 用法: ./scripts/validate/docker_build.sh
# 依赖: docker

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

log() { echo "[docker-build-validate] $*"; }
die() { echo "[docker-build-validate] ERROR: $*" >&2; exit 1; }

cd "$REPO_ROOT"

log "验证 Docker 构建..."

# 1. 验证 Dockerfile.prod 能构建 (audit P0-1, P2-1)
log "构建生产镜像 (标签: hemal-audit-test:local)..."
docker build -f Dockerfile.prod -t hemal-audit-test:local . || die "Dockerfile.prod 构建失败"

# 验证镜像内关键文件存在
log "验证镜像内容..."
docker run --rm hemal-audit-test:local python -c "import app.main; print('app.main 导入成功')" || die "镜像内 app.main 导入失败"

# 验证 platform3o 死拷贝已移除 (audit P2-1)
if docker run --rm hemal-audit-test:local ls /platform/3O/ 2>/dev/null | grep -q .; then
    die "platform3o 目录仍存在于镜像中 (应已移除死拷贝)"
fi
log "✅ platform3o 死拷贝已移除"

# 验证基础镜像 digest 钉死 (audit P2-2)
if ! grep -q "python:3.12-slim@sha256:" Dockerfile.prod; then
    die "Dockerfile.prod 基础镜像未钉死 digest"
fi
log "✅ 基础镜像 digest 已钉死"

# 2. 验证 dev Dockerfile 使用 uv sync --frozen (audit P2-2)
if ! grep -q "uv sync --frozen" Dockerfile; then
    die "dev Dockerfile 未使用 uv sync --frozen"
fi
log "✅ dev Dockerfile 使用 uv sync --frozen"

# 3. 验证 web Dockerfile 基础镜像 digest 钉死
if ! grep -q "node:22-alpine@sha256:" web/Dockerfile; then
    die "web/Dockerfile 基础镜像未钉死 digest"
fi
log "✅ web Dockerfile 基础镜像 digest 已钉死"

# 4. 验证 edge_bridge 依赖版本锁定 (audit P2-2)
if grep -q ">=" edge_bridge/requirements.txt; then
    die "edge_bridge/requirements.txt 仍使用浮动版本"
fi
log "✅ edge_bridge 依赖版本已锁定"

# 清理测试镜像
docker rmi hemal-audit-test:local >/dev/null 2>&1 || true

log "✅ Docker 构建验证全部通过"