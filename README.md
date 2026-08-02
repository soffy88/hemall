# Hemall - 电商系统

Hemall 是一个现代化的电商平台，支持 AI 助手、实时推荐、多支付渠道等特性。

## 📋 项目结构

- `app/` - FastAPI 后端应用
- `web/` - Next.js 前端应用  
- `charts/hemal/` - Helm 部署图表
- `docs/` - 文档目录

## 🚀 部署指南

### 本地开发
```bash
# 安装依赖
uv sync --all-extras

# 启动服务
uvicorn app.main:app --reload --port 8000
```

### 生产部署

#### Docker Compose (小型环境)
```bash
docker compose -f docker-compose.prod.yml up -d
```

#### Kubernetes (推荐)
```bash
# 部署到 mall.sxueji.com
./scripts/deploy.sh mall-sxueji latest registry.sxueji.com

# 部署到其他环境
./scripts/deploy.sh production v1.0.0 your-registry.com
```

详细部署文档请参考：
- [通用部署指南](docs/deployment.md)
- [生产环境部署与性能优化](docs/production-deployment.md)  
- [mall.sxueji.com 专用部署指南](docs/deployment-mall-sxueji.md)

## 📊 可观测性

- **健康检查**: `/health/live`, `/health/ready`
- **指标**: `/metrics` (Prometheus 格式)
- **API 文档**: `/docs` (Swagger UI)

## 🔐 安全 (补天计划)

- **Webhook 验签装甲**: 抖音转化回调 `/growth/douyin_callback` 与支付回调
  (`/payments/wechat/notify`, `/payments/alipay/notify`) 在 ASGI 网关层强制
  HMAC-SHA256 验签，非法请求 403 丢弃，不触碰业务层。签名协议：
  ```
  X-Hemall-Signature: hex( HMAC_SHA256(HEMALL_WEBHOOK_SECRET, raw_body) )
  X-Hemall-Timestamp: Unix 秒 (可选，±300s 防重放)
  X-Hemall-Nonce:     一次性串 (可选，防重放去重)
  ```
  生产环境必须用 `HEMALL_WEBHOOK_SECRET` 覆盖默认值。
- **限流防刷**: 全部零登录公开端点 (ext 自助操作 + 整个 `/store/*` 商城面) 按
  IP + `X-Device-Id` 令牌桶限流 (默认 60 请求/分钟)，超限 429。
- **收款网关预留**: `ManualPaymentGateway` 已实装统一下单 (Prepay) 与退款
  (Refund) 的入参/出参契约，接入真实微信支付/Stripe 密钥时替换 provider 实现即可
  (环境变量约定 `HEMALL_WECHAT_MCH_ID` / `HEMALL_WECHAT_API_V3_KEY` /
  `HEMALL_STRIPE_SECRET_KEY`)。

## 🔧 环境配置

复制 `.env.example` 并根据环境修改：
```bash
cp .env.example .env
# 编辑 .env 文件
```

专用环境配置模板：
- `.env.mall-sxueji` - mall.sxueji.com 生产环境配置

## 🧪 测试

```bash
# 运行所有测试
pytest tests/

# 运行特定测试
pytest tests/test_auth.py
```
