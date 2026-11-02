# 称重台接入契约：真实秤 → Hemal（试点版）

日期：2026-11-03。状态：**试点只用模拟器注入，未接真实秤。**
决定：商祺一体秤只称重，识别归 Hemal（拍照点选试点先行，视觉识别另立项）。

## 1. 链路

```
商祺一体秤（只称重）
  → 店内网关（秤自带 Windows / 或 USB-串口读数程序）
  → POST /ext/hardware/webhook/weigh（HARDWARE_SECRET Bearer <redacted>）
  → hardware_event（event_type='weigh'，与 pick 同记一本账）
  → 柜台页 /shop/counter 轮询最新读数 → 店员点选确认 → 计价行项进单据
```

网关也可以改走 MQTT（话题 `cn/v1/n/{node}/w/{scale}/weigh`，payload 同下），
由 iot-bridge 翻译后打同一个端点；MQTT 认证问题修好前优先直连 HTTP。

## 2. 端点契约

`POST /ext/hardware/webhook/weigh`，`Authorization: Bearer <redacted>`，
`Content-Type: application/json`。字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `message_id` | 是 | 网关生成唯一 ID（`scale-<uuid>`），重发靠它幂等 |
| `weight_grams` | 是 | 整数克，1..15000（秤量程 15KG），超限 422 拒收 |
| `tote_id` | 否 | 筐/台号标签（柜台轮询键之一） |
| `ticket_id` | 否 | 柜台单据 UUID；给了则事件挂进该单据（须为 open 态） |
| `batch_id` / `variant_id` | 否 | 二选一；给了当场计价；都不给只记重量（`unpriced`），柜台确认时挂商品 |
| `node_id` | 否 | 秤/节点编号，留痕 |
| `timestamp` | 否 | 毫秒时间戳，留痕 |

计价（主干侧完成，网关不算钱）：
- 单价 = 批次 `retail_price_cents`（缺失退变体 `reference_price_cents`），取不到 → 422，不写账。
- 基准 = 变体规格文本里的 g/kg（如 `500g/份` → 500 克）；解析不出 → 422，不写账。
- 金额 = 重量 × 单价 / 基准，四舍五入到分。

响应：`{status: processed|unpriced, priced: bool, amount_cents, ...}`；
重发同一 `message_id` 返回 `{status: "duplicate"}`，不重复计账。

## 3. 串口/USB 读数契约（待卖家确认）

网关从秤读数的常见形态（须卖家书面确认后才按此实现）：

- 物理层：RS232 / USB 转串口，常见参数 9600 波特、8 数据位、无校验、1 停止位。
- 帧格式：ASCII 重量字符串（如 `+  1.234kg` / `1234g`），行尾 `\r\n`；
  稳定位/去皮/毛净重标志位以卖家文档为准。
- 网关职责：读串口 → 稳定后取值 → 克换算 → 生成 `message_id` → POST 上报；
  断网本地缓存、恢复后按 `message_id` 幂等补发，不重复计费。

**待卖家书面确认（未确认前不写驱动）：**

1. 重量数据出口形式：串口 / USB / HTTP / MQTT / 全封闭？
2. 串口参数（波特率/数据位/校验/停止位）与帧格式（含稳定位、单位、去皮标志）。
3. 称重模块与主板之间是否还有厂商私有协议，Windows 侧有无 SDK/虚拟串口。
4. AI 摄像头取流方式（RTSP / USB UVC / 封闭），视觉识别立项时再定。
5. 支付通道（扫码/刷卡/现金）与结算回调形式（结算闭环立项时再定）。

## 4. 模拟器（试点专用）

`POST /store/counter/simulate-weigh`（商家鉴权），与真实秤走同一记账函数，
唯一区别是 `source='simulator'` 且 `message_id` 前缀为 `sim-`。
真实秤接入后停用本端点；生产账本中 simulator 行永久可辨，不与真实称重混淆。

## 5. 试点边界（本次不做）

- 视觉识别（另立项）；库存扣减（结算建单时随订单扣，不在这里改库存）；
- 支付与结算闭环（单据只到 `pending_settlement` 待结算态）；
- 闸口“重量 × 支付”双维裁决（IoT 报告 §4.3，结算闭环立项时建）。
