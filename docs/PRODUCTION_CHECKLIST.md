# Hemall 生产切换清单 (PRODUCTION_CHECKLIST)

切 `HEMALL_ENVIRONMENT=production` 之前逐项核对。**带 ✅ 的项有自动化测试兜底**，
其余是人工确认项。

代码侧的强制约束在 `app/config.py::Settings.validate_production_security()`——
启动时逐条校验，任何一条不满足就抛 `RuntimeError` 拒绝启动 (fail-closed)。
本文档是它的人类可读版本 + 它管不到的运维项。

---

## 0. 快速自检

```bash
# 本地/CI 先跑一遍生产配置的合法性 (不起服务)
HEMALL_ENVIRONMENT=production HEMALL_PG_DSN=... python -c \
  "from app.config import Settings; Settings().validate_production_security(); print('ok')"

# 部署后
curl -fsS https://<host>/health/ready | jq .    # 必须 status=healthy
```

---

## 1. 密钥文件 (推荐 Docker secrets，禁止裸环境变量)

所有 `*_FILE` 变量指向容器内密钥文件路径；**显式环境变量优先于 `*_FILE`**。
`docker-compose.mall-sxueji.yml` 已按此装配 (secrets 挂载到 `/run/secrets/*`)。

| 环境变量 | Settings 字段 | 挂载路径示例 | 必填 |
|---|---|---|---|
| `HEMALL_JWT_SECRET_FILE` | `jwt_secret` | `/run/secrets/jwt_secret` | ✅ |
| `HEMALL_WEBHOOK_SECRET_FILE` | `webhook_secret` | `/run/secrets/webhook_secret` | ✅ |
| `HEMALL_HARDWARE_SECRET_FILE` | `hardware_secret` | `/run/secrets/hardware_secret` | ✅ |
| `ENCRYPTION_KEY_FILE` | `encryption_key` | `/run/secrets/encryption_key` | ✅ |
| `HEMALL_PG_PASSWORD_FILE` | `pg_password` | `/run/secrets/pg_password` | 建议 |
| `HEMALL_PG_DSN_FILE` | `pg_dsn` | `/run/secrets/pg_dsn` | 二选一 |
| `HEMALL_REDIS_URL_FILE` | `redis_url` | `/run/secrets/redis_url` | 可选 |
| `HEMALL_WECHAT_PAY_PRIVATE_KEY_FILE` | `wechat_pay_private_key_path` | `/run/secrets/wechat_private_key` | wechat 必填 |
| `HEMALL_WECHAT_PAY_API_V3_KEY_FILE` | `wechat_pay_api_v3_key` | `/run/secrets/wechat_api_v3_key` | wechat 必填 |
| `HEMALL_WECHAT_PAY_PLATFORM_CERT_FILE` | `wechat_pay_platform_cert_path` | `/run/secrets/wechat_platform_cert` | wechat 必填 |
| `HEMALL_WECHAT_MP_APP_SECRET_FILE` | `wechat_mp_app_secret` | `/run/secrets/wechat_mp_app_secret` | 小程序登录必填 |
| `HEMALL_STRIPE_SECRET_KEY_FILE` | `stripe_secret_key` | `/run/secrets/stripe_secret_key` | stripe 必填 |
| `HEMALL_STRIPE_WEBHOOK_SECRET_FILE` | `stripe_webhook_secret` | `/run/secrets/stripe_webhook_secret` | stripe 必填 |
| `HEMALL_ALIPAY_PRIVATE_KEY_FILE` | `alipay_private_key_path` | `/run/secrets/alipay_private_key` | alipay 必填 |
| `HEMALL_ALIPAY_PUBLIC_KEY_FILE` | `alipay_public_key_path` | `/run/secrets/alipay_public_key` | alipay 必填 |

生成密钥：

