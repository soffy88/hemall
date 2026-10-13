#!/bin/bash
# Hemall Kubernetes 部署脚本
#
# 设计要点（审计 P1-3 / P1-4 的教训，改动时请一并保持）：
#   1. `--set` 的键必须与 charts/hemal/values.yaml 的真实路径一致，并带
#      `--strict` —— Helm 对未知键只告警并忽略，拼错的键会退化成"部署成功
#      但没生效"。加 --strict 后拼错直接报错。
#   2. 部署后**核对集群里实际运行的镜像**，不匹配就非零退出。rollout status
#      在 spec 没变化时会"立即成功"，不能当成"新版本已上线"的证据。
#   3. 健康检查必须有断言，`kubectl get` 只是展示状态。

set -euo pipefail

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 日志函数
log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARN:${NC} $1"
}

log_error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1" >&2
}

die() {
    log_error "$1"
    exit 1
}

# Chart 内的真实值路径：镜像 tag
IMAGE_TAG_KEY="api.image.tag"
# Chart 内的真实值路径：外部 Secret 名
SECRET_KEY="secrets.existingSecret"
# release 名 (templates 里所有资源都以 {{ .Release.Name }}- 开头)
RELEASE_NAME="hemall"

# 检查必要工具
check_prerequisites() {
    log "检查必要工具..."

    local commands=("kubectl" "helm" "docker" "git")
    for cmd in "${commands[@]}"; do
        command -v "${cmd}" &> /dev/null || die "${cmd} 未安装，请先安装"
    done

    log "所有必要工具已安装"
}

# 构建 Docker 镜像
build_image() {
    local tag=$1
    local registry=$2

    log "构建 Docker 镜像: ${registry}/hemall:${tag}"

    # 3O 平台包由 uv sync --frozen 依 pyproject.toml [tool.uv.sources] 的固定
    # git rev 拉取安装，无需 additional build context（审计 P2-1）。
    docker build -f Dockerfile.prod \
        -t "${registry}/hemall:${tag}" .

    # 推送到仓库
    if [ "$registry" != "local" ]; then
        log "推送镜像到仓库: ${registry}/hemall:${tag}"
        docker push "${registry}/hemall:${tag}"
    fi

    log "镜像构建完成"
}

# 部署到 Kubernetes
deploy_to_k8s() {
    local tag=$1
    local namespace=$2
    local values_file=$3
    local secret_name=$4

    log "部署到 Kubernetes 命名空间: ${namespace}"

    # 创建命名空间（已存在时忽略，不视为失败）
    kubectl create namespace "${namespace}" || log_warn "命名空间 ${namespace} 可能已存在"

    # 部署应用
    #   --strict : 未知 --set 键直接报错，而不是告警后静默忽略
    #   键路径    : api.image.tag / secrets.existingSecret，与 chart values 对齐
    helm upgrade --install "${RELEASE_NAME}" ./charts/hemal \
        --namespace "${namespace}" \
        --strict \
        --set "${IMAGE_TAG_KEY}=${tag}" \
        --set "${SECRET_KEY}=${secret_name}" \
        --values "${values_file}" \
        --timeout 10m0s

    log "等待部署完成..."
    kubectl rollout status "deployment/${RELEASE_NAME}-api" -n "${namespace}" --timeout=300s
    # 就绪门：等待可用副本（readiness 503 时此步失败，阻止坏版本接流量）
    kubectl wait --for=condition=available "deployment/${RELEASE_NAME}-api" -n "${namespace}" --timeout=300s

    log "部署完成"
}

# 部署后核对集群里**实际运行**的镜像
verify_running_image() {
    local tag=$1
    local namespace=$2

    log "核对集群实际运行的镜像 (期望 tag: ${tag})..."

    local running
    running=$(kubectl get "deployment/${RELEASE_NAME}-api" -n "${namespace}" \
        -o jsonpath='{.spec.template.spec.containers[?(@.name=="api")].image}')

    if [ -z "${running}" ]; then
        die "读不到 deployment/${RELEASE_NAME}-api 的镜像，无法确认新版本是否真的上线"
    fi

    log "集群实际镜像: ${running}"

    # 比较 tag 部分（忽略 registry/仓库前缀差异）
    local running_tag="${running##*:}"
    if [ "${running_tag}" != "${tag}" ]; then
        die "镜像校验失败：请求 tag=${tag}，集群实际在跑 ${running}。
  这通常意味着 --set 的键路径写错（Helm 会静默忽略未知键），
  或 values 文件覆盖了该键。请核对 chart values 中 ${IMAGE_TAG_KEY} 的层级。"
    fi

    log "镜像校验通过: ${running}"
}

