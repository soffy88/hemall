# 🚀 Phase 0: 基础加固 - 任务看板

## ✅ 全部已完成

### Week 1: 可观测性 + CI/CD + Docker ✅
- [x] 结构化日志 (structlog + TraceID)
- [x] Prometheus 指标采集 (QPS/延迟/错误率/活跃请求/DB池/Redis)
- [x] OpenTelemetry 链路追踪 (Console/OTLP)
- [x] 健康检查端点 (/health/live, /health/ready, /health/metrics/summary)
- [x] CI/CD 流水线 (.github/workflows/ci.yml)
- [x] Dockerfile (多阶段构建，非 root)
- [x] docker-compose.yml (app + PG + Redis + Jaeger + Prometheus + Grafana)
- [x] Grafana 仪表板 (预配置)

### Week 2: 安全加固 + 支付 + 文档 ✅

#### 🔒 安全加固
- [x] 登录/注册限流 — `app/security/rate_limit.py` (slowapi, 按 IP/User 限流)
- [x] 敏感数据加密 — `app/security/crypto.py` (Fernet AES, encrypt/decrypt/mask)
- [x] 审计日志表 — `app/security/audit.py` (DDL + 中间件 + 装饰器)
- [x] Auth 端点限流 — `app/auth.py` (@limiter.limit("5/minute"))
- [x] 审计中间件集成 — `app/main.py` (自动记录所有 POST/PUT/PATCH/DELETE)

#### 💳 支付对接 MVP
- [x] 支付状态机 — `app/payments/models.py` (7 种状态, 完整转换矩阵)
- [x] 微信支付 SDK — `app/payments/wechat.py` (V3 API 骨架 + 沙箱降级)
- [x] 支付宝 SDK — `app/payments/alipay.py` (当面付骨架 + 沙箱降级)
- [x] 支付 API 路由 — `app/payments/router.py` (创建/查询/退款/回调)
- [x] 支付表结构 — payment_session DDL + 索引 (bootstrap.py)

#### 📝 文档
- [x] 部署指南 — `docs/deployment.md` (快速开始/Docker/环境变量/支付对接/生产注意)
- [x] 可观测性运维手册 — `docs/observability.md` (日志/指标/追踪/Grafana/告警/故障排查)

---

## 📊 测试统计

| 指标 | 数值 |
|------|------|
| 总测试数 | 115 |
| 通过 | 115 |
| 跳过 | 65 |
| 失败 | 0 |
| Week 2 新增测试 | 22 |

---

## 📁 文件清单

### Week 1 (可观测性)
```
新增 (12):
  .github/workflows/ci.yml
  app/middleware/__init__.py, logging.py, metrics.py
  app/observability/__init__.py, health.py, tracing.py
  Dockerfile, docker-compose.yml, prometheus.yml
  grafana/provisioning/datasources/prometheus.yml
  grafana/provisioning/dashboards/dashboards.yml, hemall-overview.json

修改 (4):
  pyproject.toml, app/main.py, app/bootstrap.py, app/deps.py
```

### Week 2 (安全 + 支付 + 文档)
```
新增 (9):
  app/security/__init__.py, rate_limit.py, crypto.py, audit.py
  app/payments/__init__.py, models.py, wechat.py, alipay.py, router.py
  tests/test_phase0_week2.py
  docs/deployment.md, docs/observability.md

修改 (4):
  pyproject.toml (+cryptography), app/main.py, app/bootstrap.py, app/auth.py
```

---

## 📡 API 端点总览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health/live` | GET | 存活探针 |
| `/health/ready` | GET | 就绪探针 (DB/Redis) |
| `/health/metrics/summary` | GET | 指标摘要 |
| `/metrics` | GET | Prometheus 指标 |
| `/auth/login` | POST | 登录 (限流 5/min) |
| `/payments/create` | POST | 创建支付会话 |
| `/payments/query/{id}` | GET | 查询支付状态 |
| `/payments/refund` | POST | 申请退款 |
| `/payments/wechat/notify` | POST | 微信支付回调 |
| `/payments/alipay/notify` | POST | 支付宝回调 |
| + 99 个商务 omodul 端点 | POST | 3O 范式商务逻辑 |

---

## 🔜 后续规划 (Phase 1+)

- [ ] 支付 SDK 生产实现 (替换沙箱)
- [ ] 限流后端切换为 Redis (当前内存态)
- [ ] WebSocket 实时推送 (订单状态变更)
- [ ] 商品搜索 (Elasticsearch)
- [ ] 移动端推送 (FCM/APNs)
- [ ] Kubernetes Helm Chart
