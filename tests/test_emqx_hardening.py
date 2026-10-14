"""P1-8: EMQX 管理台默认口令 + MQTT 匿名可发布 → 可伪造硬件事件。

`docker-compose.iot.yml` 原本是：

    ports:
      - "1883:1883"    # 物理车库硬件 MQTT 端口
      - "18083:18083"  # EMQX Dashboard (admin/public)

三个问题叠在一起：

1. **Dashboard 绑 0.0.0.0 且用出厂 `admin/public`** —— 注释自己都承认。
   拿到 broker 就等于拿到全部设备数据与控制权。
2. **1883 完全没有认证** —— 任何能连到这个口的主机都可以匿名订阅/发布。
3. `iot-bridge` 收到任意 MQTT 消息后会用 `HARDWARE_SECRET` 签名
   POST `/ext/hardware/webhook`（该 Secret 由 compose 注入，本意就是要通）。
   于是攻击者只要能向 broker 发一条消息，桥就会**合法地**转发一条伪造的
   硬件事件 —— 上游验签必然通过。这条链是"匿名 MQTT"直接变现成"伪造订单/
   库存/闸口事件"的路径。

修复要点：dashboard 只绑回环 + 口令从环境变量读且缺失即拒绝启动；
1883 启用内置数据库认证 + 文件 ACL，ACL 只允许 edge_bridge 账号在硬件主题
上 publish/subscribe。

本测试分两层：
- **静态契约**（compose / ACL / bootstrap 脚本 / 网桥代码），跑得快、抓回归；
- **行为验证**（真起一个 EMQX + 真 MQTT 客户端），证明 ACL 真的拦得住 ——
  这类"安全配置"最怕的就是 YAML 看着对、运行时没生效。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
IOT_COMPOSE = REPO_ROOT / "docker-compose.iot.yml"
ACL_FILE = REPO_ROOT / "emqx" / "acl.conf"
BOOTSTRAP = REPO_ROOT / "emqx" / "bootstrap.sh"
BRIDGE_MAIN = REPO_ROOT / "edge_bridge" / "bridge_main.py"

#: 网桥订阅的硬件主题（来自 bridge_main.py 的 MQTT_SUB_TOPICS 默认值）。
#: ACL 必须覆盖这些主题，否则加固之后硬件事件直接收不到 —— 这是"只收紧
#: 权限"时最容易踩的坑。
HARDWARE_TOPICS = ["cn/v1/+/s/+/pick", "cn/v1/+/g/+/req"]


def _iot() -> dict:
    return yaml.safe_load(IOT_COMPOSE.read_text(encoding="utf-8")) or {}


def _emqx_service() -> dict:
    return _iot()["services"]["emqx"]


def _emqx_env() -> dict[str, str]:
    env = _emqx_service().get("environment") or {}
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    out: dict[str, str] = {}
    for item in env:
        k, _, v = str(item).partition("=")
        out[k] = v
    return out


# --------------------------------------------------------------------------
# Dashboard: 不得对公网暴露，且不得用出厂口令
# --------------------------------------------------------------------------


def test_dashboard_is_not_published_on_all_interfaces():
    ports = _emqx_service().get("ports") or []
    dash = [p for p in ports if "18083" in str(p)]
    assert dash, "找不到 dashboard 的端口映射"
    for entry in dash:
        assert str(entry).startswith("127.0.0.1:") or "127.0.0.1:" in str(entry), (
            f"EMQX dashboard 绑定 {entry} —— 管理台控制着全部设备数据与控制权，"
            f"必须只绑 127.0.0.1（需要远程访问请走 SSH 隧道 / VPN）"
        )


def test_dashboard_password_is_required_and_not_the_factory_default():
    env = _emqx_env()
    value = env.get("EMQX_DASHBOARD__DEFAULT_PASSWORD", "")
    assert value, "EMQX 未设置 EMQX_DASHBOARD__DEFAULT_PASSWORD —— 出厂默认是 admin/public"
    assert ":?" in value, (
        f"dashboard 口令必须以 `${{VAR:?}}` 形式 fail-closed 注入，实际为 {value!r}；"
        f"缺失时应当拒绝启动，而不是悄悄用 admin/public"
    )
    # 变量名不能是占位默认值
    var = value.split(":?", 1)[1].rstrip("}")
    assert var, "fail-closed 变量名不能为空"
    assert var not in {"public", "admin", "changeme", "change_me"}, f"dashboard 口令变量名 {var!r} 本身就是弱默认值"


#: 会被一眼看穿的弱口令。出现在"生效值"里就是事故；出现在 `:?` 的报错
#: 文案里（告诉运维出厂默认是什么）则是必要的。
WEAK_SECRETS = {"public", "admin", "changeme", "change_me", "password", "secret", "12345", "123456", "admin123"}


def _resolved_secret(value: str) -> str | None:
    """从 compose 的值里取出"生效的凭据"。

    - `${VAR:-default}` → default 是生效值（没设环境变量时就用它）
    - `${VAR:?message}` → None（fail-closed，必须由环境提供）
    - 字面量          → 字面量本身
    """
    text = str(value)
    if ":?" in text:
        return None
    m = re.search(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-(.+)\}", text)
    if m:
        return m.group(1)
    if "${" in text:
        return None
    return text


def test_default_credentials_are_not_baked_into_the_compose_file():
    """compose 里不能有**生效的**弱口令。

    不做裸子串匹配：`${VAR:?... the EMQX factory default is admin/public}`
    这类报错文案里提到出厂默认值是必要的，真正要防的是把它写进某个生效值。
    """
    for service, env in _iot()["services"].items():
        items = env.get("environment") or []
        pairs = env.items() if isinstance(env, dict) else [str(i).split("=", 1) for i in items]
        for key, value in pairs:
            if "PASSWORD" not in str(key).upper() and "SECRET" not in str(key).upper():
                continue
            resolved = _resolved_secret(value)
            assert resolved is None, (
                f"{service}.{key} 用了 `${{VAR:-...}}` 默认值 {resolved!r} —— "
                f"这会在环境变量没设时悄悄退回一个可猜的值"
            )
            assert str(resolved).strip().lower() not in WEAK_SECRETS, (
                f"{service}.{key} 写死了弱口令"
            )

    for service in ("emqx", "emqx-bootstrap", "iot-bridge"):
        env = _iot()["services"][service].get("environment") or {}
        if isinstance(env, dict):
            joined = "\n".join(f"{k}={v}" for k, v in env.items())
        else:
            joined = "\n".join(map(str, env))
        assert ":?" in joined or "${" in joined, f"{service} 的凭据没有走环境变量注入"


# --------------------------------------------------------------------------
# MQTT 1883: 认证 + ACL
# --------------------------------------------------------------------------


def test_mqtt_authenticator_is_configured():
    env = _emqx_env()
    assert env.get("EMQX_AUTHENTICATION__1__MECHANISM") == "password_based", (
        "1883 未启用密码认证 —— 任何能连到该口的主机都能匿名发布伪造硬件事件"
    )
    assert env.get("EMQX_AUTHENTICATION__1__BACKEND") == "built_in_database"
    # 内置库明文存储不可接受
    assert "sha256" in str(env.get("EMQX_AUTHENTICATION__1__PASSWORD_HASH_ALGORITHM__NAME", "")).lower() or (
        "bcrypt" in str(env.get("EMQX_AUTHENTICATION__1__PASSWORD_HASH_ALGORITHM__NAME", "")).lower()
    ), "口令哈希算法未显式指定（默认可能是明文/弱哈希）"


def test_mqtt_authorization_denies_by_default():
    env = _emqx_env()
    assert env.get("EMQX_AUTHORIZATION__NO_MATCH") == "deny", (
        "no_match 必须是 deny：默认放行等于没有 ACL"
    )
    # 静默丢弃会让"有人在越权"完全不可见
    assert env.get("EMQX_AUTHORIZATION__DENY_ACTION") == "disconnect", (
        "deny_action 应为 disconnect：ignore 会把越权尝试静默丢弃，"
        "运维完全看不到有人在试探 ACL"
    )
    # 命中缓存会让改过的 ACL 在 ttl 内不生效
    assert str(env.get("EMQX_AUTHORIZATION__CACHE__ENABLE", "")).lower() == "false", (
        "授权缓存必须关闭：缓存会让刚改的 ACL 在 TTL 内继续放行（安全上等同于没改）"
    )
    assert env.get("EMQX_AUTHORIZATION__SOURCES__1__TYPE") == "file"
    assert env.get("EMQX_AUTHORIZATION__SOURCES__1__ENABLE") == "true"


def test_acl_file_is_mounted_read_only():
    volumes = [str(v) for v in _emqx_service().get("volumes") or []]
    acl_mounts = [v for v in volumes if "acl.conf" in v]
    assert acl_mounts, "ACL 文件没有挂进 EMQX 容器 —— 授权链指向了一个不存在的文件"
    for mount in acl_mounts:
        assert mount.rstrip("/").endswith(":ro"), f"ACL 必须只读挂载: {mount}"


# --------------------------------------------------------------------------
# ACL 内容
# --------------------------------------------------------------------------


def test_acl_file_exists_and_only_allows_hardware_topics():
    assert ACL_FILE.is_file(), f"缺少 {ACL_FILE}"
    text = ACL_FILE.read_text(encoding="utf-8")

    allows = re.findall(r"\{\s*allow\s*,([^}]*)\}", text)
    denies = re.findall(r"\{\s*deny\s*,([^}]*)\}", text)
    assert allows, "ACL 里一条 allow 都没有 —— 没有任何客户端能连（网桥会连不上）"
    assert denies, (
        "ACL 里一条显式 deny 都没有。`no_match=deny` 只在**没有匹配规则**时兜底，"
        "但如果 allow 规则写得太宽（例如 `{allow, all, publish, [\"#\"]}`），"
        "兜底永远不会触发 —— 需要显式 deny 收紧。"
    )
    for rule in allows:
        assert '"#"' not in rule, f"存在全通配规则 {rule.strip()} —— 等于没有 ACL"
        assert '"+"' not in rule.split("publish")[-1] if "publish" in rule else True, (
            f"publish 规则里不应出现单层通配 {rule.strip()}"
        )


def test_acl_covers_every_topic_the_bridge_subscribes():
    """ACL 必须覆盖网桥订阅的全部主题，否则加固后硬件事件直接收不到。"""
    assert ACL_FILE.is_file()
    text = ACL_FILE.read_text(encoding="utf-8")
    missing = [t for t in HARDWARE_TOPICS if t not in text]
    assert not missing, (
        f"ACL 没有覆盖网桥订阅的主题 {missing} —— 加固之后硬件事件会被 no_match=deny "
        f"全部丢弃（网桥只订阅 {HARDWARE_TOPICS}）"
    )
    assert "subscribe" in text, "ACL 必须包含 subscribe 规则，否则网桥收不到任何事件"


def test_acl_mentions_the_edge_bridge_account():
    text = ACL_FILE.read_text(encoding="utf-8")
    assert "edge_bridge" in text, (
        "ACL 应显式限定 edge_bridge 账号；`all` 意味着任何通过认证的用户都能发布硬件主题"
    )


def test_bridge_sub_topics_in_code_match_the_acl():
    """网桥代码里的默认订阅主题与 ACL 必须一致（防止有人改了代码忘了改 ACL）。"""
    code = BRIDGE_MAIN.read_text(encoding="utf-8")
    m = re.search(r'MQTT_SUB_TOPICS\s*=\s*os\.getenv\(\s*"MQTT_SUB_TOPICS"\s*,\s*"([^"]+)"', code)
    assert m, "未能从 bridge_main.py 解析出 MQTT_SUB_TOPICS 默认值"
    topics = [t.strip() for t in m.group(1).split(",")]
    acl = ACL_FILE.read_text(encoding="utf-8")
    missing = [t for t in topics if t not in acl]
    assert not missing, f"代码订阅了 {missing} 但 ACL 未覆盖 —— 这些事件会被丢弃"


# --------------------------------------------------------------------------
# bootstrap: 账号必须在启动时以 fail-closed 的方式创建
# --------------------------------------------------------------------------


def test_bootstrap_script_exists_and_is_fail_closed():
    assert BOOTSTRAP.is_file(), f"缺少 {BOOTSTRAP} —— 内置数据库里没有用户，1883 等于全员被拒"
    text = BOOTSTRAP.read_text(encoding="utf-8")
    for var in ("HEMALL_EMQX_DASHBOARD_PASSWORD", "HEMALL_EMQX_EDGE_USERNAME", "HEMALL_EMQX_EDGE_PASSWORD"):
        assert var in text, f"bootstrap 必须消费 {var}，否则账号/口令只能靠人工在控制台点"
    assert ":?" in (IOT_COMPOSE.read_text(encoding="utf-8")), "compose 侧应以 ${VAR:?} 形式注入上述变量"
    assert re.search(r'exit 1|exit 1\b', text), "bootstrap 在缺配置时必须非零退出"


def test_bootstrap_rejects_weak_or_default_secrets():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert re.search(r"public|admin|changeme|change_me|12345|secret", text, re.I), (
        "bootstrap 必须显式拒绝弱口令/出厂默认值，而不是照单全收"
    )
    assert re.search(r"\$\{#", text) or "length" in text or "弱" in text or "weak" in text.lower(), (
        "bootstrap 应当对口令做最小长度检查"
    )


def test_bootstrap_is_idempotent():
    """bootstrap 会在每次 `docker compose up` 时重跑，必须可重复执行。"""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "409" in text or "already" in text.lower() or "ignore" in text.lower(), (
        "bootstrap 未处理「用户已存在」的情况 —— 第二次启动会因 409 失败，"
        "让 compose 永远停在 bootstrap 步骤"
    )


def test_bridge_depends_on_bootstrap_completion():
    """网桥必须在账号就绪后才启动，否则它会连不上 1883。"""
    depends = _iot()["services"]["iot-bridge"].get("depends_on")
    entry = depends if isinstance(depends, dict) else {}
    key = next((k for k in entry if "bootstrap" in k), None)
    assert key, f"iot-bridge 依赖 {list(entry)}，没有等 bootstrap 完成"
    assert "completed_successfully" in str(entry[key]), (
        f"bootstrap 依赖条件是 {entry[key]} —— 只是 started 的话网桥会在账号创建前就去连 1883"
    )


def test_bridge_authenticates_to_mqtt():
    """网桥必须带用户名口令连 broker，且缺失时不许匿名连接。"""
    code = BRIDGE_MAIN.read_text(encoding="utf-8")
    assert re.search(r"username_pw_set", code), "网桥没有设置 MQTT 用户名口令 —— 认证开了也连不上"
    assert re.search(r"MQTT_USERNAME\s*=", code), "网桥未从环境变量读取 MQTT 用户名"
    assert re.search(r"MQTT_PASSWORD\s*=", code), "网桥未从环境变量读取 MQTT 口令"

    bridge_env = _iot()["services"]["iot-bridge"].get("environment") or []
    joined = "\n".join(map(str, bridge_env))
    assert "MQTT_USERNAME" in joined and "MQTT_PASSWORD" in joined, (
        "compose 没给网桥注入 MQTT 凭据"
    )
    assert ":?" in joined, "网桥的 MQTT 凭据必须以 ${VAR:?} fail-closed 注入"


# --------------------------------------------------------------------------
# 行为验证: 真起 EMQX + 真 MQTT 客户端，证明 ACL 真的拦得住
# --------------------------------------------------------------------------

_BEHAVIOUR = r"""
set -eu
BASE=http://127.0.0.1:${DASH_PORT}
MQ_HOST=127.0.0.1
MQ_PORT=${MQTT_PORT}
DL_PASSWORD='${DL_PASSWORD}'
EDGE_USER='${EDGE_USER}'
EDGE_PASSWORD='${EDGE_PASSWORD}'

