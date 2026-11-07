"""P0-16: release images must be immutable and traceable to one git commit."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILD_RELEASE = ROOT / "scripts" / "build-release.sh"
BACKEND_DOCKERFILE = ROOT / "Dockerfile.prod"
FRONTEND_DOCKERFILE = ROOT / "web" / "Dockerfile"


def _fake_tools(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"

    def write(name: str, body: str) -> None:
        path = bindir / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    write(
        "docker",
        f"""
echo "docker $*" >> "{log}"
case "$*" in
  *"image inspect"*)
    printf '%s' "$HEMALL_FAKE_REVISION"
    ;;
esac
exit 0
""",
    )
    write("git", 'echo "git $*" >> "$HEMALL_FAKE_LOG"\nexit 0')
    return bindir


def test_dockerfiles_record_the_source_revision():
    for path in (BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE):
        text = path.read_text(encoding="utf-8")
        assert "ARG GIT_SHA" in text
        assert 'org.opencontainers.image.revision="${GIT_SHA}"' in text


def test_build_script_is_executable_and_uses_strict_shell_mode():
    assert BUILD_RELEASE.is_file()
    assert os.access(BUILD_RELEASE, os.X_OK)
    text = BUILD_RELEASE.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "latest" not in text


def test_release_compose_is_pinned_to_full_sha_tags(tmp_path):
    bindir = _fake_tools(tmp_path)
    out = tmp_path / "release"
    sha = "a" * 40
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}{os.pathsep}{env['PATH']}",
            "HEMALL_RELEASE_GIT_SHA": sha,
            "HEMALL_RELEASE_OUTPUT_DIR": str(out),
            "HEMALL_FAKE_LOG": str(tmp_path / "calls.log"),
            "HEMALL_FAKE_REVISION": sha,
        }
    )
    proc = subprocess.run(
        ["bash", str(BUILD_RELEASE)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    compose = yaml.safe_load(
        (out / sha / "docker-compose.release.yml").read_text(encoding="utf-8")
    )
    assert compose["services"]["api"]["image"].endswith(f":sha-{sha}")
    assert compose["services"]["web"]["image"].endswith(f":sha-{sha}")

    release_env = (out / sha / "release.env").read_text(encoding="utf-8")
    assert f"GIT_SHA={sha}" in release_env
    assert "BACKEND_IMAGE=" in release_env
    assert "FRONTEND_IMAGE=" in release_env


def test_build_script_rejects_floating_or_short_revisions():
    text = BUILD_RELEASE.read_text(encoding="utf-8")
    assert re.search(r"\^\[0-9a-f\]\{40\}\$", text)
    assert "docker buildx build" in text
    assert '--build-arg "GIT_SHA=${GIT_SHA}"' in text


def test_deploy_composes_pin_the_known_production_baseline():
    for name in ("docker-compose.deploy.yml", "docker-compose.mall-old.yml"):
        data = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
        services = data["services"]
        assert services["api"]["image"] == (
            "registry.sxueji.com/hemall/backend:0.6.18-prod"
        )
        assert services["web"]["image"] == (
            "registry.sxueji.com/hemall/frontend:0.5.10-prod"
        )


def test_rollback_override_is_pinned_to_the_known_production_baseline():
    path = ROOT / "ops" / "release" / "docker-compose.rollback-baseline.yml"
    services = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]
    assert services["api"]["image"] == (
        "registry.sxueji.com/hemall/backend:0.6.18-prod"
    )
    assert services["web"]["image"] == (
        "registry.sxueji.com/hemall/frontend:0.5.10-prod"
    )
