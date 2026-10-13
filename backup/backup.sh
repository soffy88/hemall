#!/bin/sh
# PostgreSQL 自动备份脚本 (audit P2-4)
#
# 环境变量:
#   POSTGRES_HOST       数据库主机 (默认: db)
#   POSTGRES_PORT       数据库端口 (默认: 5432)
#   POSTGRES_USER       数据库用户 (默认: postgres)
#   POSTGRES_PASSWORD   数据库密码 (必填)
#   POSTGRES_DB         数据库名 (默认: postgres)
#   BACKUP_DIR          备份存储目录 (默认: /backups)
#   BACKUP_SCHEDULE     cron 表达式 (默认: "0 3 * * *" 每天凌晨 3 点)
#   BACKUP_RETENTION_DAYS  保留天数 (默认: 30)
#   BACKUP_COMPRESS     是否压缩 (默认: true)
#   S3_ENDPOINT         S3 兼容存储端点 (可选，用于异地备份)
#   S3_BUCKET           S3 桶名 (可选)
#   S3_ACCESS_KEY       S3 访问密钥 (可选)
#   S3_SECRET_KEY       S3 密钥 (可选)
#   S3_REGION           S3 区域 (可选，默认: us-east-1)

set -eu

POSTGRES_HOST="${POSTGRES_HOST:-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD 必须设置}"
POSTGRES_DB="${POSTGRES_DB:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
BACKUP_COMPRESS="${BACKUP_COMPRESS:-true}"
S3_ENDPOINT="${S3_ENDPOINT:-}"
S3_BUCKET="${S3_BUCKET:-}"
S3_ACCESS_KEY="${S3_ACCESS_KEY:-}"
S3_SECRET_KEY="${S3_SECRET_KEY:-}"
S3_REGION="${S3_REGION:-us-east-1}"

mkdir -p "$BACKUP_DIR"

log() { echo "[backup] $(date -Is) $*"; }

# 执行单次备份
do_backup() {
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    local backup_file="${BACKUP_DIR}/${POSTGRES_DB}_${timestamp}.sql"

    log "开始备份数据库 ${POSTGRES_DB} 到 ${backup_file}"

    PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
        -h "$POSTGRES_HOST" \
        -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        --no-password \
        --verbose \
        --format=custom \
        --compress=9 \
        --file="${backup_file}.dump" \
        || { log "备份失败"; return 1; }

    # 同时生成纯 SQL 版本便于人工检查/恢复
    PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
        -h "$POSTGRES_HOST" \
        -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        --no-password \
        --format=plain \
        --file="${backup_file}" \
        || { log "SQL 导出失败"; return 1; }

    if [ "$BACKUP_COMPRESS" = "true" ]; then
        gzip "${backup_file}"
        backup_file="${backup_file}.gz"
        log "备份已压缩: ${backup_file}"
    fi

    log "备份完成: ${backup_file} + ${backup_file%.sql.gz}.dump"

    # 上传到 S3 (如配置)
    if [ -n "$S3_ENDPOINT" ] && [ -n "$S3_BUCKET" ] && [ -n "$S3_ACCESS_KEY" ] && [ -n "$S3_SECRET_KEY" ]; then
        log "上传备份到 S3..."
        AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY" \
        AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY" \
        AWS_DEFAULT_REGION="$S3_REGION" \
        aws --endpoint-url "$S3_ENDPOINT" s3 cp "${backup_file}" "s3://${S3_BUCKET}/$(basename "${backup_file}")" \
            && aws --endpoint-url "$S3_ENDPOINT" s3 cp "${backup_file%.sql.gz}.dump" "s3://${S3_BUCKET}/$(basename "${backup_file%.sql.gz}.dump")" \
            && log "S3 上传完成" \
            || log "S3 上传失败"
    fi
}

# 清理过期备份
cleanup_old_backups() {
    log "清理 ${BACKUP_RETENTION_DAYS} 天前的备份..."
    find "$BACKUP_DIR" -type f \( -name "*.sql" -o -name "*.sql.gz" -o -name "*.dump" \) -mtime "+${BACKUP_RETENTION_DAYS}" -delete
    log "清理完成"
}

# 主循环
main() {
    log "PostgreSQL 备份服务启动"
    log "目标: ${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
    log "存储: ${BACKUP_DIR}"
    log "保留: ${BACKUP_RETENTION_DAYS} 天"
    [ -n "$S3_ENDPOINT" ] && log "异地存储: s3://${S3_BUCKET} (${S3_ENDPOINT})"

    # 立即执行一次备份（容器启动时）
    do_backup
    cleanup_old_backups

    # 设置定时任务
    if [ -n "${BACKUP_SCHEDULE:-}" ]; then
        log "安装定时任务: ${BACKUP_SCHEDULE}"
        echo "${BACKUP_SCHEDULE} /backup/backup.sh >> /var/log/backup.log 2>&1" > /etc/crontabs/root
        crond -f -l 8 &
        CRON_PID=$!
        log "cron 守护进程已启动 (PID: $CRON_PID)"
        wait $CRON_PID
    else
        log "未设置 BACKUP_SCHEDULE，仅执行一次备份后退出"
    fi
}

# 兼容直接调用 /backup/backup.sh 做单次备份
if [ "${1:-}" = "once" ]; then
    do_backup
    cleanup_old_backups
    exit 0
fi

main