fail() { echo "RESULT_FAIL: $1"; exit 1; }

# 匿名连接必须被拒
out=$(mosquitto_pub -h "$MQ_HOST" -p "$MQ_PORT" -t 'cn/v1/D1/s/S1/pick' -m x -d 2>&1 || true)
echo "$out" | grep -q 'CONNACK (4)' || fail "匿名连接没有被拒: $out"

# 口令错误必须被拒
out=$(mosquitto_pub -h "$MQ_HOST" -p "$MQ_PORT" -u "$EDGE_USER" -P wrong-password -t 'cn/v1/D1/s/S1/pick' -m x -d 2>&1 || true)
echo "$out" | grep -qE 'CONNACK \((4|5)\)' || fail "错误口令没有被拒: $out"

# 未知用户必须被拒
out=$(mosquitto_pub -h "$MQ_HOST" -p "$MQ_PORT" -u attacker -P whatever -t 'cn/v1/D1/s/S1/pick' -m x -d 2>&1 || true)
echo "$out" | grep -qE 'CONNACK \((4|5)\)' || fail "未知用户没有被拒: $out"

# 正确凭据 + 允许的主题 -> 必须真的送达订阅者
mosquitto_sub -h "$MQ_HOST" -p "$MQ_PORT" -u "$EDGE_USER" -P "$EDGE_PASSWORD" -t 'cn/v1/+/s/+/pick' -v -C 1 > "$SUB_LOG" 2>/dev/null &
subpid=$!
sleep 3
mosquitto_pub -h "$MQ_HOST" -p "$MQ_PORT" -u "$EDGE_USER" -P "$EDGE_PASSWORD" -t 'cn/v1/D1/s/S1/pick' -m 'ALLOWED' -q 1 \
  || fail "正确凭据发布允许主题失败"
