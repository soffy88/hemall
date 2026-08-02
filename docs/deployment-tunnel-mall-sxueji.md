# Hemall 部署到 mall.sxueji.com — Cloudflare Tunnel 接入指南

## 📋 架构

```
用户浏览器
    ↓ https://mall.sxueji.com
Cloudflare 边缘 (TLS 终止, CDN/WAF)
    ↓ Cloudflare Tunnel (远程管理模式, 已运行: aegis-cloudflared)
Docker 网络 helios-net
    ↓ http://hemal-caddy-1:80
Caddy (:8092 容器内 :80)  ← 反向代理 + 安全头
    ↓ http://api:8000
Hemall FastAPI
    ├── PostgreSQL 16 (db)
    ├── Redis 7
    ├── Prometheus / Grafana / Jaeger (内部网络)
```

**关键点：**
- 服务器 80/443 已被 `aegis-caddy` 占用（其他项目）
- 服务器架构为 Cloudflare Tunnel **远程管理模式**（cloudflared --token）
- 路由配置在 Cloudflare Dashboard，不在服务器本地
- 我们的 Caddy 监听 `127.0.0.1:8092`（无冲突），并加入共享网络 `helios-net`

---

## ✅ 已完成（服务器端）

- [x] Docker Compose 配置 `docker-compose.mall-sxueji.yml`
- [x] Caddyfile（内部 HTTP 反代，TLS 由 Cloudflare 管理）
- [x] 环境变量 `.env.mall-sxueji`（随机密钥已生成）
- [x] API 镜像（含 3O 平台包依赖）
- [x] 数据库初始化脚本

## 📌 待您完成（Cloudflare Dashboard，约 3 分钟）

### Step 1: 确认/添加 DNS 记录

在 Cloudflare Dashboard → `sxueji.com` zone → DNS：

| 类型 | 名称 | 内容 | 代理状态 |
|------|------|------|----------|
| CNAME | `mall` | `<tunnel-id>.cfargotunnel.com` | Proxied (橙色云朵) |

> 若使用远程管理 tunnel：`tunnel-id` 在 **Zero Trust → Networks → Tunnels** 页面查看。
> 若想先让 DNS 指向服务器再走 tunnel，可改为 A 记录指向 `47.236.50.128`（但 tunnel 方案推荐 CNAME）。

### Step 2: 添加 Tunnel 路由

Cloudflare Dashboard → **Zero Trust → Networks → Tunnels** → 选择现有 tunnel（aegis）→ **Public Hostnames** → **Add a public hostname**：

| 字段 | 值 |
|------|-----|
| Subdomain | `mall` |
| Domain | `sxueji.com` |
| Path | (留空) |
| Service Type | `HTTP` |
| URL | `http://hemal-caddy-1:80` |

> `hemal-caddy-1` 是我们的 Caddy 容器名（加入 helios-net 网络后，cloudflared 可通过 Docker DNS 解析）。
> 若容器名不同，用 `docker ps | grep caddy` 查看实际名称。

### Step 3: 验证

```bash
# 服务器端验证 Caddy 已就绪
curl -H "Host: mall.sxueji.com" http://127.0.0.1:8092/health/live

# 浏览器访问
https://mall.sxueji.com/health/live
# 预期: {"status":"ok"}
```

---

## 🚀 部署命令

```bash
cd /data/soffy/projects/hemal

# 1. 构建 API 镜像（如未构建）
docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml build api

# 2. 启动全部服务
docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml up -d

# 3. 查看状态
docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml ps

# 4. 查看日志
docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml logs -f api
```

## 🩺 故障排查

| 症状 | 排查 |
|------|------|
| 502 Bad Gateway | `docker compose ... ps` 检查 api 是否 healthy；`docker compose ... logs api` |
| tunnel 无法连接 | 确认 Caddy 容器已加入 helios-net：`docker network inspect helios-net \| grep caddy` |
| 域名打不开 | Dashboard 检查 Public Hostname 配置、DNS CNAME 是否 Proxied |
| 证书错误 | Tunnel 模式下证书由 Cloudflare 自动管理，等待 1-5 分钟生效 |

## 🔄 更新部署

```bash
docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml build api
docker compose --env-file .env.mall-sxueji -f docker-compose.mall-sxueji.yml up -d
```

## 📊 服务地址（内网）

| 服务 | 地址 |
|------|------|
| API | `http://api:8000`（内部）/ `https://mall.sxueji.com`（公网） |
| Caddy | `127.0.0.1:8092`（宿主） |
| Prometheus | 仅内部网络（hemall-backend） |
| Grafana | 仅内部网络（hemall-backend） |
| Jaeger | 仅内部网络（hemall-backend） |
