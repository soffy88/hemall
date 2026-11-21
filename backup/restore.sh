#!/bin/sh
# PostgreSQL 恢复脚本 (audit P2-4)
#
# 用法:
#   1. 列出可用备份:
#        ./restore.sh list
#   2. 恢复最新备份:
#        ./restore.sh latest
#   3. 恢复指定备份文件:
#        ./restore.sh /backups/hemall_prod_20261013_030000.sql.gz
#   4. 从 S3 恢复:
#        ./restore.sh s3://bucket/backup_file.sql.gz
#
# 环境变量:
#   POSTGRES_HOST       数据库主机 (默认: db)
#   POSTGRES_PORT       数据库端口 (默认: 5432)
#   POSTGRES_USER       数据库用户 (默认: postgres)
#   POSTGRES_PASSWORD   数据库密码 (必填)
#   POSTGRES_DB         目标数据库名 (默认: postgres)
#   BACKUP_DIR          备份存储目录 (默认: /backups)
#   S3_ENDPOINT         S3 兼容存储端点 (可选)
#   S3_BUCKET           S3 桶名 (可选)
#   S3_ACCESS_KEY       S3 访问密钥 (可选)
#   S3_SECRET_KEY       S3 密钥 (可选)
#   S3_REGION           S3 区域 (可选，默认: us-east-1)
#   RESTORE_TARGET_DB   恢复目标库名，留空则覆盖 POSTGRES_DB (危险！)
#   FORCE               设为 "true" 跳过确认提示 (危险！)

set -eu

POSTGRES_HOST="${POSTGRES_HOST:-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD 必须设置}"
POSTGRES_DB="${POSTGRES_DB:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
S3_ENDPOINT="${S3_ENDPOINT:-}"
S3_BUCKET="${S3_BUCKET:-}"
S3_ACCESS_KEY="${S3_ACCESS_KEY:-}"
S3_SECRET_KEY="${S3_SECRET_KEY:-}"
S3_REGION="${S3_REGION:-us-east-1}"
RESTORE_TARGET_DB="${RESTORE_TARGET_DB:-$POSTGRES_DB}"
FORCE="${FORCE:-false}"

log() { echo "[restore] $(date -Is) $*"; }
die() { echo "[restore] ERROR: $*" >&2; exit 1; }

