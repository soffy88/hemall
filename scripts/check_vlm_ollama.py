#!/usr/bin/env python3
"""手动冒烟: 验证 Ollama MiniCPM-V 真模型可达且能识货 (不进 CI/pytest)。

前置: ubuntu26 宿主机 Ollama 已部署 minicpm-v47-1b-local:latest。

跑法 (仓库根目录, 用项目 uv 环境):
    ./deploy-tools/uv run python scripts/check_vlm_ollama.py <商品图路径>

配置走 env (与 app.config 的 Settings 同名):
    HEMALL_VLM_OLLAMA_BASE_URL  默认 http://127.0.0.1:11434 (宿主机视角);
                                api 容器内验证时填 http://172.17.0.1:11434
    HEMALL_VLM_OLLAMA_MODEL     默认 minicpm-v47-1b-local:latest
    HEMALL_VLM_OLLAMA_TIMEOUT_S 默认 120

成功时打印 identify_product 的 JSON 结果; 模型不可达/识别失败会抛异常并
以非零码退出——冒烟就是要看到真实失败, 不做任何伪造兜底。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ext.vlm_ollama_provider import OllamaVLMProvider  # noqa: E402


async def _main(image_path: Path) -> None:
    provider = OllamaVLMProvider(
        base_url=os.environ.get("HEMALL_VLM_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        model=os.environ.get(
            "HEMALL_VLM_OLLAMA_MODEL", "minicpm-v47-1b-local:latest"
        ),
        timeout_s=float(os.environ.get("HEMALL_VLM_OLLAMA_TIMEOUT_S", "120")),
    )
    result = await provider.identify_product(image_bytes=image_path.read_bytes())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    asyncio.run(_main(Path(sys.argv[1])))
