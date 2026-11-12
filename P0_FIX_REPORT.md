# Hemall 上线 P0 修复报告

分支：`fix/launch-p0`（代码基线 9de8dde；最新 HEAD 见文末）
工作树：`/data/soffy/wt/hemal-p0fix`
原则：未重启/未重建任何线上容器（hemal-api-1/web-1/caddy-1/db-1/redis-1/ts-bridge），
未改线上 compose/Caddyfile，未写生产数据库；未推送镜像、未下单真实支付。

## 总表

| P0 | 状态 | 主要提交 | 证据 |
| --- | --- | --- | --- |
| P0-01 合规主体与法定页面 | 已准备待上线（需用户提供文本） | 9fabb17 | 九页路由 + 页脚 + 未正式营业横幅；`content/compliance/manifest.json` |
| P0-02 真实支付未接通 | 需用户提供商户号/密钥/AppID | 034950a | 未配置时前台不宣称在线支付，manual 可用 |
| P0-03 支付宝生产回调不可用 | 已准备待上线（需支付宝密钥/AppID） | 034950a, c3d0ebd | 验签 + 幂等 + 金额校验；provider 回调放行 CSRF |
| P0-04 支付成功→履约链断裂 | 已修复 | 346d664, 864511d | 状态统一 paid/confirmed；确认端点 jsonb 地址 500 已修 |
| P0-05 超时关单与库存释放 | 已修复 | 346d664 | 定时引擎 + 可配超时；临时库冒烟自动关单释放 |
| P0-06 客户端可任意定运费 | 已修复 | 346d664 | 服务端规则计价；前端不再传价格 |
| P0-07 首页加购是假动作 | 已修复 | e4944ea | 首页调用真实购物车 API；冒烟加购成功 |
| P0-08 食品数据/质检门禁 | 已准备待上线（需用户确认下架清单） | 95c7a3e | 前台门禁 + 入库校验 + 后台待补资料标记；只读下架清单 236 条 |
| P0-09 酒类未成年人保护 | 已修复（默认隐藏，待用户决定开启） | bbbd71c | 默认 `alcohol_sales_enabled=false`；年龄签名 Cookie + 提示 |
| P0-10 履约/商家通知未配置 | 已准备待上线（需接收人/凭据） | 9fabb17 | provider 抽象（webhook/邮件）、后台未配置醒目提示、配送范围/时窗可配 |
| P0-11 退款/售后不可信 | 已修复 | 3ab35a6, d5c632b | manual 必须人工确认；按原订单行真实批次回补；重复收货不双补 |
| P0-12 注册与账号合规不足 | 已准备待上线（需短信 provider/协议文本） | 9fabb17 | 双勾选 + 同意版本落库；短信默认关闭 |
| P0-13 安全基线未启用 | 已准备待上线（需运维执行） | cdba37d | Next.js 16.4.5（critical 1→0）；/metrics 内网/Token；TOTP 开关；最小权限 SQL |
| P0-14 备份和恢复缺失 | 已修复 | 7a8c238, 82bdf21 | 生产 pg_dump + Redis 快照；恢复核验 93 表；每日 03:30 cron |
| P0-15 监控告警缺失 | 已准备待上线（需接收人） | 82bdf21 | Prometheus 抓取 + 10 条核心告警 + Alertmanager 占位；promtool 通过 |
| P0-16 发布不可复现、回滚未验证 | 已修复/已演练 | baf550e, 5830872, 864511d | sha 镜像、一键脚本、RELEASE.md；临时环境 新镜像→0.6.18-prod→新镜像 均 healthy |

## 本次（续跑）额外修复

1. **P0-11 部分退款被“进行中”守卫误挡**（d5c632b）
   - 原守卫把 `refunding` 也算进行中；manual 退款审核后停在 `refunding` 等线下确认，
     导致同一订单无法再发起其余部分退款。改为只拦 `submitted/approved`，
     累计额度仍把 `refunding` 计入。
2. **P0-11 退货回补 ON CONFLICT 失效**（d5c632b）
   - `stock_movement(idempotency_key)` 是部分唯一索引，`ON CONFLICT (idempotency_key)`
     不指定谓词时 PostgreSQL 报 “no unique or exclusion constraint”。
     补 `WHERE idempotency_key IS NOT NULL`（兼容线上已有索引，无需改生产 schema）。
3. **P0-04 确认订单 500**（864511d，冒烟发现）
   - asyncpg 未注册 jsonb codec 时读回 JSON 文本，`OrderSnapshot` 要求 dict，
     `/orders/{id}/confirm` 抛 ValidationError。service 层解析两列地址并加回归测试。
