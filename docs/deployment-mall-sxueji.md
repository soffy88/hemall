# Hemall 部署到 mall.sxueji.com 完整指南

## 📋 部署前准备

### 1. 环境要求
- Kubernetes 集群 (v1.20+)
- Helm v3+
- cert-manager (用于自动 SSL 证书管理)
- Nginx Ingress Controller
- 外部数据库服务 (PostgreSQL 16+)
- 外部缓存服务 (Redis 7+)
- 外部搜索服务 (Elasticsearch 8+)

### 2. 密钥准备
在部署前，需要创建以下 Kubernetes Secret：

```bash
# 数据库密码
kubectl create secret generic hemall-secrets \
  --from-literal=postgres-password='your-postgres-password' \
  --from-literal=redis-password='your-redis-password' \
  --from-literal=jwt-secret-key='your-jwt-secret-key-32-bytes-min' \
  -n mall-sxueji

# 支付网关密钥
kubectl create secret generic hemall-payment-secrets \
  --from-literal=wechat-pay-api-key='your-wechat-api-key' \
  --from-literal=alipay-private-key='your-alipay-private-key' \
  -n mall-sxueji

# 推送服务密钥
kubectl create secret generic hemall-push-secrets \
  --from-literal=firebase-credentials='your-firebase-credentials-json' \
  --from-literal=apns-key='your-apns-key-content' \
  -n mall-sxueji
```

### 3. SSL 证书准备
如果使用 cert-manager 自动管理证书，请确保已配置 Let's Encrypt 生产环境 Issuer：

```yaml
# letsencrypt-prod.yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@sxueji.com
    privateKeySecretRef:
      name: letsencrypt-prod-account-key
    solvers:
    - http01:
        ingress:
          class: nginx
```

应用 issuer：
```bash
kubectl apply -f letsencrypt-prod.yaml
```

## 🚀 部署步骤

### 1. 构建并推送 Docker 镜像
```bash
# 构建生产镜像
docker build -f Dockerfile.prod -t registry.sxueji.com/hemall/backend:latest .

# 推送到私有仓库
docker push registry.sxueji.com/hemall/backend:latest
```

### 2. 创建命名空间
```bash
kubectl create namespace mall-sxueji
```

### 3. 使用 Helm 部署
```bash
# 方法一：使用部署脚本（推荐）
./scripts/deploy.sh mall-sxueji latest registry.sxueji.com

# 方法二：手动 Helm 命令
helm upgrade --install hemall ./charts/hemal \
  --namespace mall-sxueji \
  --values charts/hemal/values.mall-sxueji.yaml \
  --set image.repository=registry.sxueji.com/hemall/backend \
  --set image.tag=latest \
  --timeout 10m0s
```

### 4. 验证部署
```bash
# 检查 Pod 状态
kubectl get pods -n mall-sxueji

# 检查服务状态
kubectl get svc -n mall-sxueji

# 检查 Ingress 状态
kubectl get ingress -n mall-sxueji

# 检查证书状态
kubectl get certificates -n mall-sxueji
```

## 🔧 环境变量配置

### 核心环境变量
在 `values.mall-sxueji.yaml` 中已配置以下关键环境变量：

| 变量 | 值 | 说明 |
|------|-----|------|
| `CORS_ORIGINS` | `https://mall.sxueji.com,https://www.mall.sxueji.com` | 允许的前端域名 |
| `DATABASE_POOL_SIZE` | `30` | 数据库连接池大小 |
| `REDIS_CONNECTION_POOL_SIZE` | `30` | Redis 连接池大小 |
| `GUNICORN_WORKERS` | `4` | Gunicorn 工作进程数 |

### 安全配置
- **JWT_SECRET**: 必须通过 Secret 配置强随机字符串（≥32字节）
- **ENCRYPTION_KEY**: Fernet 加密密钥，建议通过 Secret 配置
- **SSL**: 强制 HTTPS，HSTS 启用

## 📊 监控与告警

### Prometheus 监控
部署后会自动创建 ServiceMonitor，Prometheus 会自动发现并监控以下指标：
- HTTP 请求计数和延迟
- 数据库连接池状态
- Redis 缓存命中率
- 应用内存和 CPU 使用率

### Grafana 仪表板
导入提供的 Grafana 仪表板 JSON 文件：
```bash
# 仪表板文件位置
grafana/provisioning/dashboards/hemall-overview.json
```

### 告警规则
已配置以下关键告警：
- **高错误率**: 5xx 错误率 > 5%
- **高延迟**: P95 响应时间 > 2秒
- **服务不可用**: Pod 不可用

## 🔄 更新部署

### 蓝绿部署
```bash
# 更新镜像版本
helm upgrade hemall ./charts/hemal \
  --namespace mall-sxueji \
  --values charts/hemal/values.mall-sxueji.yaml \
  --set image.tag=v1.1.0
```

### 回滚
```bash
# 回滚到上一个版本
helm rollback hemall -n mall-sxueji
```

## 🛠️ 故障排查

### 常见问题

#### 1. Ingress 无法访问
- 检查 DNS 解析：`nslookup mall.sxueji.com`
- 检查 Ingress Controller：`kubectl get pods -n ingress-nginx`
- 检查证书状态：`kubectl describe certificate -n mall-sxueji`

#### 2. 数据库连接失败
- 检查 Secret：`kubectl get secret hemall-secrets -n mall-sxueji -o yaml`
- 检查外部数据库网络连通性
- 检查数据库用户权限

#### 3. 应用启动缓慢
- 检查资源限制：`kubectl describe pod -n mall-sxueji`
- 检查依赖服务状态
- 查看应用日志：`kubectl logs -n mall-sxueji -l app=hemall`

### 日志查看
```bash
# 查看应用日志
kubectl logs -f -n mall-sxueji -l app=hemall

# 查看特定 Pod 日志
kubectl logs -f <pod-name> -n mall-sxueji
```

## 📞 支持信息

- **域名**: mall.sxueji.com
- **API 端点**: https://mall.sxueji.com
- **健康检查**: https://mall.sxueji.com/health/live
- **文档**: https://mall.sxueji.com/docs