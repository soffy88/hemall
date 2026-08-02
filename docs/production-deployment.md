# Hemall 生产环境部署与性能优化指南

## 📋 概述

本文档详细说明如何将 Hemall 部署到生产环境，并进行性能优化以满足高并发、低延迟的要求。

## 🚀 部署架构

### Kubernetes 部署 (推荐)

```
Internet → LoadBalancer → Ingress Controller → Hemall Service → Pods (3+ instances)
                             ↓
                        Redis Cluster (缓存/会话)
                             ↓
                        PostgreSQL (主从复制)
                             ↓
                        Elasticsearch (集群)
                             ↓
                        Monitoring Stack (Prometheus/Grafana/Jaeger)
```

### Docker Compose 部署 (小型环境)

```
Internet → Nginx Proxy → Hemall Container
                              ↓
                        Redis Container
                              ↓
                        PostgreSQL Container
                              ↓
                        Elasticsearch Container
```

## 🔧 性能优化配置

### 1. 应用层优化

#### Gunicorn 配置 (`gunicorn.conf.py`)
```python
# Worker 进程数 = (CPU 核心数 * 2) + 1
workers = 4
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
max_requests = 1000
max_requests_jitter = 50
timeout = 30
keepalive = 5
preload_app = True  # 预加载应用减少内存占用
```

#### Uvicorn 配置
```python
# 在 main.py 中
uvicorn.run(
    "app.main:app",
    host="0.0.0.0",
    port=8000,
    workers=4,
    loop="uvloop",      # 更快的事件循环
    http="httptools",   # 更快的 HTTP 解析
    reload=False,       # 生产环境禁用自动重载
)
```

#### 连接池优化
```python
# 数据库连接池
DATABASE_POOL_SIZE = 20
DATABASE_MAX_OVERFLOW = 30

# Redis 连接池
REDIS_CONNECTION_POOL_SIZE = 20
```

### 2. 数据库优化

#### PostgreSQL 配置 (`postgresql.conf`)
```conf
# 内存相关
shared_buffers = 256MB          # 物理内存的 25%
effective_cache_size = 1GB      # OS 和 PG 缓存总和
work_mem = 4MB                  # 每个查询的工作内存
maintenance_work_mem = 64MB     # 维护操作内存

# 并发相关
max_connections = 200           # 最大连接数
superuser_reserved_connections = 3

# WAL 和检查点
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100
random_page_cost = 1.1          # SSD 环境调低
effective_io_concurrency = 200  # SSD 并发能力
```

#### 索引优化
```sql
-- 订单表常用查询索引
CREATE INDEX idx_orders_user_created ON orders(user_id, created_at DESC);
CREATE INDEX idx_orders_status_updated ON orders(status, updated_at DESC);

-- 库存表常用查询索引
CREATE INDEX idx_stock_product_warehouse ON stock_movement(product_id, warehouse_id);
CREATE INDEX idx_stock_reservation ON stock_movement(reservation_id) WHERE reservation_id IS NOT NULL;

-- 用户行为表索引
CREATE INDEX idx_behaviors_user_time ON user_behaviors(user_id, timestamp DESC);
CREATE INDEX idx_behaviors_product_time ON user_behaviors(product_id, timestamp DESC);
```

### 3. 缓存策略优化

#### Redis 配置优化
```conf
# 内存策略
maxmemory 2gb
maxmemory-policy allkeys-lru  # LRU 淘汰策略

# 持久化 (生产环境建议关闭 RDB/AOF)
save ""  # 禁用 RDB 快照
appendonly no  # 禁用 AOF

# 网络优化
tcp-keepalive 300
timeout 0
tcp-backlog 511
```

#### 应用缓存策略
```python
# 分级 TTL 策略
CACHE_TTL_MAP = {
    "hot_data": 300,      # 热点数据 5 分钟
    "warm_data": 1800,    # 温数据 30 分钟  
    "cold_data": 7200,    # 冷数据 2 小时
    "static_data": 86400, # 静态数据 24 小时
}

# 缓存预热策略
async def warmup_cache():
    """启动时预热关键缓存"""
    # 预加载热门商品
    hot_products = await get_hot_products(limit=100)
    for product in hot_products:
        await cache.set(f"product:{product.id}", product, ttl=300)
    
    # 预加载分类树
    category_tree = await get_category_tree()
    await cache.set("category_tree", category_tree, ttl=7200)
```

