#!/bin/bash
# Helm Chart 验证脚本 (CI 防回归门禁)
# 用法: ./scripts/validate/helm.sh
# 依赖: docker (用于运行 helm template 和 helm lint)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CHART_DIR="$REPO_ROOT/charts/hemal"

log() { echo "[helm-validate] $*"; }
die() { echo "[helm-validate] ERROR: $*" >&2; exit 1; }

cd "$REPO_ROOT"

[ -d "$CHART_DIR" ] || die "Chart 目录不存在: $CHART_DIR"

log "验证 Helm Chart: $CHART_DIR"

# Helm 命令封装 (优先用宿主机 helm，否则用 docker)
# 注意: docker 运行时必须将 chart 目录作为工作目录
helm_cmd() {
    if command -v helm &>/dev/null; then
        helm "$@"
    else
        docker run --rm \
            -v "$REPO_ROOT:/workspace" \
            -w "/workspace/charts/hemal" \
            alpine/helm:3.16.4 \
            "$@"
    fi
}

# 1. helm lint
log "运行 helm lint..."
if command -v helm &>/dev/null; then
    helm lint "$CHART_DIR" || die "helm lint 失败"
else
    docker run --rm \
        -v "$REPO_ROOT:/workspace" \
        -w "/workspace/charts/hemal" \
        alpine/helm:3.16.4 \
        lint . || die "helm lint 失败"
fi

# 2. 对多个 values 文件渲染模板并验证
VALUES_FILES=(
    "$CHART_DIR/values.yaml"
    "$CHART_DIR/values.staging.yaml"
    "$CHART_DIR/values.production.yaml"
    "$CHART_DIR/values.mall-sxueji.yaml"
)

for values in "${VALUES_FILES[@]}"; do
    if [ ! -f "$values" ]; then
        log "⚠️ values 文件不存在，跳过: $values"
        continue
    fi
    
    log "渲染模板: $values"
    # 转换为容器内路径
    values_container="/workspace/${values#$REPO_ROOT/}"
    # 提供必需的 secrets.existingSecret 值以通过模板渲染
    if command -v helm &>/dev/null; then
        output=$(helm template test-release "$CHART_DIR" -f "$values" --set secrets.existingSecret=test-secret 2>&1) || die "helm template 失败: $values"
    else
        output=$(docker run --rm \
            -v "$REPO_ROOT:/workspace" \
            -w "/workspace/charts/hemal" \
            alpine/helm:3.16.4 \
            template test-release . -f "$values_container" --set secrets.existingSecret=test-secret 2>&1) || die "helm template 失败: $values"
    fi
    
    # 验证关键资源存在
    check_resource() {
        local kind="$1"
        local name_pattern="$2"
        local desc="$3"
        if echo "$output" | grep -q "kind: $kind" && echo "$output" | grep -q "$name_pattern"; then
            log "  ✅ 发现资源: $desc"
        else
            die "缺少关键资源: $desc (kind=$kind, name~=$name_pattern)"
        fi
    }
    
    check_resource "Deployment" "test-release-api" "API Deployment"
    check_resource "Service" "test-release-api" "API Service"
    check_resource "Ingress" "test-release-api" "Ingress"
    check_resource "ServiceMonitor" "test-release" "ServiceMonitor"
    check_resource "PrometheusRule" "test-release" "PrometheusRule"
    check_resource "NetworkPolicy" "test-release" "NetworkPolicy"
    
    # 验证 Ingress serviceName 正确 (audit P1-2)
    if echo "$output" | grep -q "name: test-release-api-service"; then
        die "Ingress serviceName 错误: 仍为 test-release-api-service (应为 test-release-api)"
    fi
    if echo "$output" | grep -q "name: test-release-api[^ ]"; then
        log "  ✅ Ingress serviceName 正确: test-release-api"
    fi
    
    # 验证 securityContext uid/gid 对齐 (audit P1-1)
    if echo "$output" | grep -q "runAsUser: 10001"; then
        log "  ✅ securityContext runAsUser=10001 对齐镜像"
    else
        die "securityContext runAsUser 不是 10001"
    fi
    
    # 验证没有 command 覆盖 entrypoint (audit P1-1)
    if echo "$output" | grep -A 20 "kind: Deployment" | grep -q "command:"; then
        die "Deployment 包含 command 覆盖 entrypoint (应移除以保留 entrypoint.sh 降权)"
    fi
    
    # 验证 env 变量带 HEMALL_ 前缀
    if echo "$output" | grep -q "name: LOG_LEVEL[^_]"; then
        die "发现无前缀 env 变量 LOG_LEVEL (应为 HEMALL_LOG_LEVEL)"
    fi
    if echo "$output" | grep -q "name: GUNICORN_WORKERS[^_]"; then
        die "发现无前缀 env 变量 GUNICORN_WORKERS (应为 HEMALL_GUNICORN_WORKERS)"
    fi
    
    # 验证 values.mall-sxueji.yaml 镜像 tag 不是 latest (audit P2-8)
    if [ "$values" = "$CHART_DIR/values.mall-sxueji.yaml" ]; then
        if echo "$output" | grep -q "image:.*:latest"; then
            die "values.mall-sxueji.yaml 仍使用 latest tag"
        fi
        log "  ✅ values.mall-sxueji.yaml 镜像 tag 已钉死"
    fi
done

# 3. kubeconform 验证 (如果可用)
if command -v kubeconform &>/dev/null; then
    log "运行 kubeconform 验证..."
    for values in "${VALUES_FILES[@]}"; do
        [ -f "$values" ] || continue
        helm_cmd template test-release "$CHART_DIR" -f "$values" | kubeconform -strict -summary || die "kubeconform 失败: $values"
    done
    log "✅ kubeconform 验证通过"
else
    log "⚠️ kubeconform 未安装，跳过 Kubernetes schema 验证"
fi

log "✅ Helm Chart 所有验证通过"