# Hemall 生产环境部署完整指南

## 📋 部署前准备

### 1. 环境要求

#### 硬件要求
```
最低配置:
- CPU: 4 核
- 内存: 16 GB
- 存储: 100 GB SSD

推荐配置:
- CPU: 8+ 核
- 内存: 32+ GB
- 存储: 500+ GB SSD/NVMe
```

#### 软件要求
```
必需软件:
- Kubernetes 1.24+
- Helm 3.8+
- Docker 20.10+
- kubectl 1.24+

推荐工具:
- cert-manager (TLS 证书管理)
- ingress-nginx (反向代理)
- Prometheus + Grafana (监控)
- Loki + Promtail (日志)
```

### 2. 基础设施准备

#### Kubernetes 集群设置
```bash
# 创建命名空间
kubectl create namespace hemall-production
kubectl create namespace hemall-staging

# 创建必要的 CRDs (如果使用 cert-manager)
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.12.0/cert-manager.crds.yaml

# 安装 cert-manager
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager --namespace cert-manager --create-namespace
```

#### 外部服务准备
```bash
# 创建数据库 Secret
kubectl create secret generic hemall-postgres \
  --from-literal=postgres-password=your-secure-password \
  --namespace hemall-production

# 创建 Redis Secret
kubectl create secret generic hemall-redis \
  --from-literal=redis-password=your-secure-password \
  --namespace hemall-production

# 创建应用 Secret
kubectl create secret generic hemall-secrets \
  --from-literal=jwt-secret-key=your-super-secret-jwt-key \
  --from-literal=database-url=postgresql://hemall:password@postgresql-primary.database.svc.cluster.local:5432/hemall_production \
  --from-literal=redis-url=redis://:password@redis-master.cache.svc.cluster.local:6379/0 \
  --namespace hemall-production
```

## 🚀 部署步骤

### 1. 构建和推送镜像

```bash
# 构建生产镜像
docker build -f Dockerfile.prod -t registry.example.com/hemall/backend:v1.0.0 .

# 推送镜像
docker push registry.example.com/hemall/backend:v1.0.0
```

### 2. 部署 Hemall

```bash
# 部署到生产环境
helm upgrade --install hemall ./charts/hemal \
  --namespace hemall-production \
  --values charts/hemal/values.production.yaml \
  --set image.tag=v1.0.0 \
  --timeout 10m0s

# 检查部署状态
kubectl rollout status deployment/hemall-api -n hemall-production
```

### 3. 验证部署

```bash
# 检查 Pod 状态
kubectl get pods -n hemall-production

# 检查服务状态
kubectl get svc -n hemall-production

# 检查 Ingress
kubectl get ingress -n hemall-production

# 测试健康检查
curl -k https://api.hemall.com/health/live
curl -k https://api.hemall.com/health/ready
```

## 🔧 配置管理

### 1. 环境变量配置

```yaml
# 生产环境关键配置
env:
  LOG_LEVEL: INFO                    # 日志级别
  DATABASE_POOL_SIZE: 30            # 数据库连接池大小
  REDIS_CONNECTION_POOL_SIZE: 30    # Redis 连接池大小
  GUNICORN_WORKERS: 4               # Gunicorn Worker 数量
  GUNICORN_TIMEOUT: 30              # 请求超时时间
```

### 2. 资源限制配置

```yaml
# 资源请求和限制
resources:
  requests:
    cpu: 500m
    memory: 1Gi
  limits:
    cpu: "2"
    memory: 2Gi
```

### 3. 自动伸缩配置

```yaml
# HPA 配置
autoscaling:
  enabled: true
  minReplicas: 3
  maxReplicas: 20
  targetCPUUtilizationPercentage: 70
  targetMemoryUtilizationPercentage: 80
```

## 🛡️ 安全配置

### 1. 网络策略

```yaml
# 启用网络策略
security:
  networkPolicies:
    enabled: true
```

### 2. TLS 证书

```yaml
# Ingress TLS 配置
ingress:
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
```

### 3. Pod 安全标准

```yaml
# Pod 安全上下文
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 3000
    fsGroup: 2000
```

## 📊 监控和告警

### 1. Prometheus 监控

```yaml
# 启用监控
monitoring:
  enabled: true
  serviceMonitor:
    enabled: true
    interval: 30s
```