# 运行健康检查（带断言，不只是打印状态）
run_health_check() {
    local namespace=$1
    local deployment="${RELEASE_NAME}-api"

    log "运行健康检查..."

    # 展示集群状态
    kubectl get pods -n "${namespace}"
    kubectl get svc -n "${namespace}"
    kubectl get deployments -n "${namespace}"

    # 断言 1: 所有期望副本都已就绪（就绪数 < 期望数说明还有 Pod 没起来）
    local desired ready
    desired=$(kubectl get "deployment/${deployment}" -n "${namespace}" -o jsonpath='{.spec.replicas}')
    ready=$(kubectl get "deployment/${deployment}" -n "${namespace}" -o jsonpath='{.status.readyReplicas}')
    ready=${ready:-0}
    if [ "${ready}" -lt "${desired}" ]; then
        die "就绪副本 ${ready}/${desired}：有 Pod 没通过 readiness，流量会被打到这里"
    fi
    log "就绪副本 ${ready}/${desired} OK"

    # 断言 2: Service 的 selector 必须真的选得到 API pod。
    # 曾经发生过 Ingress/Service 名字或 selector 写错 → 静默 503/无后端
    # （audit P1-2），而 rollout status 对此完全无感。
    local endpoints
    endpoints=$(kubectl get "endpoints/${RELEASE_NAME}-api" -n "${namespace}" \
        -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)
    if [ -z "${endpoints}" ]; then
        die "Service ${RELEASE_NAME}-api 没有任何就绪后端 endpoint —— 流量会直接 503"
    fi
    log "Service 后端 endpoint: ${endpoints}"

    log "健康检查完成"
}

# 主部署函数
main() {
    local environment=${1:-production}
    local tag=${2:-0.3.0}
    local registry=${3:-your-registry.com}
    # 生产凭据由外部 Secret 管理；chart 的 secret.yaml 对空值 fail-closed。
    # 允许用 HEMALL_EXISTING_SECRET 覆盖，否则回落到按环境命名的约定。
    local secret_name=${HEMALL_EXISTING_SECRET:-"${RELEASE_NAME}-${environment}-secrets"}

    log "开始 Hemall ${environment} 环境部署"
    log "镜像标签: ${tag}"
    log "镜像仓库: ${registry}"
    log "凭据 Secret: ${secret_name}"

    check_prerequisites

    build_image "${tag}" "${registry}"

    # 根据环境选择 values 文件（缺失时回落默认并告警）
    local values_file="charts/hemal/values.yaml"
    case "${environment}" in
        staging) values_file="charts/hemal/values.staging.yaml" ;;
        production) values_file="charts/hemal/values.production.yaml" ;;
        mall-sxueji) values_file="charts/hemal/values.mall-sxueji.yaml" ;;
    esac
    if [ ! -f "${values_file}" ]; then
        log_warn "values 文件 ${values_file} 不存在，回落 charts/hemal/values.yaml"
        values_file="charts/hemal/values.yaml"
    fi

    deploy_to_k8s "${tag}" "${environment}" "${values_file}" "${secret_name}"

    # 先核对镜像，再谈健康 —— 顺序反了就是"假上线"的温床
    verify_running_image "${tag}" "${environment}"
    run_health_check "${environment}" "${RELEASE_NAME}-api"

    log "Hemall ${environment} 环境部署成功完成! (镜像 tag: ${tag})"
}

# 显示帮助信息
show_help() {
    echo "用法: $0 [环境] [标签] [仓库]"
    echo ""
    echo "参数:"
    echo "  环境    部署环境 (staging|production|mall-sxueji，默认: production)"
    echo "  标签    Docker 镜像标签 (默认: 0.3.0，与 Chart appVersion 对齐)"
    echo "         不要传 latest —— chart 已钉死 tag，浮动 tag 无法复现也无法回滚"
    echo "  仓库    Docker 镜像仓库 (默认: your-registry.com)"
    echo ""
    echo "凭据:"
    echo "  HEMALL_EXISTING_SECRET  外部 Secret 名 (默认: hemall-<环境>-secrets)"
    echo "         chart 不内置任何生产密钥，Secret 至少要提供 HEMALL_PG_DSN、"
    echo "         HEMALL_REDIS_URL、HEMALL_JWT_SECRET、HEMALL_WEBHOOK_SECRET、"
    echo "         HEMALL_HARDWARE_SECRET、HEMALL_CORS_ORIGINS 等"
    echo ""
    echo "示例:"
    echo "  $0 production v1.2.1 registry.example.com"
    echo "  $0 staging v1.2.1"
    echo ""
    echo "环境变量:"
    echo "  HEMALL_EXISTING_SECRET=my-secret $0 production v1.2.1"
}

# 参数处理
case "${1:-}" in
    -h|--help)
        show_help
        exit 0
        ;;
    *)
        main "$@"
        ;;
esac
