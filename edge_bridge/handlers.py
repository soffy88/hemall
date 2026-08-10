"""edge_bridge.handlers — clearnode-iot-bridge 纯翻译函数 (零第三方依赖)。

独立外挂域：本模块只做"MQTT 短键 payload + topic 坐标 → 主干 HTTP 长键
payload"的确定性翻译，不碰网络、不碰 MQTT 客户端——所以主干测试套件可以
直接 import 它做纯单测，不必安装 paho-mqtt。

MQTT topic 契约 (固件侧):
    cn/v1/n/{node_id}/s/{shelf_id}/pick     重力货架 pick (拿走/放回)
    cn/v1/n/{node_id}/g/{gate_id}/req       闸口称重对账请求
    返回: cn/v1/n/{node_id}/g/{gate_id}/res

MQTT payload 契约 (固件侧短键):
    pick: {"m_id": str, "dw": int克, "t_id": str, "ts": int毫秒}
    gate: {"m_id": str, "t_id": str, "rw": int克}

主干 HTTP payload 契约 (hemall ACL 长键):
    pick: {"message_id", "node_id", "shelf_id", "delta_weight", "tote_id", "timestamp"}
    gate: {"gate_id", "tote_id", "raw_weight_grams"}
"""

from __future__ import annotations

from typing import Any


def parse_edge_topic(topic: str) -> dict[str, str]:
    """拆解 MQTT topic 获取物理上下文。

    Args:
        topic: ``cn/v1/n/{node_id}/s/{shelf_id}/pick`` 或
            ``cn/v1/n/{node_id}/g/{gate_id}/req``。

    Returns:
        {"node_id", "entity" ("s"|"g"), "entity_id", "signal"}。

    Raises:
        ValueError: topic 格式不符合约定 (少于 7 段或前缀不是 cn/v1)。
    """
    parts = topic.split("/")
    if len(parts) < 7 or parts[0] != "cn" or parts[1] != "v1":
        raise ValueError(f"malformed edge topic: {topic!r}")
    return {
        "node_id": parts[3],
        "entity": parts[4],
        "entity_id": parts[5],
        "signal": parts[6],
    }


def build_pick_payload(mqtt_payload: dict[str, Any], topic: str) -> dict[str, Any]:
    """把 pick 信号翻译成主干 /ext/hardware/webhook/pick 的标准 payload。

    Args:
        mqtt_payload: 固件短键 payload {"m_id", "dw", "t_id", "ts"}。
        topic: pick topic (cn/v1/n/{n}/s/{s}/pick)。

    Returns:
        {"message_id", "node_id", "shelf_id", "delta_weight", "tote_id", "timestamp"}。

    Raises:
        ValueError: topic 不是 pick 信号或 payload 缺关键字段。
    """
    ctx = parse_edge_topic(topic)
    if ctx["entity"] != "s" or ctx["signal"] != "pick":
        raise ValueError(f"topic is not a pick signal: {topic!r}")
    try:
        delta_weight = int(mqtt_payload["dw"])
        tote_id = str(mqtt_payload["t_id"])
    except KeyError as exc:
        raise ValueError(f"pick payload missing field: {exc}") from exc
    return {
        "message_id": str(mqtt_payload.get("m_id") or ""),
        "node_id": ctx["node_id"],
        "shelf_id": ctx["entity_id"],
        "delta_weight": delta_weight,
        "tote_id": tote_id,
        "timestamp": int(mqtt_payload.get("ts") or 0) or None,
    }


def build_gate_payload(mqtt_payload: dict[str, Any], topic: str) -> dict[str, Any]:
    """把闸口称重信号翻译成主干 /gate-reconcile 的标准 payload。

    Args:
        mqtt_payload: 固件短键 payload {"m_id", "t_id", "rw"}。
        topic: gate req topic (cn/v1/n/{n}/g/{g}/req)。

    Returns:
        {"gate_id", "tote_id", "raw_weight_grams"}。

    Raises:
        ValueError: topic 不是 gate req 信号或 payload 缺关键字段。
    """
    ctx = parse_edge_topic(topic)
    if ctx["entity"] != "g" or ctx["signal"] != "req":
        raise ValueError(f"topic is not a gate request: {topic!r}")
    try:
        raw_weight = int(mqtt_payload["rw"])
        tote_id = str(mqtt_payload["t_id"])
    except KeyError as exc:
        raise ValueError(f"gate payload missing field: {exc}") from exc
    return {
        "gate_id": ctx["entity_id"],
        "tote_id": tote_id,
        "raw_weight_grams": raw_weight,
    }


def build_gate_reply(
    context: dict[str, str], decision: dict[str, Any], mqtt_payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """把主干对账裁决翻译回 MQTT QoS 1 下发行 (响应 topic + 短键 payload)。

    Args:
        context: parse_edge_topic 的产物 (须是 gate req 的上下文)。
        decision: 主干响应 (含 "action"/"act" 与 "led")；缺失时按系统降级
            block/red 处理 (网桥在主干宕机时兜底)。
        mqtt_payload: 原 gate 请求 payload (取 req_id)。

    Returns:
        (response_topic, mqtt_reply_payload)。
        response_topic = cn/v1/n/{node_id}/g/{gate_id}/res
        mqtt_reply_payload = {"req_id", "act", "led"}
    """
    action = decision.get("action") or decision.get("act") or "block"
    led = decision.get("led", "red")
    response_topic = f"cn/v1/n/{context['node_id']}/g/{context['entity_id']}/res"
    return response_topic, {
        "req_id": str(mqtt_payload.get("m_id") or ""),
        "act": action,
        "led": led,
    }