### 2. 自定义告警规则

```yaml
# PrometheusRule 示例
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: hemall-rules
spec:
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
```

## 🔄 持续集成和部署

### 1. GitHub Actions CI/CD

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
    
    - name: Set up Docker Buildx
      uses: docker/setup-buildx-action@v2
    
    - name: Login to Registry
      uses: docker/login-action@v2
      with:
        registry: registry.example.com
        username: ${{ secrets.REGISTRY_USERNAME }}
        password: ${{ secrets.REGISTRY_PASSWORD }}
    
    - name: Build and push
      uses: docker/build-push-action@v4
      with:
        context: .
        file: ./Dockerfile.prod
        push: true
        tags: registry.example.com/hemall/backend:${{ github.sha }}
    
    - name: Deploy to Kubernetes
      run: |
        helm upgrade --install hemall ./charts/hemal \
          --namespace hemall-production \
          --set image.tag=${{ github.sha }}
```

## 🧪 性能测试

### 1. 运行基准测试

```bash
# 运行性能测试
python scripts/benchmark.py

# 测试结果示例
====================================================================================
性能基准测试结果
====================================================================================
端点                           请求数   成功率   RPS      平均响应(ms)   P95响应(ms)   P99响应(ms)
--------------------------------------------------------------------------------------------
/health/live                   1000     100.0%   100.0    5.2            8.1           12.3
/health/ready                  1000     100.0%   100.0    6.1            9.2           15.4
/products/123                  15000    99.9%    250.0    15.3           28.7          45.2
/search/products?q=phone       9000     99.8%    150.0    22.1           42.3          68.9
/inventory/stock/P001          30000    100.0%   500.0    8.7            18.2          29.1
/recommend/home?user_id=test   7500     99.9%    125.0    31.4           58.7          89.3
/recommend/hot                 7500     100.0%   125.0    28.9           52.1          78.4
/orders/stats                  6000     99.9%    100.0    35.2           65.8          98.7
====================================================================================
```

## 📈 性能优化建议

### 1. 数据库优化

```sql
-- 创建复合索引优化查询性能
CREATE INDEX idx_orders_user_status_created ON orders(user_id, status, created_at DESC);
CREATE INDEX idx_stock_product_warehouse_updated ON stock_movement(product_id, warehouse_id, updated_at DESC);
```

### 2. 缓存策略优化

```python
# 分级缓存策略
CACHE_TTL_MAP = {
    "hot_data": 300,      # 热点数据 5 分钟
    "warm_data": 1800,    # 温数据 30 分钟
    "cold_data": 7200,    # 冷数据 2 小时
    "static_data": 86400, # 静态数据 24 小时
}
```

### 3. 连接池优化

```python
# 数据库连接池配置
DATABASE_POOL_SIZE = 30
DATABASE_MAX_OVERFLOW = 50

# Redis 连接池配置
REDIS_CONNECTION_POOL_SIZE = 30
```

## 🆘 故障排除

### 1. 常见问题

#### 应用启动失败
```bash
# 检查 Pod 日志
kubectl logs -n hemall-production deployment/hemall-api

# 检查事件
kubectl describe pod -n hemall-production <pod-name>
```

#### 响应时间过长
```bash
# 检查资源使用情况
kubectl top pods -n hemall-production

# 检查 HPA 状态
kubectl get hpa -n hemall-production
```

#### 数据库连接问题
```bash
# 检查数据库连接
kubectl exec -it <pod-name> -n hemall-production -- \
  python -c "import asyncpg; print('Database connection OK')"
```

### 2. 回滚操作

```bash
# 回滚到上一个版本
helm rollback hemall 1 -n hemall-production

# 回滚到指定版本
helm history hemall -n hemall-production
helm rollback hemall <revision> -n hemall-production
```

## 📚 运维最佳实践

### 1. 日常维护检查清单

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

### 2. 备份和恢复

```bash
# 数据库备份
kubectl exec -it postgresql-primary-0 -n database -- \
  pg_dump -U hemall hemall_production > backup-$(date +%Y%m%d).sql

# 恢复数据库
kubectl exec -it postgresql-primary-0 -n database -- \
  psql -U hemall hemall_production < backup-20231201.sql
```

---
*文档版本: 1.0*  
*最后更新: 2025年*  
*适用环境: Hemall v0.3.0+*