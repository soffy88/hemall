# Hemall 安全基线（P0-13）

## 管理员 TOTP

- `HEMALL_REQUIRE_ADMIN_TOTP=true`（默认）：生产环境下 `admin` 与
  `HEMALL_PRIVILEGED_STAFF_IDS` 中的账号必须先绑定 TOTP 才能获得完整后台会话。
- 首次登录会进入 `/login/totp-setup`，用 Google Authenticator / 1Password 等
  TOTP App 扫码或手输密钥；绑定成功后必须保存一次性恢复码。
- 管理员列表 `/admin/users` 新增“两步验证”列，可核对是否已绑定。
- 不建议关闭。仅在完成书面风险评估并安排补偿控制后，才可临时设置
  `HEMALL_REQUIRE_ADMIN_TOTP=false`。

## `/metrics`

- Caddy 仅允许 `private_ranges` 访问 `/metrics*`，公网返回 404。
- 后端增加第二道门：设置 `HEMALL_METRICS_ACCESS_TOKEN` 后必须携带
  `X-Metrics-Token` 或 `Authorization: Bearer <token>`；未设置 token 时只放行
  回环/内网来源。
- Prometheus 抓取配置应通过 secret 注入该 header，不要将 token 写进仓库。

## 数据库最小权限

- 迁移/DDL 使用 owner；API 运行时使用 `hemall_runtime`（NOLOGIN、无 DDL）。
- SQL 模板：`scripts/sql/hemall-runtime-role.sql`，只生成，本次未执行。
- 上线前由 DBA 替换 `<OWNER_ROLE>`、执行 SQL，并运行文件末尾的只读验证查询。
