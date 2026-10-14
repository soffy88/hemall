"""P1-7: prod compose 的 nginx 入口必须能真正起来。

审计发现：

1. `docker-compose.prod.yml` 挂 `./ssl:/etc/nginx/ssl`，但仓库根本没有 `ssl/`
   目录。compose 会把它自动建成**空目录**，nginx 直接
   `[emerg] cannot open certificate "/etc/nginx/ssl/fullchain.pem"` → 反复重启。
2. `nginx.conf:21` 的 `server_name api.hemall.example.com` 是占位符，却被 prod
   compose 当成真配置使用；商户照着配 DNS 只会指向一个不存在的名字。
3. 启动路径上没有任何 `nginx -t` 门禁 —— 配置写错时的表现是容器
   CrashLoop + 一屏 emerg，而不是一条说清"缺什么、怎么补"的错误。

本测试把三点变成不变量：
- 证书/私钥路径来自挂载路径且**可配置**，不是写死的仓库内绝对路径；
- `server_name` 来自必填环境变量，不允许占位符；模板里不得残留占位符；
- 启动入口脚本：缺证书/缺 server_name → 清晰报错并非零退出；渲染后执行
  `nginx -t` 门禁；全部通过才 exec nginx。脚本用 /bin/sh 真跑验证。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "nginx" / "nginx-entrypoint.sh"
TEMPLATES = sorted(REPO_ROOT.glob("nginx*.conf.template"))
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
MALL_COMPOSE = REPO_ROOT / "docker-compose.mall-sxueji.yml"

def _composes_with_nginx() -> list[Path]:
    out = []
    for p in (PROD_COMPOSE, MALL_COMPOSE):
        if p.is_file() and "nginx" in (_compose(p).get("services") or {}):
            out.append(p)
    assert out, "没有任何 compose 定义 nginx 服务，P1-7 的门禁形同虚设"
    return out


#: 收集在模块加载期执行，需要放在 _compose 定义之后。
COMPOSE_WITH_NGINX: list[Path] = []


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


COMPOSE_WITH_NGINX.extend(_composes_with_nginx())


def test_templates_exist():
    names = {p.name for p in TEMPLATES}
    assert "nginx.conf.template" in names, (
        "缺少 nginx.conf.template —— prod 入口配置应改为模板 (server_name/证书路径可注入)"
    )


# ── 证书路径与挂载 ─────────────────────────────────────────────────────────


def test_no_hardcoded_absolute_cert_path_in_repo_templates():
    """模板里的证书路径必须是占位符，不能是仓库里根本不存在的绝对路径。"""
    for tpl in TEMPLATES:
        # 忽略注释行 (模板头部注释会引用历史问题里的旧写法)
        text = "\n".join(
            ln for ln in tpl.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")
        )
        for m in re.finditer(r"ssl_certificate(_key)?\s+([^;]+);", text):
            value = m.group(2).strip()
            assert value.startswith("${"), (
                f"{tpl.name}: ssl_certificate 写死为 '{value}' —— "
                "应从挂载路径经环境变量注入 (audit P1-7)"
            )


def test_templates_declare_both_cert_and_key_placeholders():
    text = (REPO_ROOT / "nginx.conf.template").read_text(encoding="utf-8")
    assert re.search(r"ssl_certificate\s+\$\{[A-Z0-9_]+\};", text), "缺少证书占位符"
    assert re.search(r"ssl_certificate_key\s+\$\{[A-Z0-9_]+\};", text), "缺少私钥占位符"


def test_templates_have_no_placeholder_server_name():
    for tpl in TEMPLATES:
        text = tpl.read_text(encoding="utf-8")
        for m in re.finditer(r"^\s*server_name\s+([^;]+);", text, re.M):
            names = m.group(1).split()
            for n in names:
                assert n != "_" or tpl.name == "nginx.conf.template", (
                    f"{tpl.name}: server_name 仍是通配占位 '_'"
                )
                assert "example.com" not in n, (
                    f"{tpl.name}: server_name 仍是占位域名 '{n}' (audit P1-7/P3-9)"
                )


@pytest.mark.parametrize("compose", COMPOSE_WITH_NGINX, ids=lambda p: p.name)
def test_nginx_service_mounts_template_and_certs(compose: Path):
    data = _compose(compose)
    svc = (data.get("services") or {}).get("nginx")
    assert svc, f"{compose.name} 没有 nginx 服务"
    vols = [v.split(":")[0] for v in svc.get("volumes") or []]
    assert any(v.endswith(".conf.template") for v in vols), (
        f"{compose.name}: nginx 未挂载 *.conf.template，入口脚本无从渲染"
    )
    assert any(v.endswith("/ssl") for v in vols), (
        f"{compose.name}: nginx 未挂载证书目录，容器起来即 emerg"
    )


@pytest.mark.parametrize("compose", COMPOSE_WITH_NGINX, ids=lambda p: p.name)
def test_nginx_service_declares_tls_env_fail_closed(compose: Path):
    data = _compose(compose)
    svc = (data.get("services") or {}).get("nginx") or {}
    env_text = json_dumps(svc.get("environment"))
    for name in (
        "HEMALL_API_SERVER_NAME",
        "HEMALL_SSL_CERT_PATH",
        "HEMALL_SSL_KEY_PATH",
    ):
        assert re.search(rf"\$\{{{name}:\?", env_text), (
            f"{compose.name}: {name} 未以 ${{{name}:?...}} 必填形式注入 —— "
            "缺配置时应启动失败并给出清晰提示，而不是起一个占位域名的入口"
        )


def json_dumps(obj) -> str:

    if isinstance(obj, list):
        return "\n".join(str(x) for x in obj)
    if isinstance(obj, dict):
        return "\n".join(f"{k}={v}" for k, v in obj.items())
    return str(obj or "")


@pytest.mark.parametrize("compose", COMPOSE_WITH_NGINX, ids=lambda p: p.name)
def test_nginx_service_uses_gate_entrypoint(compose: Path):
    data = _compose(compose)
    svc = (data.get("services") or {}).get("nginx") or {}
    entry = json_dumps(svc.get("entrypoint") or svc.get("command") or "")
    assert "nginx-entrypoint.sh" in entry, (
        f"{compose.name}: nginx 未使用带证书校验 + nginx -t 门禁的入口脚本"
    )


# ── 入口脚本行为 (真跑 /bin/sh) ────────────────────────────────────────────


def _run(args, env: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    return subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), *args],
        capture_output=True,
        text=True,
        env=full,
        cwd=str(cwd or REPO_ROOT),
    )


@pytest.fixture()
def tls_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ssl"
    d.mkdir()
    (d / "fullchain.pem").write_text("-----BEGIN CERTIFICATE-----\nFAKE\n", encoding="utf-8")
    (d / "privkey.pem").write_text("-----BEGIN PRIVATE KEY-----\nFAKE\n", encoding="utf-8")
    return d


def test_entrypoint_fails_clearly_when_cert_missing(tmp_path: Path, tls_dir: Path):
    proc = _run(
        [],
        {
            "HEMALL_API_SERVER_NAME": "api.hemall.cn",
            "HEMALL_SSL_CERT_PATH": str(tls_dir / "fullchain.pem"),
            "HEMALL_SSL_KEY_PATH": str(tls_dir / "does-not-exist.pem"),
            "HEMALL_SSL_SOURCE_DIR": str(tls_dir),
            "NGINX_CONF_TEMPLATE": str(REPO_ROOT / "nginx.conf.template"),
            "NGINX_CONF_OUTPUT": str(tmp_path / "nginx.conf"),
            "NGINX_BIN": "/bin/true",
        },
    )
    assert proc.returncode != 0, "私钥缺失却仍然继续启动了 nginx"
    msg = proc.stderr + proc.stdout
    assert "does-not-exist.pem" in msg, f"错误信息没有指出缺哪个文件:\n{msg}"
    assert "privkey" in msg.lower() or "私钥" in msg, f"错误信息没有点明是私钥:\n{msg}"


def test_entrypoint_fails_clearly_when_cert_empty(tmp_path: Path, tls_dir: Path):
    (tls_dir / "empty.pem").write_text("", encoding="utf-8")
    proc = _run(
        [],
        {
            "HEMALL_API_SERVER_NAME": "api.hemall.cn",
            "HEMALL_SSL_CERT_PATH": str(tls_dir / "empty.pem"),
            "HEMALL_SSL_KEY_PATH": str(tls_dir / "privkey.pem"),
            "HEMALL_SSL_SOURCE_DIR": str(tls_dir),
            "NGINX_CONF_TEMPLATE": str(REPO_ROOT / "nginx.conf.template"),
            "NGINX_CONF_OUTPUT": str(tmp_path / "nginx.conf"),
            "NGINX_BIN": "/bin/true",
        },
    )
    assert proc.returncode != 0, "空证书文件被当成有效证书放行"
    assert "empty.pem" in (proc.stderr + proc.stdout)


def test_entrypoint_fails_when_server_name_missing(tmp_path: Path, tls_dir: Path):
    proc = _run(
        [],
        {
            "HEMALL_SSL_CERT_PATH": str(tls_dir / "fullchain.pem"),
            "HEMALL_SSL_KEY_PATH": str(tls_dir / "privkey.pem"),
            "HEMALL_SSL_SOURCE_DIR": str(tls_dir),
            "NGINX_CONF_TEMPLATE": str(REPO_ROOT / "nginx.conf.template"),
            "NGINX_CONF_OUTPUT": str(tmp_path / "nginx.conf"),
            "NGINX_BIN": "/bin/true",
        },
    )
    assert proc.returncode != 0, "缺 server_name 也能起 (会暴露一个占位域名的入口)"
    assert "HEMALL_API_SERVER_NAME" in (proc.stderr + proc.stdout)


def test_entrypoint_fails_when_ssl_dir_absent(tmp_path: Path):
    """审计原始故障：./ssl 不存在 → 容器起不来。必须给出"怎么补"的提示。"""
    proc = _run(
        [],
        {
            "HEMALL_API_SERVER_NAME": "api.hemall.cn",
            "HEMALL_SSL_CERT_PATH": "/etc/nginx/ssl/fullchain.pem",
            "HEMALL_SSL_KEY_PATH": "/etc/nginx/ssl/privkey.pem",
            "HEMALL_SSL_SOURCE_DIR": str(tmp_path / "no-such-dir"),
            "NGINX_CONF_TEMPLATE": str(REPO_ROOT / "nginx.conf.template"),
            "NGINX_CONF_OUTPUT": str(tmp_path / "nginx.conf"),
            "NGINX_BIN": "/bin/true",
        },
    )
    assert proc.returncode != 0
    msg = proc.stderr + proc.stdout
    assert "no-such-dir" in msg
    assert "certbot" in msg.lower() or "fullchain" in msg.lower(), (
        f"错误信息没有给出补救办法:\n{msg}"
    )


def test_entrypoint_renders_template_without_leftover_placeholders(
    tmp_path: Path, tls_dir: Path
):
    out = tmp_path / "nginx.conf"
    proc = _run(
        [],
        {
            "HEMALL_API_SERVER_NAME": "api.hemall.cn",
            "HEMALL_SSL_CERT_PATH": str(tls_dir / "fullchain.pem"),
            "HEMALL_SSL_KEY_PATH": str(tls_dir / "privkey.pem"),
            "HEMALL_SSL_SOURCE_DIR": str(tls_dir),
            "NGINX_CONF_TEMPLATE": str(REPO_ROOT / "nginx.conf.template"),
            "NGINX_CONF_OUTPUT": str(out),
            "NGINX_BIN": "/bin/true",
        },
    )
    assert proc.returncode == 0, proc.stderr
    text = out.read_text(encoding="utf-8")
    # 只校验注入用的 HEMALL_ 占位符；模板注释里可能出现的其它 ${...} 无关
    assert "${HEMALL_" not in text, "渲染结果里仍有未替换的 HEMALL_ 占位符"
    assert "api.hemall.cn" in text
    # nginx 自身变量 ($host 等) 必须原样保留
    assert "$host" in text, "envsubst 把 nginx 自己的变量也替换掉了"
    assert "$remote_addr" in text, "envsubst 把 nginx 自己的变量也替换掉了"
    assert "$proxy_add_x_forwarded_for" in text, "nginx 自身变量被误替换"


def test_entrypoint_runs_nginx_t_before_starting(tmp_path: Path, tls_dir: Path):
    """`nginx -t` 是审计要求的门禁：配置错就别进 CrashLoop。"""
    marker = tmp_path / "nginx-t-was-called"
    fake = tmp_path / "fake-nginx"
    # 追加而非覆盖: 脚本会先用 -t 校验再真正启动，两次都要留痕
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{marker}"\n'
        'if [ "$1" = "-t" ]; then echo "nginx: configuration file test is successful"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    proc = _run(
        [],
        {
            "HEMALL_API_SERVER_NAME": "api.hemall.cn",
            "HEMALL_SSL_CERT_PATH": str(tls_dir / "fullchain.pem"),
            "HEMALL_SSL_KEY_PATH": str(tls_dir / "privkey.pem"),
            "HEMALL_SSL_SOURCE_DIR": str(tls_dir),
            "NGINX_CONF_TEMPLATE": str(REPO_ROOT / "nginx.conf.template"),
            "NGINX_CONF_OUTPUT": str(tmp_path / "nginx.conf"),
            "NGINX_BIN": str(fake),
        },
    )
    assert proc.returncode == 0, proc.stderr
    calls = marker.read_text(encoding="utf-8")
    assert "-t" in calls, f"启动前没有执行 nginx -t，实际调用: {calls!r}"
    assert "daemon off" in calls, "nginx 未以 -g 'daemon off;' 前台运行启动"


def test_entrypoint_fails_when_nginx_t_fails(tmp_path: Path, tls_dir: Path):
    fake = tmp_path / "fake-nginx-bad"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-t" ]; then echo "nginx: [emerg] unknown directive" >&2; exit 1; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    proc = _run(
        [],
        {
            "HEMALL_API_SERVER_NAME": "api.hemall.cn",
            "HEMALL_SSL_CERT_PATH": str(tls_dir / "fullchain.pem"),
            "HEMALL_SSL_KEY_PATH": str(tls_dir / "privkey.pem"),
            "HEMALL_SSL_SOURCE_DIR": str(tls_dir),
            "NGINX_CONF_TEMPLATE": str(REPO_ROOT / "nginx.conf.template"),
            "NGINX_CONF_OUTPUT": str(tmp_path / "nginx.conf"),
            "NGINX_BIN": str(fake),
        },
    )
    assert proc.returncode != 0, "nginx -t 失败却仍然继续启动"
    assert "nginx -t" in (proc.stderr + proc.stdout) or "unknown directive" in (
        proc.stderr + proc.stdout
    )


def test_entrypoint_is_strict_mode():
    body = ENTRYPOINT.read_text(encoding="utf-8")
    assert re.search(r"^set\s+-[a-z]*e", body, re.M)
    assert re.search(r"^set\s+-[a-z]*u", body, re.M)
