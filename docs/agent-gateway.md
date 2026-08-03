# 🤖 智能体网关 (Agent Gateway) — Hermes / Cindy / 任意 Agent 接管系统指南

> 一句话: **把整个 Hemall 暴露成一个标准的工具协议后端**。任何智能体
> (Hermes、Cindy、Claude、自研 Agent) 拿到工具清单就能接管系统——
> 上架商品、调价、补货、结算、退款、视频号推广，全部可编程调用；
> 店主本人用手机打开 `/agent` 指挥台，发中文命令、传实拍视频即可上架。

---

## 1. 四个标准入口

| 端点 | 方法 | 作用 |
|---|---|---|
| `/agent/tools` | GET | 工具发现: 全部 omodul 工具清单 (含参数 JSON Schema) |
| `/agent/execute` | POST | 工具执行 (JSON-RPC: `{tool, args}`) — **Hermes/Cindy 的 function-calling 后端** |
| `/agent/command` | POST | 自然语言命令 (`{"text": "上架 丹东草莓 19.9元 30件"}`) |
| `/agent/ingest` | POST | 视频/图片传货 (multipart `file=`) → 自动上架 |

**鉴权**: `/agent/*` 全部要求管理员 JWT (Bearer `hemall_auth`)，未登录 401。
前端 `/agent` 页面未登录自动跳转 `/login`。

---

## 2. Hermes / Cindy 如何接管系统 (标准流程)

智能体接管 = 三步:

### Step 1 — 拿工具清单 (工具发现)

```bash
# 管理员 JWT (签发方式见 §6)
TOKEN=<ADMIN_JWT>

curl -s http://mall.sxueji.com/agent/tools -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

返回 125+ 个工具，每个含 `tool / domain / path / require_auth / parameters`。
这就是智能体的 function-calling 工具表。

### Step 2 — 工具调用 (function-calling 后端)

把 `/agent/execute` 配成智能体的 tool-call 后端即可:

```json
POST /agent/execute
Authorization: Bearer <ADMIN_JWT>
{
  "tool": "catalog/create_product",
  "args": {"title": "丹东草莓", "slug": "dandong-strawberry"}
}
```

### Step 3 — 完整接管示例 (agent 视角的对话式操作)

```text
用户: 帮我上架丹东草莓，19.9 元，30 件
Agent 决策:
  tool = supply-chain/create_inventory_batch  (需要 variant_id/location_id)
  → 先 catalog/create_product → catalog/update_product(status=published)
  → 再 catalog/create_product_variant
  → 最后 supply-chain/create_inventory_batch(video_url=占位图, stock_qty=30, retail_price=1990)
```

> 不想自己编排的 agent 可以直接走 `/agent/command` 把上面整个链路
> 交给网关编排器 (自动建商品 + 变体 + 批次 + 发布)。

---

## 3. 手机指挥台 (/agent)

店主在手机上打开 `https://mall.sxueji.com/agent` (登录后):

- **📝 命令输入**: 自然语言指挥，如
  - `上架 山东红富士 12.9元 60件`
  - `山东红富士降价到 9.9`
  - `牛奶补货 100 件`
  - `给供应商结算`
  - `订单 <id> 退款 50 元`
  - `山东红富士下架`
- **🎥 视频传货**: 选择手机实拍视频/图片 → 自动创建商品 + 变体 + 库存
  (媒体文件即商品图/视频，可在线播放)
- **🧰 工具面板**: 全部可执行工具清单，与 Hermes/Cindy 拿到的是同一份协议

### 命令种类 → 实际动作

| 命令 | 网关编排动作 |
|---|---|
| 上架/新品/传货 | create_product → publish → create_variant → create_batch |
| 降价/调价 到 X元 | 按商品名找 active 批次 → 改批次零售价 + 同步变体参考价 |
| 补货 N件 | 按商品名找批次 → adjust_inventory_level(+N) |
| 下架/报废 | 按商品名找批次 → mark_batch_for_disposal |
| 结算 | 最新/指定批次 → batch_settlement |
| 退款 N元 | 指定/最新订单 → refund_payment |
| 视频号推广 | 按商品名找批次 → execute_channel_broadcast_workflow |

