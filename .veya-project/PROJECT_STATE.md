# Hemal 项目状态 — 监控 Agent 权威记忆

> 更新时间：2026-08-16
> 仓库：/home/soffy/projects/hemal | 分支：master | 最新 commit：3546cf1

## 项目概览
Hemall — 企业级电商后端。Python 3.12 + FastAPI + Next.js 16 + PostgreSQL 15 + Redis 7 + ES 8.12。
3O 元素库：obase / oprim / oskill / omodul / oservi。已部署 mall.sxueji.com (K8s Helm)。

## 后端模块 (app/, 29,965 行)
orders(12态机) | payments(微信/支付宝) | inventory(FIFO) | search(ES+spider) | recommend(协同过滤) | ai_assistant(RAG) | push | realtime(WebSocket) | risk(风控) | analytics(PG/CH) | eventsourcing(CQRS) | i18n | cache | ext/(23模块含spider/agent_gateway/payment_gateways/hardware_webhook)

## 前端 (web/)
Next.js 16 + React 19 + Tailwind 4 + @helios/blocks 组件库
路由：/admin(商家) /agent(智能体) /shop(商城) /login

## 部署
| 环境 | 地址 | 方式 |
|---|---|---|
| Dev | localhost:8000 | Docker Compose |
| Prod | mall.sxueji.com | K8s Helm charts/hemal |
| Staging | 待确认 | K8s Helm values.staging.yaml |

## 构建命令
uv sync --all-extras | ruff check app/ | ruff format --check app/ | pytest tests/ -q | uv run uvicorn app.main:app --reload

## 版本
v1.0.0-ghost-ignition → v1.1.0-trust-flywheel → v1.2.0-iot-sidecar (当前)

## 安全基线
JWT双token | Webhook HMAC-SHA256 | fail-closed | .env gitignore | IP+Device限流

## 健康端点
/health/live | /health/ready | /metrics (Prometheus)

## 当前风险
- 低：Next.js 16 + React 19 前沿版本需跟踪
- 低：30k 行代码单人维护压力