```bash
# HMAC 类 (JWT / webhook / hardware) 必须 ≥32 字节
openssl rand -base64 48                       # → jwt_secret / webhook_secret / hardware_secret
# Fernet (PII 加密) — 一旦上线必须固定, 换 key = 历史密文永久不可解
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> ⚠️ `ENCRYPTION_KEY` 每次重启换新会让已加密的顾客 PII 永久无法解密。
> 首次生成后立刻入库备份，生产环境**禁止**留空。

✅ 覆盖测试：`tests/test_db_roles_and_secret_files.py`

---

## 2. 基础必填变量

| 环境变量 | 说明 | 生产要求 |
|---|---|---|
| `HEMALL_ENVIRONMENT` | `production` 才会触发安全基线校验 | ✅ `production` |
| `HEMALL_PG_DSN` | 生产库 DSN | ✅ 不能是开发默认 `postgres:test` |
| `HEMALL_PG_SSLMODE` | 连接**非本机/非容器内网** PG 时 | ✅ `require`/`verify-ca`/`verify-full` |
| `HEMALL_REDIS_URL` | Redis | ✅ |
| `HEMALL_JWT_SECRET` | JWT HMAC 密钥 | ✅ ≥32 字节、非默认值 |
| `HEMALL_WEBHOOK_SECRET` | 支付/抖音回调 HMAC | ✅ ≥32 字节、非默认值 |
| `HEMALL_HARDWARE_SECRET` | IoT 边桥 Bearer | ✅ ≥32 字节、非默认值 |
| `ENCRYPTION_KEY` | Fernet PII 加密 | ✅ 有效 Fernet base64 |
| `HEMALL_CORS_ORIGINS` | JSON 数组 | ✅ 禁止 `*` |
| `HEMALL_PRIVILEGED_STAFF_IDS` | 逗号分隔 app_user ID | ✅ 至少一个 |
| `HEMALL_SUPPORT_STAFF_IDS` | 可读后台 PII 的 support 角色 | 可选 |

**sslmode 默认升级**：`effective_pg_dsn` 在目标是外部主机且没显式设
`HEMALL_PG_SSLMODE` 时，自动把 `prefer` 升成 `require`。显式设置
(`HEMALL_PG_SSLMODE=disable` 或 DSN 里写 `sslmode=`) 会被尊重——但生产校验
会**拒绝**非 `require*` 的外部连接。容器内网主机名 (`db`/`postgres`/
`*.internal` 等) 不强制 TLS。

✅ 覆盖测试：`tests/test_db_roles_and_secret_files.py`

---

## 3. 管理员 TOTP 两步验证 (Task 2)

production 下**管理员和 `HEMALL_PRIVILEGED_STAFF_IDS` 中的账号必须已绑定
TOTP**，否则进不了 `/admin`——这是硬拦截，不是提示。

上线步骤：

1. 首次引导管理员：`HEMALL_ALLOW_PUBLIC_ADMIN_REGISTRATION=true` 启动一次，
   创建 `app_user`，然后**立刻关回 false**。
2. 用该管理员登录 → `/login` 会要求第二步 → 跳 `/login/totp-setup`。
3. 绑定页显示 otpauth URI + 二维码 + 10 个一次性恢复码，**恢复码当场存档**
   （哈希入库，丢了只能重置绑定）。
4. 逐个给 `HEMALL_PRIVILEGED_STAFF_IDS` 里的账号重复第 2–3 步。
5. 确认 `/admin` 可用后，把 `HEMALL_ALLOW_PUBLIC_ADMIN_REGISTRATION` 设为 false。

⚠️ 防重放：同一 30 秒时间步的验证码只能用一次；连续输错会触发限流。
⚠️ 员工离职时：回收 `HEMALL_PRIVILEGED_STAFF_IDS` 里的 ID，并清空该
`app_user` 的 `totp_secret` / `totp_recovery_codes_hash`。

✅ 覆盖测试：`tests/test_admin_totp.py`（22 例）、`web/e2e/totp.spec.ts`

---

## 4. 支付网关 (Task 3)

`HEMALL_PAYMENT_GATEWAY_PROVIDER` 必须选真实网关；**production 禁止 `manual`**。
密钥缺失时 `build_payment_gateway` 会诚实回退 manual，而 production 校验会
因此拒绝启动——不会出现"以为接了微信、实际是人工确认"。

### 4.1 微信支付 v3

| 变量 | 说明 |
|---|---|
| `HEMALL_PAYMENT_GATEWAY_PROVIDER` | `wechat` |
| `HEMALL_WECHAT_PAY_APPID` | 公众号/小程序 AppID |
| `HEMALL_WECHAT_PAY_MCHID` | 商户号 mchid |
| `HEMALL_WECHAT_PAY_SERIAL_NO` | 商户 API 证书序列号 |
| `HEMALL_WECHAT_PAY_PRIVATE_KEY_FILE` | apiclient_key.pem 路径 |
| `HEMALL_WECHAT_PAY_API_V3_KEY_FILE` | APIv3 密钥 (32 字节) |
| `HEMALL_WECHAT_PAY_PLATFORM_CERT_FILE` | 平台证书公钥路径 |
| `HEMALL_WECHAT_PAY_NOTIFY_URL` | **回调地址**，如 `https://mall.sxueji.com/api/payments/wechat/notify` |
| `HEMALL_WECHAT_PLATFORM_CERT_STORE` | 轮换落盘路径，默认 `var/wechat_platform_cert.json` |
| `HEMALL_WECHAT_MP_APP_ID` | 小程序 AppID（微信登录 jscode2session 用；小程序 JSAPI 支付时 `HEMALL_WECHAT_PAY_APPID` 也应填它） |
| `HEMALL_WECHAT_MP_APP_SECRET_FILE` | 小程序 AppSecret 文件（微信登录用；未配置时 wechat-login/bind 返回 503） |

