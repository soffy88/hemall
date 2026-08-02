# 📊 可观测性运维手册

## 目录

1. [架构概览](#架构概览)
2. [日志系统 (structlog)](#日志系统)
3. [指标系统 (Prometheus)](#指标系统)
4. [链路追踪 (OpenTelemetry + Jaeger)](#链路追踪)
5. [Grafana 仪表板](#grafana-仪表板)
6. [告警配置指南](#告警配置指南)
7. [故障排查手册](#故障排查手册)

---

## 架构概览

```
                    ┌──────────────┐
                    │   Client     │
                    └──────┬───────┘
                           │ HTTP
                    ┌──────▼───────┐
                    │  Metrics MW  │ ← Prometheus 采集 /metrics
                    ├──────────────┤
                    │  Logging MW  │ → structlog JSON → stdout
                    ├──────────────┤
                    │ Audit MW     │ → audit_log (PostgreSQL)
                    ├──────────────┤
                    │  FastAPI     │
                    │  Routes      │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────▼────┐ ┌────▼─────┐ ┌────▼────┐
        │PostgreSQL│ │  Redis   │ │ Jaeger  │
        └──────────┘ └──────────┘ └─────────┘
              │
        ┌─────▼────┐
        │Prometheus│ ← 每 15s 抓取 /metrics
        └─────┬────┘
              │
        ┌─────▼────┐
        │ Grafana  │
        └──────────┘
```

---

## 日志系统

### 格式

所有日志以 JSON 格式输出到 stdout，便于日志收集系统解析:

```json
{
  "trace_id": "e6cb0707-2dbc-4c22-ab23-c9bdbbe3e216",
  "method": "POST",
  "path": "/checkout/complete_checkout",
  "client_ip": "192.168.1.100",
  "status_code": 200,
  "latency_ms": 42.15,
  "event": "request_completed",
  "level": "info",
  "timestamp": "2024-01-15T10:30:45.123456Z"
}
```

### 日志级别

| 级别 | 用途 | 何时关注 |
|------|------|----------|
| `debug` | 详细调试信息 | 本地开发 |
| `info` | 请求完成、启动/关停 | 日常监控 |
| `warning` | 降级、重试、非致命错误 | 需要关注 |
| `error` | 未处理异常、业务失败 | 立即处理 |
| `critical` | 系统不可用 | 紧急响应 |

### TraceID 传播

每个请求自动分配唯一 TraceID:
1. 客户端可传入 `x-trace-id` 请求头
2. 服务端在响应头中返回 `x-trace-id`
3. 日志中每条记录都包含 `trace_id`

```bash
# 按 TraceID 搜索日志
kubectl logs deploy/hemall-backend | jq 'select(.trace_id == "e6cb0707-...")'
```

### 审计日志

写操作 (POST/PUT/PATCH/DELETE) 自动记录到 `audit_log` 表:

```sql
-- 查看最近 1 小时的操作
SELECT actor_id, action, resource_type, response_status, created_at
FROM audit_log
WHERE created_at > NOW() - INTERVAL '1 hour'
ORDER BY created_at DESC
LIMIT 50;

-- 按用户查看操作历史
SELECT action, resource_type, resource_id, request_path, duration_ms
FROM audit_log
WHERE actor_id = 'user-123'
ORDER BY created_at DESC;

-- 查看失败操作
SELECT actor_id, action, request_path, response_status, metadata
FROM audit_log
WHERE response_status >= 400
ORDER BY created_at DESC;
```

---

## 指标系统

### Prometheus 指标

| 指标名 | 类型 | 说明 | 标签 |
|--------|------|------|------|
| `http_requests_total` | Counter | HTTP 请求总数 | method, path, status |
| `http_request_duration_seconds` | Histogram | 请求延迟 | method, path |
| `http_requests_in_progress` | Gauge | 当前活跃请求 | method |
| `db_pool_size` | Gauge | 连接池大小 | pool |
| `db_pool_used` | Gauge | 已使用连接数 | pool |
| `redis_status` | Gauge | Redis 状态 (1=up) | - |

### 常用 PromQL 查询

```promql
# QPS (每秒请求数)
rate(http_requests_total[5m])

# 错误率 (> 400 状态码占比)
sum(rate(http_requests_total{status=~"4..|5.."}[5m]))
/
sum(rate(http_requests_total[5m]))

# P99 延迟
histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))

# P50 延迟
histogram_quantile(0.50, rate(http_request_duration_seconds_bucket[5m]))

# 活跃请求数
sum(http_requests_in_progress)

# DB 连接池使用率
db_pool_used / db_pool_size * 100
```

### 路径规范化

指标中的路径自动规范化，避免高基数问题:

```
/products/550e8400-e29b-41d4-a716-446655440000 → /products/{id}
/orders/123/items/456 → /orders/{id}/items/{id}
```

---

## 链路追踪

### Jaeger 使用

1. 打开 http://localhost:16686
2. 在 Search 页面选择服务: `hemall-backend`
3. 可按 Tags 搜索: `http.status_code=500`
4. 查看单个 Trace 的完整调用链

### OTLP 配置

生产环境将追踪数据发送到 OTLP 端点:

```env
OTEL_TRACES_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4317
```

### 自定义 Span

在业务代码中创建自定义 Span:

```python
from app.observability.tracing import get_tracer

tracer = get_tracer(__name__)

async def process_order(order_id: str):
    with tracer.start_as_current_span("process_order") as span:
        span.set_attribute("order_id", order_id)
        # 业务逻辑...
```

---

## Grafana 仪表板

### 预置面板

| 面板 | 说明 | 数据源 |
|------|------|--------|
| QPS | 每秒请求数 (按状态码着色) | Prometheus |
| 请求延迟 | P50/P95/P99 延迟曲线 | Prometheus |
| 错误率 | 5xx/4xx 占比 | Prometheus |
| 活跃请求 | 当前处理中的请求数 | Prometheus |
| DB 连接池 | 大小 vs 已用 | Prometheus |
| Redis 状态 | 连接状态 | Prometheus |

### 自定义面板

添加新的 Grafana 面板:
1. 打开 Grafana → Dashboard → Add Panel
2. 选择 Prometheus 数据源
3. 输入 PromQL 查询
4. 选择可视化类型 (Time series / Stat / Gauge)

---

## 告警配置指南

### Prometheus Alerting Rules

在 `prometheus.yml` 中添加告警规则:

```yaml
groups:
  - name: hemall_alerts
    rules:
      # 高错误率
      - alert: HighErrorRate
        expr: |
          sum(rate(http_requests_total{status=~"5.."}[5m]))
          / sum(rate(http_requests_total[5m])) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "错误率超过 5%"
          description: "5xx 错误率: {{ $value | humanizePercentage }}"

      # 高延迟
      - alert: HighLatency
        expr: |
          histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m])) > 2
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "P99 延迟超过 2 秒"

      # DB 连接池耗尽
      - alert: DBPoolExhausted
        expr: db_pool_used / db_pool_size > 0.9
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "DB 连接池使用率超过 90%"

      # Redis 不可用
      - alert: RedisDown
        expr: redis_status == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Redis 不可用"
```

### 告警通知

配合 Alertmanager 发送告警:
- 企业微信 / 钉钉 Webhook
- PagerDuty (值班轮转)
- 邮件

---

## 故障排查手册

### 服务无法启动

```bash
# 1. 检查日志
docker compose logs hemall-backend

# 2. 检查健康检查
curl http://localhost:8000/health/live

# 3. 常见原因
# - 端口被占用: 修改 docker-compose.yml 中的端口映射
# - DB 连接失败: 检查 PG_DSN 和 PostgreSQL 状态
# - 缺少环境变量: 检查 .env 文件
```

### 接口响应慢

```bash
# 1. 在 Grafana 查看延迟面板
# 2. 在 Jaeger 搜索慢请求: 按 duration > 1s 过滤
# 3. 检查 DB 查询: 连接池是否耗尽?
curl http://localhost:8000/health/metrics/summary

# 4. 检查 Redis 延迟
docker exec hemall-redis redis-cli ping
```

### 支付异常

```bash
# 1. 查看审计日志
SELECT * FROM audit_log
WHERE action LIKE '%payment%'
ORDER BY created_at DESC LIMIT 20;

# 2. 查看支付会话
SELECT id, order_id, status, provider, amount, created_at
FROM payment_session
ORDER BY created_at DESC LIMIT 20;

# 3. 检查回调
# 微信: POST /payments/wechat/notify
# 支付宝: POST /payments/alipay/notify
# 查看回调日志:
docker compose logs hemall-backend | grep "notify"
```

### 限流触发

```bash
# 限流返回 429 Too Many Requests
# 查看日志中被限流的请求:
docker compose logs hemall-backend | jq 'select(.status_code == 429)'

# 调整限流规则: 修改 app/security/rate_limit.py
# 或针对登录: app/auth.py 中的 @limiter.limit("5/minute")
```

### 数据加密问题

```bash
# 检查 ENCRYPTION_KEY 是否配置
echo $ENCRYPTION_KEY

# 验证加密/解密
python -c "
from app.security.crypto import encrypt_field, decrypt_field
enc = encrypt_field('test-data')
print(f'Encrypted: {enc}')
print(f'Decrypted: {decrypt_field(enc)}')
"
```
