# Hemall P0 发布与回滚手册

本手册对应 `fix/launch-p0`。它只描述如何构建、切换和回滚；真实支付、短信、
第三方通知与合规文本仍须由用户确认后另行配置。

## 1. 发布产物

- 后端镜像：`registry.sxueji.com/hemall/backend:sha-<完整 40 位 git sha>`
- 前端镜像：`registry.sxueji.com/hemall/frontend:sha-<完整 40 位 git sha>`
- 本地清单：`release/<完整 git sha>/release.env`
- Compose override：`release/<完整 git sha>/docker-compose.release.yml`
- 回滚基线：`ops/release/docker-compose.rollback-baseline.yml`

镜像标签必须带完整 commit；禁止使用 `latest`、短 sha 或“同名重推”覆盖已发布标签。
两个 Dockerfile 都写入 `org.opencontainers.image.revision`，发布前脚本会核对。

## 2. 上线前检查

1. 工作树必须干净，且 `git rev-parse HEAD` 是待发布 commit。
2. `/data` 可用空间大于 10G；生产备份最近一次成功且已做恢复核验。
3. 用户确认真实支付开关、合规文本、酒类销售、配送范围/时窗、通知接收人、
   管理员 TOTP、metrics token 和 DB runtime 角色 SQL。
4. 记录当前容器镜像和容器 ID，供回滚取证：

   ```bash
   docker inspect hemal-api-1 --format '{{.Config.Image}} {{.Image}}'
   docker inspect hemal-web-1 --format '{{.Config.Image}} {{.Image}}'
   ```

## 3. 构建并推送

在干净的 `fix/launch-p0` 工作树执行：

```bash
docker login registry.sxueji.com
scripts/build-release.sh --push
```

脚本会构建前后端、核对镜像内的 commit label，并生成精确的 Compose override。
未带 `--push` 时只构建本机镜像，不会修改或重启任何容器。

## 4. 上线步骤

以下命令在部署目录执行，示例变量必须替换为构建产物中的实际值：

```bash
export HEMALL_RELEASE_SHA=<完整 git sha>
export HEMALL_RELEASE_DIR=/data/soffy/releases/hemall/$HEMALL_RELEASE_SHA

# 0) 再次做只读备份；失败即停止上线
bash /data/soffy/wt/hemal-p0fix/scripts/ops/hemall-backup.sh

# 1) 只更新 api/web；不重启 db/redis/caddy，不改线上 Caddyfile
docker compose -p hemal \
  -f docker-compose.mall-old.yml \
  -f docker-compose.deploy.yml \
  -f "$HEMALL_RELEASE_DIR/docker-compose.release.yml" \
  up -d api web

# 2) 核对实际运行的镜像
docker inspect hemal-api-1 --format '{{.Config.Image}}'
docker inspect hemal-web-1 --format '{{.Config.Image}}'

# 3) 健康与冒烟
docker exec hemal-api-1 python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)"
curl -fsS http://127.0.0.1:8092/health/live
curl -fsS http://127.0.0.1:8092/ | head
```

预计中断：`api` 与 `web` 容器重建期间约 1–3 分钟；`db`、`redis`、`caddy`
不重启。若使用蓝绿脚本，则按该脚本的切换流程执行，中断可缩短到秒级。

上线后人工检查：首页、类目、详情、加购、manual 下单、超时关单、manual 退款
确认与原批次库存回补。支付回调必须使用沙盒或用户提供的测试商户配置，禁止真实扣款。

## 5. 回滚步骤

镜像回滚优先，数据库不自动降级：

```bash
docker compose -p hemal \
  -f docker-compose.mall-old.yml \
  -f docker-compose.deploy.yml \
  -f /data/soffy/wt/hemal-p0fix/ops/release/docker-compose.rollback-baseline.yml \
  up -d api web

docker inspect hemal-api-1 --format '{{.Config.Image}}'
docker inspect hemal-web-1 --format '{{.Config.Image}}'
docker exec hemal-api-1 python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)"
curl -fsS http://127.0.0.1:8092/health/live
```

回滚目标固定为后端 `0.6.18-prod`、前端 `0.5.10-prod`。本次迁移只做增量建表/
索引，旧镜像可忽略新增表；不要为回滚执行破坏性 SQL。只有镜像回滚仍无法恢复，
且用户明确批准后，才从 `/data/soffy/backups/` 恢复数据库；恢复前先保存故障现场。

## 6. 失败处理

- 镜像拉取失败：不要继续重建；确认 registry 权限和 digest 后重试。
- `/health/ready` 失败：立即执行镜像回滚，保留 `docker logs hemal-api-1`。
- 加购/下单异常：回滚镜像；不要改生产库数据。
- 备份失败或超过 36 小时未成功：禁止上线，先修复备份并重新恢复核验。