平台证书由轮换引擎每 12h 拉 `/v3/certificates` 热更新并写 store 文件，重启后
继续用——不需要人工换证书。

### 4.2 支付宝 (RSA2)

| 变量 | 说明 |
|---|---|
| `HEMALL_PAYMENT_GATEWAY_PROVIDER` | `alipay` |
| `HEMALL_ALIPAY_APP_ID` | 应用 APPID |
| `HEMALL_ALIPAY_PRIVATE_KEY_FILE` | 应用私钥路径 |
| `HEMALL_ALIPAY_PUBLIC_KEY_FILE` | 支付宝公钥路径 |
| `HEMALL_ALIPAY_NOTIFY_URL` | 回调地址 |

### 4.3 Stripe

| 变量 | 说明 |
|---|---|
| `HEMALL_PAYMENT_GATEWAY_PROVIDER` | `stripe` |
| `HEMALL_STRIPE_SECRET_KEY_FILE` | sk_live_… |
| `HEMALL_STRIPE_WEBHOOK_SECRET_FILE` | whsec_… |
| `HEMALL_STRIPE_NOTIFY_URL` | 回调地址 |

### 4.4 回调地址清单

三家的**回调地址必须先在商户后台配好**，否则验签拿不到正确签名。地址形如：

```
https://<host>/api/payments/wechat/notify     # 微信 Native 支付结果
https://<host>/api/payments/alipay/notify     # 支付宝异步通知
https://<host>/api/payments/stripe/webhook    # Stripe webhook
https://<host>/api/refunds/gateway/notify     # 退款结果回调
```

回调一律验签 + 校验金额 + 校验商户号 + 幂等；伪造签名返回 400。

### 4.5 遗留开关（生产禁止）

`WECHAT_PAY_MCH_ID` / `ALIPAY_APP_ID` 这两个旧 SDK 开关一旦设置会**直接拒绝
启动**：它们的回调验签/查单/退款分支全是 `NotImplementedError`。生产收款只走
上面的 `HEMALL_PAYMENT_GATEWAY_PROVIDER`。

✅ 覆盖测试：`tests/test_ext_payment_gateways.py`（42 例，测试内自生成 RSA
密钥与证书模拟平台签名，伪造签名/过期时间戳均被拒）

---

## 5. 售后/退款 (Task 4)

- 顾客在 `/shop/account` 订单页提交售后申请，支持部分退款。
- 后台 `/admin/aftersales` 处理：审核通过 → 退款中 → 已退款/失败（失败可重试）。
- **累计退款额不得超过实付**，并发两次退款只有一笔成功，退货时回补库存，
  全程写 `refund_audit_log`。
- 退款结果经 `/api/refunds/gateway/notify` 回调，验签 + 幂等。

✅ 覆盖测试：`tests/test_refund_state_machine.py`（41 例）、
`web/e2e/aftersales.spec.ts`

---

## 6. 监控与告警

| 变量 / 文件 | 说明 |
|---|---|
| `HEMALL_ALERT_WEBHOOK_URL` | 告警收件人（企微/钉钉/Slack/飞书），compose 里必填，缺失即拒绝启动 |
| `prometheus.rules.yml` | 必须被 Prometheus 加载（rule_files 是 glob，缺文件不报错→0 条告警） |
| `GRAFANA_ADMIN_PASSWORD` | Grafana 管理口令 |