### 4. 搜索引擎优化 (Elasticsearch)

#### ES 集群配置
```yaml
# elasticsearch.yml
cluster.name: hemall-production
node.name: es-node-1
network.host: 0.0.0.0
http.port: 9200
discovery.type: single-node  # 单节点模式

# 性能调优
indices.fielddata.cache.size: 20%
indices.memory.index_buffer_size: 10%
```

#### 索引模板优化
```json
{
  "index_patterns": ["products-*"],
  "settings": {
    "number_of_shards": 3,
    "number_of_replicas": 1,
    "refresh_interval": "30s",
    "translog.durability": "async",
    "translog.sync_interval": "30s"
  },
  "mappings": {
    "properties": {
      "name": {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart"
      },
      "price": {"type": "integer"},
      "rating": {"type": "float"},
      "created_at": {"type": "date"}
    }
  }
}
```

## 🛡️ 安全加固

### 1. 网络安全
```yaml
# NetworkPolicy 示例
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: hemall-network-policy
spec:
  podSelector:
    matchLabels:
      app: hemall
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          name: ingress-nginx
    ports:
    - protocol: TCP
      port: 8000
  egress:
  - to:
    - namespaceSelector:
        matchLabels:
          name: database
    ports:
    - protocol: TCP
      port: 5432
```

### 2. 应用安全配置
```python
# 安全头设置
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Content-Security-Policy": "default-src 'self'",
}

# CORS 配置
CORS_ORIGINS = [
    "https://yourdomain.com",
    "https://www.yourdomain.com",
]
```

### 3. 密钥管理
```yaml
# Kubernetes Secret 示例
apiVersion: v1
kind: Secret
metadata:
  name: hemall-secrets
type: Opaque
data:
  DATABASE_URL: <base64_encoded>
  REDIS_URL: <base64_encoded>
  JWT_SECRET_KEY: <base64_encoded>
  WECHAT_PAY_KEY: <base64_encoded>
```

## 📊 监控与告警

### 1. Prometheus 告警规则
```yaml
groups:
- name: hemall.rules
  rules:
  - alert: HighErrorRate
    expr: rate(http_requests_total{status=~"5.."}[5m]) > 0.05
    for: 2m
    labels:
      severity: critical
    annotations:
      summary: "High error rate detected"
      
  - alert: HighLatency
    expr: histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 2
    for: 2m
    labels:
      severity: warning
    annotations:
      summary: "High latency detected"
      
  - alert: LowAvailability
    expr: up{job="hemall"} == 0
    for: 1m
    labels:
      severity: critical
    annotations:
      summary: "Service is down"
```

### 2. 自定义业务指标
```python
# 业务指标监控
REQUEST_COUNT = Counter('hemall_requests_total', 'Total requests', ['method', 'endpoint'])
REQUEST_DURATION = Histogram('hemall_request_duration_seconds', 'Request duration')
ORDER_PROCESSING_TIME = Histogram('hemall_order_processing_seconds', 'Order processing time')
PAYMENT_SUCCESS_RATE = Gauge('hemall_payment_success_rate', 'Payment success rate')
```

## 🚀 部署流程

### 1. CI/CD 流程
```yaml
# .github/workflows/deploy.yml
name: Deploy to Production
on:
  release:
    types: [published]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.12'
        
    - name: Install dependencies
      run: |
        pip install -e ".[production]"
        
    - name: Run tests
      run: |
        pytest tests/ -v
      
    - name: Build Docker image
      run: |
        docker build -t hemall:${{ github.sha }} .
        
    - name: Push to registry
      run: |
        docker tag hemall:${{ github.sha }} your-registry/hemall:${{ github.sha }}
        docker push your-registry/hemall:${{ github.sha }}
        
    - name: Deploy to Kubernetes
      run: |
        helm upgrade --install hemall ./charts/hemall \
          --set image.tag=${{ github.sha }} \
          --set replicaCount=3
```

