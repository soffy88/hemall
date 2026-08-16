# Hemal 自动化调度

| 频率 | 时间 (UTC) | 任务 | 命令 |
|---|---:|---|---|
| 每日 | 09:00 | 代码健康 | ruff check app/ --output-format json |
| 每日 | 09:00 | 测试回归 | pytest tests/ -q --tb=short |
| 每日 | 09:00 | 部署状态 | git status --short && git log --oneline -5 |
| 每周一 | 09:30 | 完整审计 | ruff check app/ --statistics && pytest tests/ -v |
| 每周五 | 16:00 | 配置复核 | 监控配置完整性检查 |

## 手动触发
- 每日检查：project_ask(root="/home/soffy/projects/hemal", request="执行每日监控检查")
- 每周审计：project_ask(root="/home/soffy/projects/hemal", request="执行每周审计")

## 约束
1. 同任务禁止并发；超时标记 TIMEOUT
2. 连续两次失败升级 incident；Critical 立即升级
3. 告警去重：同指纹冷却窗口内更新原事件
