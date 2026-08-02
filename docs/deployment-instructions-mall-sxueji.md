# Hemall 部署到 mall.sxueji.com - 完整操作指南

## 📋 架构概览

```
Internet → Cloudflare CDN/WAF → Caddy (Auto HTTPS) → Docker Compose Services
                                                    ├── api (Hemall FastAPI)
                                                    ├── db (PostgreSQL 16)
                                                    ├── redis (Redis 7)
                                                    ├── prometheus + grafana
                                                    └── jaeger (tracing)
```

**关键特性：**
- ✅ 自动 SSL 证书（通过 Cloudflare DNS API）
- ✅ Cloudflare CDN 加速（可选开启）
- ✅ 一键部署脚本
- ✅ 生产环境优化配置

---

## 🚀 快速部署（5 步完成）

### 第 1 步：克隆/进入项目目录

```bash
cd /data/soffy/projects/hemal
```

### 第 2 步：准备环境变量

```bash
# 复制环境变量模板
cp .env.mall-sxueji .env

# 编辑并填入真实值
nano .env
```

**必须配置的参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| `HEMALL_PG_PASSWORD` | PostgreSQL 数据库密码 | `sX9#kL2$mP8@nQ5` |
| `HEMALL_JWT_SECRET` | JWT 签名密钥（≥32字节） | 用 `openssl rand -base64 48` 生成 |
| `HEMALL_ANON_SALT` | 匿名数据盐值 | 用 `openssl rand -hex 32` 生成 |
| `CLOUDFLARE_EMAIL` | Cloudflare 账号邮箱 | `admin@sxueji.com` |
| `CLOUDFLARE_API_TOKEN` | Cloudflare DNS API Token | `Tu3K...xyz` |

**生成随机密钥：**
```bash
# JWT Secret
openssl rand -base64 48

# Anon Salt
openssl rand -hex 32
```

### 第 3 步：配置 DNS

确保域名已解析到服务器 IP：

```bash
# 在 Cloudflare DNS 控制台添加：
A record: mall.sxueji.com → <你的服务器公网IP>
A record: www.mall.sxueji.com → <你的服务器公网IP>
```

### 第 4 步：运行部署脚本

```bash
./scripts/deploy-mall-sxueji.sh
```

脚本会自动：
1. ✓ 检查 Docker/Docker Compose
2. ✓ 验证环境变量配置
3. ✓ 创建必要目录
4. ✓ 构建/拉取镜像
5. ✓ 启动所有服务
6. ✓ 等待健康检查通过

### 第 5 步：验证部署

```bash
# 查看所有服务状态
docker compose -f docker-compose.mall-sxueji.yml ps

# 查看 API 日志
docker compose -f docker-compose.mall-sxueji.yml logs -f api

# 测试 HTTP（会自动重定向到 HTTPS）
curl -I https://mall.sxueji.com/health/live
```

预期输出：
```
HTTP/2 200
strict-transport-security: max-age=31536000; includeSubDomains
content-type: application/json
{"status":"ok"}
```

---

## 🔧 详细配置说明

### Cloudflare API Token 创建

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/profile/api-tokens)
2. 点击 "Create Token"
3. 使用模板："Edit zone's DNS"
4. 选择域名区域：`sxueji.com`
5. 权限设置：
   - Zone → DNS → Edit
6. 复制生成的 Token 到 `.env` 文件

### Caddy SSL 证书

Caddy 会自动通过 Cloudflare DNS 验证获取 Let's Encrypt 证书：

```
验证流程：
1. Caddy 请求 SSL 证书
2. Cloudflare 返回 DNS challenge
3. Caddy 通过 API 添加 TXT 记录
4. Let's Encrypt 验证通过后签发证书
5. 证书自动续期（90天）
```

**手动验证证书状态：**
```bash
docker compose -f docker-compose.mall-sxueji.yml exec caddy caddy certificates
```

### 性能调优

已在配置文件中应用以下优化：

**PostgreSQL:**
```yaml
shared_buffers: 256MB      # 物理内存 25%
effective_cache_size: 1GB  # OS + PG cache
max_connections: 200
work_mem: 4MB
```

**Redis:**
```yaml
maxmemory: 2gb
maxmemory-policy: allkeys-lru
appendonly: yes            # AOF 持久化
```

**Gunicorn:**
```python
workers = 4                # CPU核心数 × 2 + 1
timeout = 30
worker_connections = 1000
```

---

## 📊 监控与日志

### Grafana 仪表板

访问 `http://<服务器IP>:3000`（默认管理员/admin）

导入预配置仪表板：
```
/etc/grafana/provisioning/dashboards/hemall-overview.json
```

### Prometheus 指标

```bash
# 直接访问
curl http://localhost:9090/api/v1/query?query=up

# 查看 Hemall 指标
curl http://mall.sxueji.com/metrics
```

### Jaeger 链路追踪

访问 `http://<服务器IP>:16686` 查看分布式追踪数据

### 日志查看

```bash
# 查看所有服务日志
docker compose -f docker-compose.mall-sxueji.yml logs -f

# 只看 API 日志
docker compose -f docker-compose.mall-sxueji.yml logs -f api

# 查看 Caddy 访问日志
docker compose -f docker-compose.mall-sxueji.yml exec caddy tail -f /var/log/caddy/access.log

# 查看 Postgres 日志
docker compose -f docker-compose.mall-sxueji.yml logs -f db
```

---

## 🔄 日常运维

### 更新部署

```bash
# 拉取最新镜像
docker compose -f docker-compose.mall-sxueji.yml pull

# 重启服务
docker compose -f docker-compose.mall-sxueji.yml up -d

# 验证
docker compose -f docker-compose.mall-sxueji.yml ps
```

### 数据库备份

