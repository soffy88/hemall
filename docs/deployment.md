# 🚀 Hemall 部署指南

## 目录

1. [快速开始 (本地开发)](#快速开始)
2. [Docker Compose 部署](#docker-compose-部署)
3. [环境变量配置](#环境变量配置)
4. [支付对接](#支付对接)
5. [生产部署注意事项](#生产部署注意事项)

---

## 快速开始

### 前置要求

- Python 3.12+
- PostgreSQL 16+
- Redis 7+
- [uv](https://github.com/astral-sh/uv) (推荐) 或 pip

### 安装依赖

```bash
# 克隆项目 (含 3O 元素库)
git clone <repo-url> hemal
cd hemal

# 使用 uv 创建虚拟环境并安装全部依赖
uv sync --all-extras

# 或使用 pip
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,observability]"
```

### 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填写数据库连接等配置
```

### 启动服务

```bash
# 开发模式 (热重载)
uvicorn app.main:app --reload --port 8000

# 生产模式
python -m app.main
```

### 访问

- API 文档: http://localhost:8000/docs
- 健康检查: http://localhost:8000/health/live
- Prometheus 指标: http://localhost:8000/metrics

---

## Docker Compose 部署

一键启动完整开发环境 (含所有可观测性工具):

```bash
docker compose up -d
```

### 服务列表

| 服务 | 端口 | 说明 |
|------|------|------|
| hemall-backend | 8000 | FastAPI 应用 |
| postgres | 5432 | PostgreSQL 16 |
| redis | 6379 | Redis 7 |
| jaeger | 16686 (UI) | 链路追踪 |
| prometheus | 9090 | 指标采集 |
| grafana | 3000 | 可视化仪表板 |

### 查看日志

```bash
# 全部服务
docker compose logs -f

# 只看后端
docker compose logs -f hemall-backend

# 查看启动健康检查
docker compose ps
```

### 停止

```bash
docker compose down

# 清理数据卷 (⚠️ 会删除数据库数据)
docker compose down -v
```

### Grafana 仪表板

1. 打开 http://localhost:3000
2. 默认账号: admin / admin (首次登录需改密码)
3. 进入 Dashboards → "Hemall Overview"
4. 仪表板已自动配置，包含:
   - QPS (每秒请求数)
   - 请求延迟 (P50/P95/P99)
   - 错误率
   - 活跃请求数
   - DB 连接池状态

---

## 环境变量配置

### 核心配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PG_DSN` | `postgresql://hemall:hemall@localhost:5432/hemall` | PostgreSQL 连接串 |
| `PG_POOL_MIN` | `2` | 连接池最小连接数 |
| `PG_POOL_MAX` | `20` | 连接池最大连接数 |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接 URL |
| `JWT_SECRET` | `dev-secret-change-me` | JWT 签名密钥 (⚠️ 生产必须修改) |
| `JWT_EXPIRES_MINUTES` | `1440` | JWT 有效期 (分钟) |
| `CORS_ORIGINS` | `http://localhost:3000` | 允许的 CORS 来源 |

### 可观测性配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OTEL_TRACES_EXPORTER` | `console` | 追踪导出器 (console/otlp) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP 端点 (Jaeger) |

### 安全配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ENCRYPTION_KEY` | (自动生成) | Fernet 加密密钥 (⚠️ 生产必须配置) |

生成密钥:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 支付配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HEMALL_PAYMENT_GATEWAY_PROVIDER` | `wechat` 或 `stripe` | 生产唯一启用的真实支付网关 |
| `HEMALL_WECHAT_PAY_*` | 按网关要求设置 | 微信支付商户、APIv3、私钥、平台证书及回调配置 |
| `HEMALL_STRIPE_SECRET_KEY` | 必填（Stripe） | Stripe 私钥 |
| `HEMALL_STRIPE_WEBHOOK_SECRET` | 必填（Stripe） | Stripe webhook 签名密钥 |
| `HEMALL_STRIPE_NOTIFY_URL` | 按部署设置 | Stripe 支付回跳/通知配置 |

---

## 支付对接

### 微信支付

1. 在[微信商户平台](https://pay.weixin.qq.com)注册并获取商户号
2. 配置 APIv3 密钥
3. 下载商户 API 证书
4. 设置回调 URL: `https://yourdomain.com/payments/wechat/notify`
5. 配置环境变量:
   ```env
   WECHAT_PAY_MCH_ID=your_mch_id
   WECHAT_PAY_API_KEY=your_api_key
   WECHAT_PAY_CERT_SERIAL=your_cert_serial
   WECHAT_PAY_NOTIFY_URL=https://yourdomain.com/payments/wechat/notify
   ```

### 支付宝（生产环境暂不支持）

支付宝回调当前在 production 环境明确拒绝结算，且没有接入经过验签的真实支付宝网关。生产配置仅允许 `wechat` 或 `stripe`。不要把支付宝标记为已可用支付方式；需要支持时，必须先实现并测试真实下单、回调验签、金额/币种关联和退款闭环。

### 本地手动模式

`manual` 仅供本地开发和测试。生产环境启动校验会拒绝 `manual`、未知 provider，以及缺少必要凭据的真实网关。

---

## 生产部署注意事项

### 安全检查清单

- [ ] 设置 `HEMALL_JWT_SECRET` 为强随机字符串 (≥32 字节)
- [ ] 配置 `ENCRYPTION_KEY` (使用 Fernet 生成)
- [ ] 配置 `HEMALL_PRIVILEGED_STAFF_IDS`，只列出确需退款/Agent 执行权限的 app_user ID
- [ ] 配置真实 `HEMALL_PAYMENT_GATEWAY_PROVIDER=wechat|stripe` 及对应凭据；生产禁止 `manual`
- [ ] 设置 `HEMALL_CORS_ORIGINS` 为 JSON 数组形式的实际前端域名
- [ ] Helm 部署必须设置 `secrets.existingSecret` 指向外部 Secret；Chart 不再生成不完整的生产 Secret
- [ ] 若启用 Elasticsearch，必须提供外部 HTTPS 地址、认证凭据与 CA；Chart 拒绝未验证安全配置的内置 Elasticsearch
- [ ] PostgreSQL 使用 SSL 连接
- [ ] Redis 设置密码 + TLS
- [ ] Docker 使用非 root 用户 (Dockerfile 已配置)
- [ ] 开启 HTTPS (通过 Nginx/Caddy 反代)

### 性能调优

```env
# 连接池: 根据并发量调整
PG_POOL_MIN=10
PG_POOL_MAX=50

# Gunicorn workers (Docker 中): CPU 核心数 × 2 + 1
# Dockerfile 默认: 4 workers, 2 threads
```

### 备份策略

```bash
# PostgreSQL 备份
docker exec hemal-postgres pg_dump -U hemall hemall > backup_$(date +%Y%m%d).sql

# 恢复
cat backup_20240101.sql | docker exec -i hemal-postgres psql -U hemall hemall
```

### 日志收集

生产环境建议将 structlog JSON 输出接入日志收集系统:

- **ELK Stack**: Filebeat → Logstash → Elasticsearch → Kibana
- **Loki**: Promtail → Loki → Grafana
- **云服务商**: 阿里云 SLS / AWS CloudWatch Logs

Docker 中可通过 logging driver 直接发送:

```yaml
# docker-compose.yml
services:
  hemall-backend:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```