### 6.1 链路追踪 (OTEL)

| 变量 | 说明 |
|---|---|
| `OTEL_TRACES_EXPORTER` | `otlp` / `console` / `none`。**未设置时 production 默认 `none`** |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 选 `otlp` 时必填，形如 `http://jaeger:4317` |

端点缺失或写成 `jaeger:4317`（缺 scheme）时导出被**禁用**并只记一条 warning，
不会在后台线程无限重试刷日志。埋点本身不受影响。没部署 jaeger 就别设
`OTEL_TRACES_EXPORTER=otlp`。

✅ 覆盖测试：`tests/test_observability_tracing.py`

---

## 7. 健康检查与自愈 (2026-10-15 事故修复)

- `GET /health/live` —— 进程存活，永远 200。
- `GET /health/ready` —— **就绪探针**。连接池缺失时返 503 `degraded`，
  响应里会带 `background recovery retrying`。
- 容器 healthcheck / K8s readinessProbe 一律指向 `/health/ready`。
  连测失败即重启该 worker。
- 启动时 PG 不可达**不再**让 worker 永久 503：`app/db_recovery.py` 会按指数
  退避 (1s→30s 封顶) 后台重试建池，请求打过来还会立即唤醒一次重试；恢复后
  自动补齐支付对账/eventsourcing/analytics/AI/推荐/ext_oservi 装配。
- `web` 容器 CMD 用 `HOSTNAME=0.0.0.0 exec node server.js`（Docker 注入的
  `HOSTNAME` 会覆盖镜像层的 `ENV`，shell 就地赋值才压得住），健康检查在
  127.0.0.1 失败时兜底探测容器 IP。

✅ 覆盖测试：`tests/test_db_pool_recovery.py`（16 例，含真 PG 断开→恢复端到端）、
`tests/test_prod_incident_deploy_artifacts.py`（10 例）

---

## 8. 数据库最小权限

迁移与运行时分离，详见 `docker-compose.mall-sxueji.yml`：

```bash
# 一次性：以 owner 身份跑 DDL
docker compose -f docker-compose.mall-sxueji.yml run --rm migrate
# 一次性：建/授权 hemall_runtime（仅 DML，无 DDL）
docker compose -f docker-compose.mall-sxueji.yml run --rm grant-runtime
```

运行时 `api` 用 `hemall_runtime` 连接（连接时 `SET ROLE`），该角色：
可以 `SELECT/INSERT/UPDATE/DELETE`，**不能** `CREATE/ALTER/DROP`。
schema 变更只能重新跑 `migrate`。

✅ 覆盖测试：`tests/test_db_roles_and_secret_files.py`（真 PG 上验证 runtime
角色执行 `CREATE TABLE` 被拒）

---

## 9. 密钥扫描

- `.gitleaks.toml` —— 规则与放行清单。
- `.github/workflows/ci.yml` —— `gitleaks` job，全历史扫描（`fetch-depth: 0`），
  是其它 job 的前置依赖。
- `.pre-commit-config.yaml` —— gitleaks pre-commit 钩子。

本地跑：`gitleaks detect --config .gitleaks.toml --source . --verbose`
放行清单只覆盖测试代码与明显的占位符，不放宽任何生产源码。

---

## 10. 前端构建

```bash
cd web
npm ci
npx tsc --noEmit
npm run lint
npx vitest run
NEXT_PUBLIC_API_URL=https://<host> npm run build    # 缺这个变量构建直接失败
```

`NEXT_PUBLIC_*` 在构建期内联进 bundle，运行时再设无效。

---

## 11. 上线前最后确认

- [ ] `.env` / secrets 不在仓库里（`git ls-files | grep -E '\.env$'` 为空）
- [ ] `HEMALL_ALLOW_PUBLIC_ADMIN_REGISTRATION=false`
- [ ] 所有 `PRIVILEGED_STAFF_IDS` 账号已绑定 TOTP
- [ ] `ENCRYPTION_KEY` 已备份到密钥管理系统
- [ ] 三家支付回调地址已在商户后台登记
- [ ] 告警 webhook 已配置且能收到测试告警
- [ ] `curl /health/ready` → `healthy`
- [ ] `gitleaks detect` → no leaks found
