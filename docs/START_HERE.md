# 🚀 Hemall 部署到 mall.sxueji.com - 5 分钟启动

## 前置条件

- ✅ Ubuntu 26.04 服务器（已配置）
- ✅ Docker 29.6.2 已安装并运行
- ✅ Docker Compose v5.3.1 已安装
- ✅ 域名 `mall.sxueji.com` 指向服务器 IP

## 操作步骤

### Step 1: 生成随机密钥

```bash
cd /data/soffy/projects/hemal

# 生成 JWT Secret (32字节+)
export JWT_SECRET=$(openssl rand -base64 48)
echo "JWT_SECRET=$JWT_SECRET" >> .env.mall-sxueji

# 生成 Anon Salt
export ANON_SALT=$(openssl rand -hex 32)
echo "HEMALL_ANON_SALT=$ANON_SALT" >> .env.mall-sxueji

# 设置数据库密码
export PG_PASSWORD=$(openssl rand -base64 32 | tr -d '+/' | cut -c1-32)
echo "HEMALL_PG_PASSWORD=$PG_PASSWORD" >> .env.mall-sxueji

# 设置 Grafana 密码
export GRAFANA_PASSWORD=$(openssl rand -base64 16)
echo "GRAFANA_ADMIN_PASSWORD=$GRAFANA_PASSWORD" >> .env.mall-sxueji
```

### Step 2: 配置 Cloudflare API Token

1. 访问 https://dash.cloudflare.com/profile/api-tokens
2. 创建新 Token，模板选择 "**Edit zone's DNS**"
3. 选择区域：`sxueji.com`
4. 复制生成的 Token
5. 添加到 `.env.mall-sxueji`:
   ```bash
   echo "CLOUDFLARE_EMAIL=admin@sxueji.com" >> .env.mall-sxueji
   echo "CLOUDFLARE_API_TOKEN=<你的Token>" >> .env.mall-sxueji
   ```

### Step 3: 准备环境变量文件

```bash
# 查看当前配置
cat .env.mall-sxueji

# 复制到 .env（脚本会使用这个文件）
cp .env.mall-sxueji .env
```

### Step 4: 执行部署

```bash
./scripts/deploy-mall-sxueji.sh
```

脚本会：
1. ✓ 检查 Docker 环境
2. ✓ 验证配置文件
3. ✓ 创建数据目录
4. ✓ 构建 Docker 镜像（或在本地使用已有镜像）
5. ✓ 启动所有服务（API、PostgreSQL、Redis、Caddy、监控栈）
6. ✓ 等待服务就绪

### Step 5: 验证部署

```bash
# 检查所有容器状态
docker compose -f docker-compose.mall-sxueji.yml ps

# 测试 API 健康检查
curl -s https://mall.sxueji.com/health/live

# 预期输出: {"status":"ok"}
```

---

## 常用命令

```bash
# 查看日志
docker compose -f docker-compose.mall-sxueji.yml logs -f api

# 重启服务
docker compose -f docker-compose.mall-sxueji.yml restart

# 停止服务
docker compose -f docker-compose.mall-sxueji.yml down

# 更新部署
docker compose -f docker-compose.mall-sxueji.yml pull && \
docker compose -f docker-compose.mall-sxueji.yml up -d

# 备份数据库
docker compose -f docker-compose.mall-sxueji.yml exec db pg_dump -U hemall hemall_prod > backup_$(date +%Y%m%d).sql
```

---

## 访问地址

| 服务 | 地址 | 说明 |
|------|------|------|
| **API** | https://mall.sxueji.com | 主应用入口 |
| API 文档 | https://mall.sxueji.com/docs | Swagger UI |
| 健康检查 | https://mall.sxueji.com/health/live | 实时状态 |
| Prometheus | http://localhost:9090 | 指标采集 |
| Grafana | http://localhost:3000 | 可视化仪表板 |
| Jaeger | http://localhost:16686 | 链路追踪 |

---

## 故障排查

如果遇到问题：

```bash
# 查看所有服务日志
docker compose -f docker-compose.mall-sxueji.yml logs

# 检查 Caddy 日志（SSL 证书相关）
docker compose -f docker-compose.mall-sxueji.yml logs caddy

# 检查 PostgreSQL 日志
docker compose -f docker-compose.mall-sxueji.yml logs db

# 查看详细部署文档
cat docs/deployment-instructions-mall-sxueji.md
```

---

## 下一步

1. **配置支付**: 编辑 `.env` 填入微信支付和支付宝配置
2. **配置前端**: 将 Next.js 前端部署到 `https://mall.sxueji.com`
3. **启用 Cloudflare CDN**: 在 Cloudflare 控制台开启缓存和 CDN 加速
4. **设置监控告警**: 配置 Prometheus Alertmanager 发送告警通知

---

*需要帮助？查看完整文档：[deployment-instructions-mall-sxueji.md](docs/deployment-instructions-mall-sxueji.md)*
