# hemal 项目状态

> 这是监控管理 agent 的权威项目记忆。未知事实不得猜测；接入真实代码仓库或部署环境后，先补齐"待确认"字段。

- **更新时间**：2026-08-16 (monitor bootstrap)
- **状态**：监控基线已建立；已绑定真实仓库 `/home/soffy/projects/hemal`。
- **负责人**：待确认
- **主仓库 / 默认分支**：`/home/soffy/projects/hemal` / `main`

## 目标

持续跟踪 hemal 的代码健康、依赖安全、部署可用性和测试覆盖；发现异常时形成可追踪、可回滚、可复盘的处理闭环。

## 架构与模块（2026-08-16 实勘）

| 项目 | 当前记录 |
|---|---|
| 技术栈与运行时 | FastAPI + uvicorn，Python >=3.12，uv 管理依赖；依赖 3O 元素库 (obase/oprim/oskill/omodul/oservi) |
| 应用入口 | `app/main.py`（FastAPI 应用；含 `/metrics`、`/health/live`、`/health/ready`） |
| 核心模块 | app/{auth,orders,payments,inventory,search,realtime,push,risk,security,eventsourcing,observability,ai_assistant,analytics,recommend,ext} |
| 数据存储 / 外部依赖 | Redis（健康检查涉及）、事件溯源、PG 方言 DDL（见 commit 3546cf1） |
| API / Web / Worker 边界 | FastAPI + uvicorn（gunicorn.conf.py 存在）；edge_bridge 为 IoT 边网独立外挂 |
| 构建、测试、Lint 命令 | `uv run pytest -q`（541 项）；lint 待配置 ruff（不可用则 SKIPPED 不伪报） |

## 部署信息

| 环境 | 地址 / 平台 | 发布方式 | 健康检查 | 回滚方式 |
|---|---|---|---|---|
| Development（本机） | docker compose，容器 `veya-backend` | compose | `http://localhost:8768/health/live` (200)、`/health/ready` (200)、`/metrics` (200) | compose 重启/回退镜像 |
| Staging | 待确认 | 待确认 | 待确认 | 待确认 |
| Production | 待确认 | 待确认 | 待确认 | 待确认 |

注意：主机 8767 端口当前对 `/health/*` 返回 404（8767→容器 8765 映射），8768 正常；监控以 8768 为准。

不得在此文件记录 token、密码、私钥或其他 secret；只记录 secret 的名称和存储位置。

## 监控入口

- 任务清单：[`MONITORING_CHECKLIST.md`](./MONITORING_CHECKLIST.md)
- 调度配置：[`SCHEDULE.md`](./SCHEDULE.md)
- 异常响应：[`INCIDENT_RESPONSE.md`](./INCIDENT_RESPONSE.md)
- 机器可读配置：[`monitoring-agent.yml`](./monitoring-agent.yml)
- Agent 运行器：[`run-monitor.sh`](./run-monitor.sh)（pi headless + 断网自动重试接续）
- 运行记录：`output/YYYY-MM-DD-<task>.md`；runner 日志 `output/runner.log`

## 首次接入待办

- [x] 绑定真实代码仓库、负责人和通知渠道（仓库已绑定；负责人/渠道待补）。
- [x] 识别 manifest、源码入口、构建/测试/Lint 命令（见上表；lint 待配置）。
- [~] 登记各环境 URL、部署平台、健康端点和回滚操作（Dev 已登记，staging/prod 待补）。
- [ ] 配置依赖扫描工具及漏洞告警阈值（uv lock --check 可用；audit 工具待装）。
- [ ] 设置测试覆盖率基线与下降阈值。
- [x] 用 dry-run 验证每日/每周调度和告警去重（由 run-monitor.sh + cron 承担）。

## 当前风险

- **中**：负责人、通知渠道、SLO、覆盖率基线均未定义。
- **低**：lint/依赖审计工具尚未安装（ruff / pip-audit），对应检查为 SKIPPED 而非通过。
