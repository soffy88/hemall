# PostgreSQL 备份容器 (audit P2-4)
FROM postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea

# 安装 cron、aws-cli (用于 S3 上传)、gzip
RUN apk add --no-cache \
    dcron \
    aws-cli \
    gzip \
    bash

# 复制备份脚本
COPY backup.sh /backup/backup.sh
COPY restore.sh /backup/restore.sh
RUN chmod +x /backup/backup.sh /backup/restore.sh

# 创建备份目录
RUN mkdir -p /backups /var/log

# 创建 cron 日志文件
RUN touch /var/log/backup.log

# 入口点
ENTRYPOINT ["/backup/backup.sh"]