sleep 3

# 正确凭据 + 禁止的主题 -> 不得送达
mosquitto_pub -h "$MQ_HOST" -p "$MQ_PORT" -u "$EDGE_USER" -P "$EDGE_PASSWORD" -t 'cn/v1/D1/s/S1/tamper' -m 'FORBIDDEN' -q 1 >/dev/null 2>&1 || true
sleep 3
kill "$subpid" 2>/dev/null || true
wait "$subpid" 2>/dev/null || true

echo "--- 订阅者实际收到 ---"
cat "$SUB_LOG" || true
grep -q 'ALLOWED' "$SUB_LOG" || fail "允许主题的消息没有送达（ACL 收得太紧，网桥会收不到硬件事件）"
if grep -q 'FORBIDDEN' "$SUB_LOG"; then
  fail "禁止主题的消息竟然送达了 —— ACL 没生效"
fi
echo "RESULT_OK"
"""


def _have_docker() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True
    ).returncode == 0


@pytest.mark.slow
@pytest.mark.skipif(not _have_docker(), reason="需要可用的 docker 来起真 EMQX")
def test_real_emqx_enforces_authentication_and_acl(tmp_path):
    """真起 EMQX（emqx:5.3）+ 真 MQTT 客户端，跑完整攻击矩阵。

    这一步是这类修复唯一有意义的验证：YAML 写得再对，ACL 没被加载 /
    认证没生效 / 缓存挡住变更，都只会在**真的发一条消息**时暴露。

    慢（拉镜像 + 等 EMQX 起 + 建账号，约 1-2 分钟），故标 slow；
    不在默认 CI 里跑，但任何改动 ACL / 认证链之后都应当跑一次。
    """
    dash_port, mqtt_port = 19085, 11985
    dl_password = "LabDashboardPw-9f3c1a"
    edge_user, edge_password = "edge_bridge", "LabEdgePw-7b2e4d"

    # 复用仓库里的 compose 配置（EMQX 那部分），换端口与凭据起一个一次性实例
    acl_mount = str(ACL_FILE)
    acl_container_path = "/opt/emqx/etc/acl.conf"
    subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", "emqx-behaviour",
                "-p", f"127.0.0.1:{dash_port}:18083",
                "-p", f"127.0.0.1:{mqtt_port}:1883",
                "-e", f"EMQX_DASHBOARD__DEFAULT_PASSWORD={dl_password}",
                "-e", "EMQX_AUTHENTICATION__1__MECHANISM=password_based",
                "-e", "EMQX_AUTHENTICATION__1__BACKEND=built_in_database",
                "-e", "EMQX_AUTHENTICATION__1__USER_ID_TYPE=username",
                "-e", "EMQX_AUTHENTICATION__1__PASSWORD_HASH_ALGORITHM__NAME=sha256",
                "-e", "EMQX_AUTHENTICATION__1__PASSWORD_HASH_ALGORITHM__SALT_POSITION=suffix",
                "-e", "EMQX_AUTHORIZATION__NO_MATCH=deny",
                "-e", "EMQX_AUTHORIZATION__DENY_ACTION=disconnect",
                "-e", "EMQX_AUTHORIZATION__CACHE__ENABLE=false",
                "-e", "EMQX_AUTHORIZATION__SOURCES__1__TYPE=file",
                "-e", "EMQX_AUTHORIZATION__SOURCES__1__ENABLE=true",
                "-e", f"EMQX_AUTHORIZATION__SOURCES__1__PATH={acl_container_path}",
                "-v", f"{acl_mount}:{acl_container_path}:ro",
                "emqx:5.3",
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )

    try:
        import time

        # 等 dashboard 就绪
        ready = False
        for _ in range(60):
            probe = subprocess.run(
                ["curl", "-sf", f"http://127.0.0.1:{dash_port}/status"], capture_output=True
            )
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(2)
        assert ready, "EMQX dashboard 未在超时内就绪"

        # 用仓库里的 bootstrap 脚本创建账号 —— 保证测的是真实现，不是另写一份
        env = {
            "EMQX_BOOTSTRAP_URL": f"http://127.0.0.1:{dash_port}",
            "HEMALL_EMQX_DASHBOARD_USERNAME": "admin",
            "HEMALL_EMQX_DASHBOARD_PASSWORD": dl_password,
            "HEMALL_EMQX_EDGE_USERNAME": edge_user,
            "HEMALL_EMQX_EDGE_PASSWORD": edge_password,
        }
        boot = subprocess.run(
            ["docker", "run", "--rm", "--network", "host",
             "-v", f"{REPO_ROOT / 'emqx'}:/work:ro", "-w", "/work",
             "-e", "EMQX_BOOTSTRAP_URL", "-e", "HEMALL_EMQX_DASHBOARD_USERNAME",
             "-e", "HEMALL_EMQX_DASHBOARD_PASSWORD", "-e", "HEMALL_EMQX_EDGE_USERNAME",
             "-e", "HEMALL_EMQX_EDGE_PASSWORD",
             "curlimages/curl:8.11.1", "sh", "/work/bootstrap.sh"],
            capture_output=True, text=True, env={**dict(**__import__("os").environ), **env}, timeout=300,
        )
        assert boot.returncode == 0, f"bootstrap 失败:\n{boot.stdout}\n{boot.stderr}"

        # 在带 mosquitto 客户端的容器里跑攻击矩阵
        sub_log = tmp_path / "sub.log"
        sub_log.touch()
        script = (
            _BEHAVIOUR.replace("${DASH_PORT}", str(dash_port))
            .replace("${MQTT_PORT}", str(mqtt_port))
            .replace("${DL_PASSWORD}", dl_password)
            .replace("${EDGE_USER}", edge_user)
            .replace("${EDGE_PASSWORD}", edge_password)
            .replace("$SUB_LOG", "/t/sub.log")
        )
        script_path = tmp_path / "matrix.sh"
        script_path.write_text(script, encoding="utf-8")

        run = subprocess.run(
            ["docker", "run", "--rm", "--network", "host", "-v", f"{tmp_path}:/t",
             "eclipse-mosquitto:2", "sh", "/t/matrix.sh"],
            capture_output=True, text=True, timeout=600,
        )
        combined = run.stdout + run.stderr
        assert "RESULT_OK" in combined, (
            f"ACL/认证行为验证失败:\n{combined}\n--- 订阅者实际收到 ---\n"
            f"{sub_log.read_text(encoding='utf-8')}"
        )
    finally:
        subprocess.run(["docker", "rm", "-f", "emqx-behaviour"], capture_output=True)
