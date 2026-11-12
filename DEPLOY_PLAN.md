# Hemall `fix/launch-p0` 上线计划

目标分支：`fix/launch-p0`
代码验证版本：`864511d6065ce0b35acebd8b232cdc6d9ca20951`
说明：该 commit 之后仅新增 P0_FIX_REPORT/DEPLOY_PLAN/SUMMARY 等文档；若在最新
HEAD 重新构建，按 `scripts/build-release.sh` 生成的 `release/<HEAD>/` 为准即可。
镜像（本机已构建，未 push）：
- `registry.sxueji.com/hemall/backend:sha-864511d6065ce0b35acebd8b232cdc6d9ca20951`
- `registry.sxueji.com/hemall/frontend:sha-864511d6065ce0b35acebd8b232cdc6d9ca20951`

发布清单已生成：`release/864511d6065ce0b35acebd8b232cdc6d9ca20951/`
（`docker-compose.release.yml`、`manifest.json`、`release.env`）。
回滚基线：后端 `0.6.18-prod`、前端 `0.5.10-prod`，
compose：`ops/release/docker-compose.rollback-baseline.yml`。

预计中断：仅重建 `api`/`web`，约 1–3 分钟；`db`/`redis`/`caddy` 不重启。
若走蓝绿脚本可缩到秒级，但需先确认脚本与当前 compose 组合。

## 0. 上线前（用户确认）

- [ ] 确认本次上线窗口与回滚负责人。
- [ ] 确认真实支付开关：未提供商户资料前保持
      `HEMALL_PAYMENT_GATEWAY_PROVIDER=manual`、`online_payment_available=false`。
- [ ] 确认酒类是否开售（默认关闭；开启需设 `HEMALL_ALCOHOL_SALES_ENABLED=true`）。
- [ ] 确认合规文本已替换 `content/compliance/manifest.json` 指向的占位文档；
      未替换时站点保持“未正式营业”。
- [ ] 确认 P0-08 下架清单（`evidence/p0-08-downlist.csv`，236 条）与补资料计划。
- [ ] 确认商家通知接收人（webhook/邮件）与短信 provider（可继续 disabled）。
- [ ] 确认 `/metrics` 访问方式（内网或 Bearer token）与 Prometheus/Alertmanager 接收人。

## 1. 备份（只读，必须先成功）

```bash
/data/soffy/ops/hemall/hemall-backup.sh
tail -5 /data/soffy/backups/hemall/backup.log
# 要求：日志出现 backup ok；/data 可用空间 > 10G
```

备份脚本已由 soffy crontab 每日 03:30 CST 执行，保留 7 日 + 4 周。

## 2. 推送镜像（需 registry 权限）

```bash
docker push registry.sxueji.com/hemall/backend:sha-864511d6065ce0b35acebd8b232cdc6d9ca20951
docker push registry.sxueji.com/hemall/frontend:sha-864511d6065ce0b35acebd8b232cdc6d9ca20951
# 或：cd /data/soffy/wt/hemal-p0fix && scripts/build-release.sh --push
```

## 3. 切换 api/web（部署目录执行）

```bash
cd /data/soffy/wt/hemal-deploy
export SHA=864511d6065ce0b35acebd8b232cdc6d9ca20951
export RELEASE=/data/soffy/wt/hemal-p0fix/release/$SHA

docker inspect hemal-api-1 --format '{{.Config.Image}} {{.Image}}'
docker inspect hemal-web-1 --format '{{.Config.Image}} {{.Image}}'

docker compose -p hemal \
  -f docker-compose.mall-old.yml \
  -f docker-compose.deploy.yml \
  -f "$RELEASE/docker-compose.release.yml" \
  up -d api web
```

首次启动会执行幂等 DDL（P0 表、索引、配置表）；只增量建表/建索引，不删列。

## 4. 上线核验

```bash
docker inspect hemal-api-1 --format '{{.Config.Image}}'
docker inspect hemal-web-1  --format '{{.Config.Image}}'
docker exec hemal-api-1 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5).status)"
curl -fsS http://127.0.0.1:8092/health/live
curl -fsS http://127.0.0.1:8092/ | head
```

人工冒烟（生产只读优先；下单/退款用测试账号 + manual）：
1. 首页/类目/详情可打开；
2. 首页与详情“加购”均进入购物车；
3. manual 下单成功，超时订单自动取消且批次 reservation 归零；
4. 支付确认后订单为 `confirmed`，发货波次可拾取；
5. manual 退款审核后为 `refunding`，人工确认后才 `succeeded`；
6. 退货确认回补到原订单行批次，重复确认不双补；
7. 合规页/页脚、未正式营业横幅、酒类默认不可见。

## 5. 回滚

镜像回滚优先；数据库不自动降级（本次只做增量 DDL，旧镜像可忽略新表）。

```bash
cd /data/soffy/wt/hemal-deploy
docker compose -p hemal \
  -f docker-compose.mall-old.yml \
  -f docker-compose.deploy.yml \
  -f /data/soffy/wt/hemal-p0fix/ops/release/docker-compose.rollback-baseline.yml \
  up -d api web

docker inspect hemal-api-1 --format '{{.Config.Image}}'   # 0.6.18-prod
docker inspect hemal-web-1  --format '{{.Config.Image}}'   # 0.5.10-prod
curl -fsS http://127.0.0.1:8092/health/live
```

回滚已演练：临时环境 `sha-864511d… → 0.6.18-prod → sha-864511d…` 均 healthy
（`/data/soffy/wt/hemal-p0fix-run/evidence/rollback-rehearsal.txt`）。

## 6. 失败处理

- 镜像拉取失败：停止，不重建；检查 registry 权限/digest。
- `/health/ready` 失败：立即镜像回滚，保留 `docker logs hemal-api-1`。
- 加购/下单异常：先回滚镜像；不要直接改生产库。
- 备份失败或超过 36 小时未成功：禁止上线，先修复备份并重新恢复核验。
- 只有镜像回滚仍无法恢复且用户明确批准，才从 `/data/soffy/backups/hemall/`
  恢复数据库；恢复前保存故障现场。
