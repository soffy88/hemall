"""线上事故修复回归 (2026-10-15) — 部署工件层。

事故 3: web 健康检查访问 ``127.0.0.1:3000`` 被拒 (Connection refused)。
Docker 会在容器运行时注入自己的 ``HOSTNAME`` (容器 IP), 覆盖镜像层的
``ENV HOSTNAME=0.0.0.0``; Next standalone 的 ``server.js`` 用
``process.env.HOSTNAME`` 决定 bind 地址, 于是只绑容器 IP, loopback 上没人监听,
健康检查恒失败 → 容器被反复重启 / 被判 unhealthy。

修法: CMD 用 shell 在 exec 前就地赋值 ``HOSTNAME=0.0.0.0`` (shell 设的进程环境
一定压过 Docker 注入的那个), 健康检查再兜底探测容器自身 IP。

这里锁住这些约定, 防止以后有人把 CMD 改回裸 ``node server.js``。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DOCKERFILE = REPO_ROOT / "web" / "Dockerfile"

COMPOSE_FILES = [
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "docker-compose.mall-sxueji.yml",
]


def parse_dockerfile(text: str) -> dict[str, str]:
    """把 Dockerfile 解析成 {指令: 逻辑行}, 跳过注释与续行反斜杠。

    直接按 "CMD " / "HEALTHCHECK " 切字符串会被注释里的同名词骗到, 所以自己
    解析一次: 注释行整行丢弃, 行尾 ``\\`` 的续行拼成一行。
    """
    instructions: dict[str, str] = {}
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buffer and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].rstrip() + " "
            continue
        buffer += line
        parts = buffer.split(None, 1)
        if parts:
            verb, value = parts[0], (parts[1] if len(parts) > 1 else "")
            # ENV 可以写多行; 合并成一个 "K=V; K=V" 串方便断言
            instructions[verb] = (
                f"{instructions[verb]}; {value}" if verb in instructions else value
            )
        buffer = ""
    return instructions


@pytest.fixture(scope="module")
def web_dockerfile() -> dict[str, str]:
    return parse_dockerfile(WEB_DOCKERFILE.read_text())


def test_web_image_still_declares_hostname_env(web_dockerfile: dict[str, str]) -> None:
    """镜像层仍要声明 HOSTNAME=0.0.0.0 (compose 里可以再覆盖)。"""
    assert "HOSTNAME=0.0.0.0" in web_dockerfile["ENV"], web_dockerfile["ENV"]
    assert "PORT=3000" in web_dockerfile["ENV"], web_dockerfile["ENV"]


def test_web_cmd_forces_hostname_before_exec(web_dockerfile: dict[str, str]) -> None:
    """CMD 必须在 exec node 之前就地导出 HOSTNAME=0.0.0.0。

    只有 ``ENV HOSTNAME=...`` 是不够的——Docker 注入的 HOSTNAME 会覆盖它。
    """
    cmd = web_dockerfile["CMD"].strip()
    assert cmd.startswith('["sh", "-c"'), f"CMD 必须经 shell 就地设置 HOSTNAME: {cmd}"
    body = re.fullmatch(r'\[\s*"sh"\s*,\s*"-c"\s*,\s*"(?P<script>[^"]*)"\s*\]', cmd)
    assert body is not None, cmd
    script = body.group("script")
    assert "node server.js" in script, script
    assert "HOSTNAME=0.0.0.0" in script, script
    # 赋值必须发生在 exec 之前, 否则 node 已经继承了 Docker 注入的 HOSTNAME
    assert script.index("HOSTNAME=0.0.0.0") < script.index("exec node"), script
    assert script.strip().startswith("HOSTNAME="), "必须紧贴 exec, 避免中间被改写"


def test_web_healthcheck_falls_back_to_container_ip(
    web_dockerfile: dict[str, str],
) -> None:
    """健康检查 127.0.0.1 失败时兜底探测容器自身 IP。"""
    healthcheck = web_dockerfile["HEALTHCHECK"]
    assert "127.0.0.1:3000" in healthcheck, healthcheck
    assert "hostname -i" in healthcheck, "必须探测容器 IP 作为兜底"
    assert "|| exit 1" in healthcheck, "两条路径都失败才算不健康"


def test_web_healthcheck_has_sane_timing(web_dockerfile: dict[str, str]) -> None:
    """给 Next 冷启动留足 start-period, 否则每次发版都会被判 unhealthy 重启。"""
    start_period = re.search(
        r"--start-period=(\d+)s", web_dockerfile["HEALTHCHECK"]
    )
    assert start_period is not None, web_dockerfile["HEALTHCHECK"]
    assert int(start_period.group(1)) >= 15, (
        "Next standalone 冷启动至少要 15s, 否则发布时容器会被误杀"
    )


@pytest.mark.parametrize("compose_name", COMPOSE_FILES)
def test_web_service_does_not_override_hostname(compose_name: str) -> None:
    """compose 里若给 web 传 HOSTNAME, 必须同样是 0.0.0.0。"""
    text = (REPO_ROOT / compose_name).read_text()
    for match in re.finditer(r"^\s*HOSTNAME[:=]\s*(\S+)\s*$", text, re.M):
        assert match.group(1) == "0.0.0.0", (
            f"{compose_name} 把 web 的 HOSTNAME 设成了 {match.group(1)}"
        )


@pytest.mark.parametrize("compose_name", COMPOSE_FILES)
def test_compose_files_still_parse(compose_name: str) -> None:
    """健康检查相关改动不能把 compose 写坏。"""
    import yaml

    data = yaml.safe_load((REPO_ROOT / compose_name).read_text())
    assert "services" in data and data["services"]
