# clearnode-iot-bridge (edge_bridge/)

Phase 10: IoT 边缘网桥独立外挂 (Event-Driven Sidecar / Anti-Corruption Layer)。

**一句话**：左手挂物理硬件 (MQTT → EMQX)，右手向 hemall 主干发结构化 HTTP
商业指令。主干不知道什么是 MQTT / QoS 1 / 断线重连——那些全在这里。

## 目录结构 (完全独立，不导入任何主干代码)

```
edge_bridge/
├── Dockerfile          # 网桥专属镜像 (python:3.12-slim)
├── requirements.txt    # paho-mqtt / httpx / pydantic (极简)
├── handlers.py         # 纯翻译函数 (零依赖，可单测)
└── bridge_main.py      # 守护进程 (MQTT 订阅 + HTTP 转发 + 熔断 + 死信队列)
```

## 职责边界

| 域 | 做什么 |
|---|---|
| MQTT (此模块) | 订阅 `cn/v1/n/{node}/s/{shelf}/pick`、`cn/v1/n/{node}/g/{gate}/req` (QoS 1) |
| 翻译 (handlers.py) | 固件短键 payload → 主干长键 payload (确定性纯函数) |
| HTTP (此模块) | `POST /ext/hardware/webhook/{pick,gate-reconcile}`，Bearer `HARDWARE_SECRET` |
| 可靠性 (此模块) | 主干不可达 → 本地 SQLite 死信队列后台重发；连续失败 → 熔断开 + 硬件降级 |

硬件事件是物理事实 (重量已经发生了)——**绝不丢弃**：主干崩溃时 pick 进
死信队列等待重发，闸口请求直接降级 `block/red` 并回执硬件。

## 配置 (环境变量)

| 变量 | 默认 | 说明 |
|---|---|---|
| `MQTT_BROKER` | `emqx` | Broker 主机 |
| `MQTT_PORT` | `1883` | Broker 端口 |
| `MQTT_SUB_TOPICS` | `cn/v1/+/s/+/pick,cn/v1/+/g/+/req` | 订阅主题 |
| `HEMALL_API_URL` | `http://hemall-api:8000/ext/hardware/webhook` | 主干 ACL 根 |
| `HARDWARE_SECRET` | `super-secret-key` | **必须**与 hemall 的 `HEMALL_HARDWARE_SECRET` 一致 |
| `DLQ_PATH` | `/data/dlq.sqlite` | 死信队列 SQLite 路径 |
| `HTTP_TIMEOUT` | `3.0` | 单次转发超时 (秒) |
| `RETRY_INTERVAL` | `5.0` | 死信重发间隔 (秒) |
| `CIRCUIT_MAX_FAILURES` | `5` | 熔断阈值 (连续失败次数) |
| `CIRCUIT_COOLDOWN` | `30.0` | 熔断冷却 (秒) |

## 本地调试

```bash
pip install -r requirements.txt
MQTT_BROKER=localhost HEMALL_API_URL=http://localhost:8000/ext/hardware/webhook \
  HARDWARE_SECRET=dev-hardware-secret-change-me python bridge_main.py
```

## 部署

见仓库根 `docker-compose.iot.yml` (扩展编排，不修改主干 compose)：

```bash
HARDWARE_SECRET=<与主干一致> \
  docker compose -f docker-compose.yml -f docker-compose.iot.yml up -d
```