```bash
# 备份数据库
docker compose -f docker-compose.mall-sxueji.yml exec db pg_dump -U hemall hemall_prod > backups/hemall_$(date +%Y%m%d_%H%M%S).sql

# 恢复数据库
cat backups/hemall_20260802.sql | docker compose -f docker-compose.mall-sxueji.yml exec -T db psql -U hemall hemall_prod
```

### 重启服务

```bash
# 重启单个服务
docker compose -f docker-compose.mall-sxueji.yml restart api

# 停止所有服务
docker compose -f docker-compose.mall-sxueji.yml down

# 启动所有服务
docker compose -f docker-compose.mall-sxueji.yml up -d
```

### 清理磁盘空间

```bash
# 清理旧镜像
docker image prune -a

# 清理未使用的卷
docker volume prune

# 清理日志文件
truncate -s 0 $(find /var/lib/docker -name "*.log" 2>/dev/null)
```

---

## ⚠️ 故障排查

### 问题 1: SSL 证书无法获取

**症状：** Caddy 容器启动后无法访问网站

**排查步骤：**
```bash
# 1. 检查 Caddy 日志
docker compose -f docker-compose.mall-sxueji.yml logs caddy

# 2. 验证 Cloudflare Token 权限
curl -s "https://api.cloudflare.com/client/v4/user/tokens/verify" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"

# 3. 确认 DNS 记录正确
dig mall.sxueji.com +short

# 4. 测试 DNS challenge 是否工作
docker compose -f docker-compose.mall-sxueji.yml exec caddy caddy list-resources
```

**解决方案：**
- 确保 API Token 有 "Zone DNS → Edit" 权限
- 确保域名在 Cloudflare 上且 Nameserver 指向 Cloudflare
- 尝试手动触发证书请求：
  ```bash
  docker compose -f docker-compose.mall-sxueji.yml restart caddy
  ```

### 问题 2: PostgreSQL 连接失败

**症状：** API 启动后立即退出，日志显示 connection refused

**排查步骤：**
```bash
# 1. 检查数据库是否就绪
docker compose -f docker-compose.mall-sxueji.yml ps

# 2. 测试数据库连接
docker compose -f docker-compose.mall-sxueji.yml exec db pg_isready -U hemall

# 3. 检查数据库日志
docker compose -f docker-compose.mall-sxueji.yml logs db

# 4. 验证环境变量
docker compose -f docker-compose.mall-sxueji.yml config | grep PG_DSN
```

**解决方案：**
- 等待数据库完全启动（可能需要 30-60 秒）
- 检查 `.env` 中的密码是否正确
- 重启数据库服务：
  ```bash
  docker compose -f docker-compose.mall-sxueji.yml restart db
  ```

### 问题 3: 502 Bad Gateway

**症状：** 访问网站返回 502 错误

**排查步骤：**
```bash
# 1. 检查 API 是否正常运行
docker compose -f docker-compose.mall-sxueji.yml ps api

# 2. 查看 API 日志
docker compose -f docker-compose.mall-sxueji.yml logs api

# 3. 测试 API 内部访问
docker compose -f docker-compose.mall-sxueji.yml exec api curl -s http://localhost:8000/health/live

# 4. 检查 Caddy 配置
docker compose -f docker-compose.mall-sxueji.yml exec caddy caddy format-config --adapter Caddyfile
```

**解决方案：**
- 确保 API 端口 8000 未被占用
- 检查防火墙规则：`ufw status`
- 重启相关服务：
  ```bash
  docker compose -f docker-compose.mall-sxueji.yml restart api caddy
  ```

### 问题 4: 磁盘空间不足

**症状：** 容器启动失败，日志显示 "no space left on device"

**排查步骤：**
```bash
# 1. 检查磁盘使用
df -h

# 2. 查看 Docker 磁盘使用
docker system df

# 3. 查找大文件
du -sh /var/lib/docker/* | sort -rh | head -10
```

**解决方案：**
```bash
# 清理 Docker 资源
docker system prune -a --volumes

# 清理日志文件
find /var/lib/docker -name "*.log" -exec truncate -s 0 {} \;

# 扩展磁盘分区（云服务器）
cloud-init-per first growpart
```

---

## 🔒 安全加固清单

### 已完成的安全措施：
- ✅ HTTPS 强制（HSTS）
- ✅ Security Headers 配置
- ✅ 非 root 用户运行容器
- ✅ 数据库密码通过环境变量管理
- ✅ JWT 签名密钥
- ✅ CORS 限制为指定域名

### 建议额外配置：
- [ ] 配置 UFW 防火墙只开放必要端口（80, 443）
- [ ] 启用 Fail2Ban 防止暴力破解
- [ ] 定期更新系统包：`apt update && apt upgrade -y`
- [ ] 配置 SSH 密钥认证，禁用密码登录
- [ ] 启用 Cloudflare WAF 规则保护应用
- [ ] 配置数据库只读副本用于报表查询
- [ ] 设置日志轮转策略

### 防火墙配置示例：
```bash
# 安装 UFW
sudo apt install ufw -y

# 默认策略
sudo ufw default deny incoming
sudo ufw default allow outgoing

# 允许 SSH, HTTP, HTTPS
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS

# 启用 UFW
sudo ufw enable

# 验证
sudo ufw status
```

---

## 📞 支持信息

- **API 文档**: https://mall.sxueji.com/docs
- **健康检查**: https://mall.sxueji.com/health/live
- **健康就绪**: https://mall.sxueji.com/health/ready
- **Prometheus Metrics**: https://mall.sxueji.com/metrics

---

## 📝 版本历史

| 日期 | 版本 | 说明 |
|------|------|------|
| 2026-08-02 | v1.0.0 | 初始部署配置 |

---

*文档最后更新：2026-08-02*
