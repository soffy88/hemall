#!/bin/bash
# Hemall - mall.sxueji.com 一键部署脚本
# 使用 Docker Compose + Caddy + Cloudflare

set -e

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log_info() {
    echo -e "${BLUE}[$(date '+%Y-%m-%d %H:%M:%S')] INFO:${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] WARN:${NC} $1"
}

log_error() {
    echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1"
}

# Check prerequisites
check_prerequisites() {
    log_info "检查部署前置条件..."
    
    local missing=()
    
    # Check Docker
    if ! command -v docker &> /dev/null; then
        missing+=("Docker")
    elif ! docker info &> /dev/null; then
        log_error "Docker 未运行，请先启动 Docker"
        exit 1
    fi
    
    # Check Docker Compose
    if ! docker compose version &> /dev/null; then
        missing+=("Docker Compose")
    fi
    
    if [ ${#missing[@]} -gt 0 ]; then
        log_error "缺少必要工具: ${missing[*]}"
        exit 1
    fi
    
    log "✓ 所有必要工具已安装"
}

# Validate environment file
validate_env() {
    log_info "验证环境变量配置..."
    
    if [ ! -f ".env.mall-sxueji" ]; then
        log_error "找不到 .env.mall-sxueji 文件"
        exit 1
    fi
    
    # Check if .env exists and has values filled
    if [ -f ".env" ]; then
        # Check critical values
        if grep -q "<YOUR_POSTGRES_PASSWORD_HERE>" .env; then
            log_warn "数据库密码未配置"
        fi
        
        if grep -q "<GENERATE_STRONG_RANDOM_STRING_32_BYTES_MIN>" .env; then
            log_warn "JWT_SECRET 未配置"
        fi
        
        if grep -q "<YOUR_CLOUDFLARE_EMAIL>" .env; then
            log_warn "Cloudflare 邮箱未配置"
        fi
        
        if grep -q "<CLOUDFLARE_API_TOKEN_WITH_DNS_EDIT_PERMISSIONS>" .env; then
            log_warn "Cloudflare API Token 未配置"
        fi
        
        log "✓ 环境变量文件存在"
    else
        log_warn ".env 文件不存在，将使用 .env.mall-sxueji 作为模板"
        cp .env.mall-sxueji .env
        log_warn "请编辑 .env 文件并填入真实值"
    fi
}

# Prepare directories
prepare_directories() {
    log_info "准备目录结构..."
    
    mkdir -p var/omodul_output
    mkdir -p logs
    mkdir -p caddy_data
    mkdir -p caddy_config
    
    log "✓ 目录创建完成"
}

# Build and deploy
deploy() {
    log_info "开始部署 Hemall to mall.sxueji.com..."
    
    # Check for image configuration
    if docker build -f Dockerfile.prod --help &> /dev/null; then
        log_info "构建 Docker 镜像..."
        
        read -p "是否本地构建镜像？(y/n, 默认: n): " BUILD_LOCAL
        BUILD_LOCAL=${BUILD_LOCAL:-n}
        
        if [[ "$BUILD_LOCAL" =~ ^[Yy]$ ]]; then
            docker build -f Dockerfile.prod -t registry.sxueji.com/hemall/backend:latest .
            log "✓ 镜像构建完成"
        else
            log_info "跳过本地构建，请确保镜像已推送到 registry.sxueji.com/hemall/backend:latest"
        fi
    fi
    
    # Start services
    log_info "启动 Docker Compose 服务..."
    docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml up -d
    
    # Wait for services to be ready
    log_info "等待服务启动 (约 60-90 秒)..."
    sleep 10
    
    # Show service status
    docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml ps
    
    # Wait for health checks
    log_info "等待 PostgreSQL 健康检查..."
    for i in {1..30}; do
        if docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml exec db pg_isready -U hemall &> /dev/null; then
            log "✓ PostgreSQL 就绪"
            break
        fi
        sleep 3
    done
    
    log_info "等待 Redis 健康检查..."
    for i in {1..20}; do
        if docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml exec redis redis-cli ping &> /dev/null; then
            log "✓ Redis 就绪"
            break
        fi
        sleep 2
    done
    
    # Final status
    log "=========================================="
    log " 部署完成！"
    log "=========================================="
    log ""
    log_info "访问地址:"
    log "  🌐 API: https://mall.sxueji.com"
    log "  📊 Grafana: http://localhost:3000 (如需外部访问)"
    log "  🔍 Jaeger: http://localhost:16686 (如需外部访问)"
    log ""
    log_info "查看日志:"
    log "  docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml logs -f api"
    log ""
    log_info "更新部署:"
    log "  docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml pull"
    log "  docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml up -d"
    log ""
}

# Show help
show_help() {
    cat << EOF
用法: $0 [选项]

选项:
  --help, -h      显示此帮助信息
  --stop          停止所有服务
  --restart       重启所有服务
  --logs          查看所有服务日志
  --cleanup       清理所有数据卷 (⚠️ 危险操作)

示例:
  $0                  # 交互式部署
  $0 --stop           # 停止服务
  $0 --restart        # 重启服务
  $0 --logs           # 查看日志

前置配置:
  1. 复制 .env.mall-sxueji 为 .env 并填入配置值
  2. 确保域名 mall.sxueji.com 已解析到服务器 IP
  3. 确保 Cloudflare API Token 有 DNS 编辑权限

EOF
}

# Main function
main() {
    case "${1:-}" in
        --help|-h)
            show_help
            exit 0
            ;;
        --stop)
            log_info "停止所有服务..."
            docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml down
            log "服务已停止"
            ;;
        --restart)
            log_info "重启所有服务..."
            docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml restart
            log "服务已重启"
            ;;
        --logs)
            docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml logs -f
            ;;
        --cleanup)
            log_warn "⚠️  此操作将删除所有数据卷！"
            read -p "确认继续？(输入 yes): " CONFIRM
            if [[ "$CONFIRM" == "yes" ]]; then
                docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml down -v
                log "所有数据已清理"
            else
                log "操作已取消"
            fi
            ;;
        "")
            check_prerequisites
            validate_env
            prepare_directories
            deploy
            ;;
        *)
            log_error "未知选项: $1"
            show_help
            exit 1
            ;;
    esac
}

main "$@"