业务规则由 omodul 自身保证 (如"批次未售完不能结算"、"无支付单不能退款")，
命令层如实回传失败原因。

---

## 4. 视频传货 (/agent/ingest)

```bash
curl -X POST http://mall.sxueji.com/agent/ingest \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/丹东草莓实拍.mp4" \
  -F "retail_price_cents=1999" \
  -F "stock_qty=25"
```

返回 `{product_id, variant_id, batch_id, media_url, ...}`:
- 商品名 = 文件名清洗 (`丹东草莓实拍.mp4` → `丹东草莓实拍`)
- 媒体 = 上传文件本身，落盘 `/media/agent_ingest/<user>/<file>` (可在线播放)
- 售价/库存/分类可用表单参数覆盖；缺省售价按成本+毛利推导
- 文件 ≤ 50MB，重复文件名自动加时间戳避免 SKU 冲突

> 升级路线: 当前商品名/分类靠文件名推导 (MVP)，VLM 就绪后可在
> ingest 前加"实景识别"步骤，自动提取品名/规格/建议售价。

---

## 5. 安全

- `/agent/*` 全部要求管理员 JWT (`get_current_user`)，工具清单里
  `require_auth: true` 的工具必须来自已登录管理员。
- 危险操作 (上架/调价/结算/退款/广播) 都落在 omodul 的决策轨迹
  (decision_trail) 与审计事件里，可回溯。
- 传货文件落盘在隔离目录，媒体 URL 无目录穿越 (StaticFiles 只读挂载)。
- 生产环境务必用 `HEMALL_WEBHOOK_SECRET` 覆盖默认验签密钥。

---

## 6. 管理员 JWT 签发 (运维)

```bash
# 用应用同款 CryptoUtil 签发 (生产用真实 HEMALL_JWT_SECRET)
uv run python -c "
from obase.crypto.util import CryptoUtil
secret = [l.split('=',1)[1].strip() for l in open('.env.mall-sxueji') if l.startswith('HEMALL_JWT_SECRET')][0]
print(CryptoUtil.jwt_sign(payload={'sub':'demo-admin','email':'demo@hemall.local','role':'ADMIN_OPS'}, secret=secret, expires_in_minutes=60, algorithm='HS256'))
"
```

生产环境建议走正式登录 `/auth/login` (app_user 表)，不要把 demo 密钥外泄。

---

## 7. 架构说明

```
手机浏览器 (/agent)  ──┐
Hermes/Cindy 等 Agent ──┼─→  /agent/* (Agent Gateway, ADMIN JWT)
其他自动化/脚本       ──┘          │
                                  ├─ 工具发现:  omodul registry → JSON Schema
                                  ├─ 工具执行:  execute_tool → omodul 同一套装配
                                  ├─ 命令编排:  route_command → execute_command
                                  │              (意图 + 实体提取 + DB 解析缺参)
                                  └─ 视频传货:  ingest_media → 商品/变体/批次/媒体
```

- **业务能力全部是 omodul 端点**，Agent Gateway 只做"发现 + 执行"标准化，
  接新业务能力零改动 (新 omodul 自动出现在 /agent/tools)。
- 命令层是规则引擎 (可插拔 LLM tool-calling)；Hermes/Cindy 走
  `/agent/execute` 时是它们自己编排，命令层只服务手机指挥台。
- **设计约定 (店主定): 规则引擎是默认, LLM 是可选, 永不替换。**
  手机指挥台保持"能传视频、能上架商品"的简单可靠路径 (零依赖/零成本/
  可审计, 每步 decision_trail)；要 LLM 理解时, 由外部 Agent (Hermes/Cindy)
  经 `/agent/execute` 决策, 或未来把 `HEMALL_COMMAND_ENGINE` 配成第二引擎
  (未配置时自动回退 rules, 不影响现有命令)。
- 测试: `tests/test_phase95_agent_gateway.py` (27 用例)。
