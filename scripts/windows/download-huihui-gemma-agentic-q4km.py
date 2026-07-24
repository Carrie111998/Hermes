"""Download Huihui gemma agentic Q4_K_M into HF cache with tqdm progress."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_CACHE", r"H:\elt_data\hf-cache")
os.environ.setdefault("HF_HOME", r"H:\elt_data\hf-cache")

from huggingface_hub import hf_hub_download  # noqa: E402

REPO = "mradermacher/Huihui-gemma-4-12B-agentic-fable5-abliterated-GGUF"
FILENAME = "Huihui-gemma-4-12B-agentic-fable5-abliterated.Q4_K_M.gguf"
META = Path.home() / ".hermes" / "logs" / "llama-hf-download" / "hf-hub-huihui-latest.json"


def main() -> int:
    META.parent.mkdir(parents=True, exist_ok=True)
    print(f"repo={REPO}", flush=True)
    print(f"file={FILENAME}", flush=True)
    print(f"cache={os.environ['HF_HUB_CACHE']}", flush=True)
    path = hf_hub_download(
        repo_id=REPO,
        filename=FILENAME,
        resume_download=True,
    )
    p = Path(path)
    size_gb = round(p.stat().st_size / (1024**3), 2)
    print(f"DOWNLOADED {p}", flush=True)
    print(f"SIZE_GB {size_gb}", flush=True)
    META.write_text(
        json.dumps(
            {
                "repo": REPO,
                "file": FILENAME,
                "path": str(p),
                "size_gb": size_gb,
                "model_id": "Huihui-gemma-4-12B-agentic-fable5-Q4_K_M",
                "hf_tag": f"{REPO}:Q4_K_M",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"meta={META}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