4. **镜像数据目录属主**（5830872）
   - `.dockerignore` 排除 `var/omodul_output/`，旧镜像由 root master 运行时创建，
     降权 worker 写文件 `PermissionError`（冒烟 `create_cart` 500）。
     Dockerfile.prod 预建目录并授权，entrypoint 增加 `mkdir -p`。
5. **测试隔离**（1e263fa, cbb3033）
   - 新增 P0 用例使共享 Redis 令牌桶在同 IP 下被耗尽，尾部 wechat 用例偶发 429；
     conftest 让每个测试 app 使用进程内桶（Redis 限流另有专项测试）。
   - 零库存预订用例补合规批次与合规标题，适配 P0-08 门禁。
6. **备份 cron 稳定路径**：cron 从 worktree 路径改指
   `/data/soffy/ops/hemall/hemall-backup.sh`，避免工作树移除后备份静默失效。

## 验证记录（2026-11-08）

- 后端全量：`2205 passed, 36 skipped`（含真实 PostgreSQL/Redis 临时容器）。
- 前端：`tsc --noEmit` 通过；`vitest` 135 passed；`next build` 通过（Next.js 16.4.5）。
- 静态检查：`ruff check app tests`、`ruff format --check app tests`、mypy
  （orders/inventory/payments/security）全部通过。
- npm audit：critical 1 → 0；仍有 1 high / 10 moderate，均为依赖链问题
  （如 undici、katex），非本次 critical 门禁范围。
- 冒烟（临时库 + 重建镜像 `sha-864511d…`）：首页/合规页、类目、详情、注册同意、
  真实加购、manual 下单、超时自动关单并释放 reservation、支付确认、
  退货回补原批次、线下确认退款、重复收货不双补 —— `ALL_E2E_OK`。
  证据：`/data/soffy/wt/hemal-p0fix-run/evidence/smoke-e2e-final.txt`。
- 发布产物：`scripts/build-release.sh` 在 HEAD 成功生成
  `release/864511d…/{docker-compose.release.yml,manifest.json,release.env}`；
  两个镜像均带完整 sha 标签（仅本机，未 push）。
- 回滚演练：临时容器 `sha-864511d… → 0.6.18-prod → sha-864511d…`，
  三次均 healthy、`/health/ready` 200、`/store/products` 200。
  证据：`evidence/rollback-rehearsal.txt`。

## 需用户提供/确认

- 支付宝 AppID/私钥/公钥、微信支付商户号/证书、Stripe 密钥（P0-02/03）。
- 隐私政策、用户协议、退换货（含生鲜）、资质、发票、投诉等正式文本（P0-01/12）。
- 短信服务商与模板（P0-12）；商家通知接收人与 webhook/邮箱（P0-10）。
- 是否开启酒类销售、年龄门槛与提示文案（P0-09，默认关闭）。
- P0-08 下架清单（236 条）确认；食品资料补齐后再上架。
- 管理员 TOTP 绑定；DB runtime 最小权限 SQL 执行；Prometheus/Alertmanager 接收人。
- 上线切换窗口与真实支付开关（DEPLOY_PLAN.md）。

## 未处理/限制

- 未执行任何生产数据修改；P0-08 待补资料与 P0-13 权限 SQL 只生成未执行。
- 镜像未 push 到 registry；上线前需在有 registry 权限的机器执行
  `scripts/build-release.sh --push`（或在当前机器 `docker push` 两个 sha 标签）。
- 本轮构建首次因构建容器访问 github.com:443 超时失败两次，重试后成功；
  若 CI/发布机网络受限，需配置代理或预拉 3O 依赖。

## 提交清单（fix/launch-p0）

```
7a8c238 ops(backup)             346d664 fix(p0): paid/timeout/shipping
034950a fix(payments alipay)    c3d0ebd fix(payments callbacks)
3ab35a6 fix(refunds)            e4944ea fix(storefront cart)
95c7a3e fix(catalog gate)       bbbd71c fix(alcohol)
9fabb17 fix(compliance/notify)  cdba37d fix(security)
82bdf21 ops(monitoring)         baf550e build(release)
859b42f style(ruff)             d5c632b fix(refunds follow-up)
cbb3033 test(preorder)          1e263fa test(rate-limit isolation)
5830872 build(var dirs)         864511d fix(orders jsonb)
cbcc864+ docs(P0 report/deploy/summary)
```

镜像与回滚演练对应代码 commit `864511d`；其后仅文档变化。