list_backups() {
    log "本地可用备份 (${BACKUP_DIR}):"
    ls -lh "${BACKUP_DIR}"/*.sql* "${BACKUP_DIR}"/*.dump 2>/dev/null | awk '{print "  " $9 " (" $5 ")"}' || echo "  (无本地备份)"
    if [ -n "$S3_ENDPOINT" ] && [ -n "$S3_BUCKET" ] && [ -n "$S3_ACCESS_KEY" ] && [ -n "$S3_SECRET_KEY" ]; then
        log "S3 可用备份 (s3://${S3_BUCKET}):"
        AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY" \
        AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY" \
        AWS_DEFAULT_REGION="$S3_REGION" \
        aws --endpoint-url "$S3_ENDPOINT" s3 ls "s3://${S3_BUCKET}/" 2>/dev/null | awk '{print "  s3://" "'"${S3_BUCKET}"'/" $4 " (" $3 ")"}' || echo "  (无 S3 备份或访问失败)"
    fi
}

download_from_s3() {
    local s3_path="$1"
    local local_file="$2"
    log "从 S3 下载: ${s3_path} -> ${local_file}"
    AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY" \
    AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY" \
    AWS_DEFAULT_REGION="$S3_REGION" \
    aws --endpoint-url "$S3_ENDPOINT" s3 cp "${s3_path}" "${local_file}" \
        || die "S3 下载失败"
}

# 查找最新备份文件
find_latest_backup() {
    local latest
    latest=$(ls -t "${BACKUP_DIR}"/*.dump 2>/dev/null | head -1)
    if [ -z "$latest" ]; then
        latest=$(ls -t "${BACKUP_DIR}"/*.sql.gz 2>/dev/null | head -1)
    fi
    if [ -z "$latest" ]; then
        latest=$(ls -t "${BACKUP_DIR}"/*.sql 2>/dev/null | head -1)
    fi
    echo "$latest"
}

# 执行恢复
do_restore() {
    local backup_file="$1"
    local is_dump=false

    case "$backup_file" in
        *.dump)
            is_dump=true
            ;;
        *.sql|*.sql.gz)
            is_dump=false
            ;;
        *)
            die "未知备份格式: $backup_file (支持 .dump, .sql, .sql.gz)"
            ;;
    esac

    # 确认提示
    if [ "$FORCE" != "true" ]; then
        echo ""
        echo "⚠️  即将恢复数据库 ${RESTORE_TARGET_DB} 从 ${backup_file}"
        echo "    这将覆盖目标数据库中的所有数据！"
        echo ""
        read -p "确认继续？输入 'YES' 确认: " confirm
        [ "$confirm" = "YES" ] || die "用户取消恢复"
    fi

    log "开始恢复数据库 ${RESTORE_TARGET_DB}..."

    # 终止现有连接
    log "终止数据库 ${RESTORE_TARGET_DB} 的现有连接..."
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres \
        -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${RESTORE_TARGET_DB}' AND pid <> pg_backend_pid();" \
        || log "终止连接警告 (可能数据库不存在或无连接)"

    # 删除并重建数据库
    log "重建数据库 ${RESTORE_TARGET_DB}..."
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres \
        -c "DROP DATABASE IF EXISTS \"${RESTORE_TARGET_DB}\";" \
        -c "CREATE DATABASE \"${RESTORE_TARGET_DB}\" OWNER \"${POSTGRES_USER}\";" \
        || die "数据库重建失败"

    if [ "$is_dump" = true ]; then
        # 使用 pg_restore 恢复 custom format
        log "使用 pg_restore 恢复..."
        PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
            -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" \
            -d "$RESTORE_TARGET_DB" \
            --verbose --clean --if-exists --no-owner --no-privileges \
            "$backup_file" \
            || die "pg_restore 失败"
    else
        # 使用 psql 恢复 SQL 文件
        if echo "$backup_file" | grep -q '\.gz$'; then
            log "解压并恢复 SQL..."
            gunzip -c "$backup_file" | PGPASSWORD="$POSTGRES_PASSWORD" psql \
                -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" \
                -d "$RESTORE_TARGET_DB" \
                -v ON_ERROR_STOP=1 \
                || die "SQL 恢复失败"
        else
            log "恢复 SQL..."
            PGPASSWORD="$POSTGRES_PASSWORD" psql \
                -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" \
                -d "$RESTORE_TARGET_DB" \
                -v ON_ERROR_STOP=1 \
                -f "$backup_file" \
                || die "SQL 恢复失败"
        fi
    fi

    log "恢复完成！"
}

main() {
    case "${1:-}" in
        list|ls)
            list_backups
            ;;
        latest)
            local latest
            latest=$(find_latest_backup)
            [ -n "$latest" ] || die "未找到本地备份文件"
            log "最新备份: $latest"
            do_restore "$latest"
            ;;
        s3://*)
            [ -n "$S3_ENDPOINT" ] && [ -n "$S3_BUCKET" ] && [ -n "$S3_ACCESS_KEY" ] && [ -n "$S3_SECRET_KEY" ] \
                || die "S3 配置不完整，无法从 S3 恢复"
            local filename
            filename=$(basename "$1")
            local local_file="${BACKUP_DIR}/${filename}"
            download_from_s3 "$1" "$local_file"
            do_restore "$local_file"
            ;;
        /*)
            [ -f "$1" ] || die "备份文件不存在: $1"
            do_restore "$1"
            ;;
        "")
            echo "用法: $0 {list|latest|<backup_file>|s3://bucket/file}"
            echo ""
            echo "示例:"
            echo "  $0 list                    # 列出所有可用备份"
            echo "  $0 latest                  # 恢复最新本地备份"
            echo "  $0 /backups/xxx.sql.gz     # 恢复指定本地备份"
            echo "  $0 s3://bucket/xxx.sql.gz  # 从 S3 恢复"
            echo ""
            echo "环境变量:"
            echo "  POSTGRES_PASSWORD (必填)   数据库密码"
            echo "  POSTGRES_HOST (默认: db)   数据库主机"
            echo "  POSTGRES_DB (默认: postgres) 目标数据库"
            echo "  RESTORE_TARGET_DB          恢复到指定库名 (默认同 POSTGRES_DB)"
            echo "  FORCE=true                 跳过确认提示 (危险)"
            exit 1
            ;;
        *)
            die "未知命令: $1"
            ;;
    esac
}

main "$@"