### 2. Helm Values 配置
```yaml
# values.production.yaml
replicaCount: 3

image:
  repository: your-registry/hemall
  tag: latest
  pullPolicy: Always

resources:
  limits:
    cpu: 1000m
    memory: 1Gi
  requests:
    cpu: 500m
    memory: 512Mi

autoscaling:
  enabled: true
  minReplicas: 3
  maxReplicas: 10
  targetCPUUtilizationPercentage: 70

env:
  LOG_LEVEL: INFO
  DATABASE_POOL_SIZE: 20
  REDIS_CONNECTION_POOL_SIZE: 20
```

## 🧪 性能基准测试

### 1. 压力测试脚本
```python
# benchmarks/load_test.py
import asyncio
import httpx
import time
from concurrent.futures import ThreadPoolExecutor

async def benchmark_endpoint(client, url, concurrency=100, duration=60):
    """压力测试单个端点"""
    start_time = time.time()
    request_count = 0
    error_count = 0
    
    async def make_request():
        nonlocal request_count, error_count
        try:
            response = await client.get(url)
            request_count += 1
            if response.status_code != 200:
                error_count += 1
        except Exception:
            error_count += 1
    
    # 并发执行
    tasks = []
    while time.time() - start_time < duration:
        if len(tasks) < concurrency:
            task = asyncio.create_task(make_request())
            tasks.append(task)
        else:
            # 等待部分任务完成
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            tasks = list(pending)
    
    # 等待剩余任务完成
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    
    elapsed = time.time() - start_time
    rps = request_count / elapsed
    
    print(f"URL: {url}")
    print(f"Duration: {elapsed:.2f}s")
    print(f"Requests: {request_count}")
    print(f"Errors: {error_count}")
    print(f"RPS: {rps:.2f}")
    print(f"Success Rate: {(request_count-error_count)/request_count*100:.2f}%")

# 测试关键端点
async def run_benchmarks():
    async with httpx.AsyncClient() as client:
        endpoints = [
            "/health/live",
            "/health/ready", 
            "/products/123",
            "/search/products?q=phone",
            "/recommend/home?user_id=test123"
        ]
        
        for endpoint in endpoints:
            await benchmark_endpoint(client, f"http://localhost:8000{endpoint}")

if __name__ == "__main__":
    asyncio.run(run_benchmarks())
```

### 2. 性能指标目标
```
关键指标目标:
- API 响应时间: < 200ms (95th percentile)
- QPS: > 1000 req/s
- 错误率: < 0.1%
- 可用性: 99.99%
- 内存使用率: < 80%
- CPU 使用率: < 70%
```

## 📚 运维文档

### 1. 日常运维检查清单
```markdown
每日检查:
- [ ] 应用健康状态 (/health/live, /health/ready)
- [ ] 数据库连接池使用情况
- [ ] Redis 内存使用率 (< 80%)
- [ ] Elasticsearch 集群状态 (green)
- [ ] Prometheus 指标采集正常
- [ ] Grafana 面板数据更新

每周检查:
- [ ] 日志轮转和清理
- [ ] 备份策略执行情况
- [ ] 安全补丁更新
- [ ] SSL 证书有效期
- [ ] 监控告警有效性测试

每月检查:
- [ ] 性能基准测试
- [ ] 容量规划评估
- [ ] 灾难恢复演练
- [ ] 安全渗透测试
```

### 2. 故障排查指南
```markdown
常见问题排查:

1. 应用启动失败
   - 检查环境变量配置
   - 查看数据库连接状态
   - 检查依赖服务可用性

2. 响应时间过长
   - 查看慢查询日志
   - 检查 Redis 缓存命中率
   - 分析火焰图定位瓶颈

3. 内存泄漏
   - 监控内存使用趋势
   - 检查连接池泄漏
   - 分析对象引用链

4. 数据库连接耗尽
   - 检查连接池配置
   - 查看长事务和锁等待
   - 优化慢 SQL 查询
```

## 📞 支持与维护

### 1. 版本升级策略
```markdown
版本升级流程:
1. 在测试环境验证新版本
2. 准备回滚方案
3. 执行蓝绿部署或滚动更新
4. 监控关键指标
5. 验证业务功能
6. 完成升级文档记录
```

### 2. 灾难恢复计划
```markdown
灾难恢复步骤:
1. 数据备份恢复
2. 服务降级预案
3. 多区域部署切换
4. 第三方服务替代方案
5. 紧急联系人清单
6. 事故报告和复盘
```

---
*文档版本: 1.0*  
*最后更新: 2025年*  
*适用环境: Hemall v0.1.0+*