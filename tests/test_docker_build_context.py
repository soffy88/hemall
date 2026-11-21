"""P0-1: 构建上下文完整性——.dockerignore 不得排除 Dockerfile* 显式 COPY 的文件。

审计发现：`.dockerignore` 的 `*.md` 把 `README.md` 排除出构建上下文，而
`Dockerfile.prod:37` 有 `COPY pyproject.toml uv.lock README.md ./`，
builder 阶段直接报 `COPY failed: file not found in build context`，
任何 prod / mall 构建必断。

这里用一个与 dockerignore 语义一致的最小匹配器做静态校验：
解析 Dockerfile 中**不带 --from 的 COPY** 源路径，逐个判定它是否会被
.dockerignore 规则排除（支持 `*`、`**`、目录后缀、否定规则 `!`）。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _parse_dockerignore(path: Path) -> list[tuple[str, bool]]:
    """返回 [(pattern, negated)]，跳过空行与 # 注释。"""
    rules: list[tuple[str, bool]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:].strip() if negated else line
        rules.append((pattern, negated))
    return rules


def _pattern_matches(pattern: str, rel: str) -> bool:
    """dockerignore 语义近似：尾部无斜杠的目录模式也匹配其下所有文件。"""
    pat = pattern.rstrip("/")
    candidates = {rel, f"{pat}/{rel}" if not rel.startswith(pat) else rel}
    for _cand in candidates:
        if fnmatch.fnmatch(rel, pattern):
            return True
        if fnmatch.fnmatch(rel, f"{pattern}*"):
            return True
    # 目录模式：`edge_bridge/` 排除其下所有内容
    for seg_prefix in _ancestor_dirs(rel):
        if fnmatch.fnmatch(seg_prefix, pattern) or fnmatch.fnmatch(seg_prefix, f"{pattern}*"):
            return True
    return False


def _ancestor_dirs(rel: str) -> list[str]:
    parts = rel.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def is_excluded(rel: str, rules: list[tuple[str, bool]]) -> bool:
    """按顺序应用规则：最后一条匹配者决定（否定规则恢复包含）。"""
    excluded = False
    for pattern, negated in rules:
        if _pattern_matches(pattern, rel):
            excluded = not negated
    return excluded


_COPY_RE = re.compile(
    r"^\s*COPY\s+(?P<args>.*)$",
    re.IGNORECASE,
)


def _dockerfile_copy_sources(dockerfile: Path) -> list[str]:
    """收集 Dockerfile 中来自构建上下文的 COPY 源（跳过 --from= 阶段引用）。"""
    sources: list[str] = []
    # 支持行连续（`COPY a b \` + `    c ./`）
    pending = ""
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip() if raw.lstrip().startswith("#") else raw
        line = line.strip()
        if pending:
            pending += " " + line
        else:
            m = _COPY_RE.match(line)
            if not m:
                continue
            pending = m.group("args")
        if pending.endswith("\\"):
            pending = pending[:-1].strip()
            continue
        args = pending
        pending = ""
        # 多行展开后重新匹配
        m = _COPY_RE.match("COPY " + args)
        if not m:
            continue
        tokens = [t for t in m.group("args").split() if not t.startswith("--")]
        if any(a.startswith("--from=") for a in tokens):
            continue  # 阶段间复制，不依赖构建上下文
        # JSON 数组形式
        if tokens and tokens[0].startswith("["):
            inner = tokens[0].lstrip("[").rstrip("]").strip('"')
            tokens = [t.strip('"') for t in inner.split(",") if t.strip()]
        for tok in tokens[:-1]:  # 最后一个是目标
            for part in tok.split(":"):
                if part.startswith("/") or part.startswith("-"):
                    continue
                sources.append(part)
    return sources


DOCKERFILES = sorted(
    p
    for p in REPO_ROOT.glob("Dockerfile*")
    if p.is_file() and p.name not in {"Dockerfile.edge-bridge"}
)


def test_dockerfiles_exist():
    assert DOCKERFILES, "未找到任何 Dockerfile"


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_copy_sources_not_excluded_by_dockerignore(dockerfile: Path):
    rules = _parse_dockerignore(DOCKERIGNORE)
    excluded = [src for src in _dockerfile_copy_sources(dockerfile) if is_excluded(src, rules)]
    assert not excluded, (
        f"{dockerfile.name} COPY 的源被 .dockerignore 排除，docker build 会失败: "
        f"{sorted(set(excluded))}"
    )


def test_readme_exception_present():
    """回归锚点：README.md 必须显式从 *.md 例外中恢复。"""
    rules = _parse_dockerignore(DOCKERIGNORE)
    assert is_excluded("README.md", rules) is False, (
        "README.md 必须通过 .dockerignore 的 `!README.md` 例外进入构建上下文"
    )


def test_dockerfile_prod_copies_readme():
    """P0-1 的直接触发条件：`COPY ... README.md` 必须仍然在 Dockerfile.prod 中。"""
    text = (REPO_ROOT / "Dockerfile.prod").read_text(encoding="utf-8")
    assert "README.md" in text
