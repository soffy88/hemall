#!/bin/bash
# Hemall 生产环境部署脚本

set -e  # 遇到错误立即退出

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
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1"
}

# 检查必要工具
check_prerequisites() {
    log "检查必要工具..."
    
    commands=("kubectl" "helm" "docker" "git")
    for cmd in "${commands[@]}"; do
        if ! command -v $cmd &> /dev/null; then
            log_error "$cmd 未安装，请先安装"
            exit 1
        fi
    done
    
    log "所有必要工具已安装"
}

# 构建 Docker 镜像
build_image() {
    local tag=$1
    local registry=$2
    
    log "构建 Docker 镜像: ${registry}/hemall:${tag}"
    
    # 构建镜像
    docker build -f Dockerfile.prod -t ${registry}/hemall:${tag} .
    
    # 推送到仓库
    if [ "$registry" != "local" ]; then
        log "推送镜像到仓库: ${registry}/hemall:${tag}"
        docker push ${registry}/hemall:${tag}
    fi
    
    log "镜像构建完成"
}

# 部署到 Kubernetes
deploy_to_k8s() {
    local tag=$1
    local namespace=$2
    local values_file=$3
    
    log "部署到 Kubernetes 命名空间: ${namespace}"
    
    # 创建命名空间
    kubectl create namespace $namespace || true
    
    # 部署应用
    helm upgrade --install hemall ./charts/hemal \
        --namespace $namespace \
        --set image.tag=$tag \
        --values $values_file \
        --timeout 10m0s
    
    log "等待部署完成..."
    kubectl rollout status deployment/hemall-api -n $namespace --timeout=300s
    
    log "部署完成"
}

# 运行健康检查
run_health_check() {
    local namespace=$1
    local service_name=$2
    
    log "运行健康检查..."
    
    # 检查 Pod 状态
    kubectl get pods -n $namespace
    
    # 检查服务状态
    kubectl get svc -n $namespace
    
    # 检查部署状态
    kubectl get deployments -n $namespace
    
    log "健康检查完成"
}

# 主部署函数
main() {
    local environment=${1:-production}
    local tag=${2:-latest}
    local registry=${3:-your-registry.com}
    
    log "开始 Hemall ${environment} 环境部署"
    log "镜像标签: ${tag}"
    log "镜像仓库: ${registry}"
    
    # 检查前提条件
    check_prerequisites
    
    # 构建镜像
    build_image $tag $registry
    
    # 根据环境选择 values 文件
    local values_file="values.yaml"
    if [ "$environment" == "staging" ]; then
        values_file="values.staging.yaml"
    elif [ "$environment" == "production" ]; then
        values_file="values.production.yaml"
    elif [ "$environment" == "mall-sxueji" ]; then
        values_file="values.mall-sxueji.yaml"
    fi
    
    # 部署到 Kubernetes
    deploy_to_k8s $tag $environment $values_file
    
    # 运行健康检查
    run_health_check $environment "hemall-api"
    
    log "Hemall ${environment} 环境部署成功完成!"
}

# 显示帮助信息
show_help() {
    echo "用法: $0 [环境] [标签] [仓库]"
    echo ""
    echo "参数:"
    echo "  环境    部署环境 (staging|production，默认: production)"
    echo "  标签    Docker 镜像标签 (默认: latest)"
    echo "  仓库    Docker 镜像仓库 (默认: your-registry.com)"
    echo ""
    echo "示例:"
    echo "  $0 production v1.0.0 registry.example.com"
    echo "  $0 staging latest"
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