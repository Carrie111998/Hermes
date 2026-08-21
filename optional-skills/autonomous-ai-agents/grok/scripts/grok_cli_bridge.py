#!/home/aoao/.hermes/hermes-agent/venv/bin/python3
"""Credential bridge for the machine-wide Grok CLI entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

from dotenv import dotenv_values


ENV_FILE = Path("/home/aoao/.hermes/.env")
REAL_GROK = Path("/mnt/f/HermesTools/grok-wsl/bin/grok")
RIGHTCODE_GROK_BASE_URL = "https://rightapi.ai/grok/v1"


def _rightcode_key_from_dotenv(env_file: Path) -> str | None:
    if not env_file.is_file():
        return None
    values = dotenv_values(env_file)
    value = values.get("RIGHTCODE_GROK_API_KEY") or values.get("RIGHTCODE_API_KEY")
    return value if isinstance(value, str) and value else None


def child_environment(
    source: Mapping[str, str], *, env_file: Path = ENV_FILE
) -> dict[str, str]:
    """Build a Grok environment without loading unrelated dotenv secrets."""
    result = dict(source)
    rightcode_key = (
        result.get("RIGHTCODE_GROK_API_KEY")
        or result.get("RIGHTCODE_API_KEY")
        or _rightcode_key_from_dotenv(env_file)
    )
    if not rightcode_key:
        return result

    result["RIGHTCODE_GROK_API_KEY"] = rightcode_key
    endpoint = result.get("GROK_MODELS_BASE_URL")
    if not endpoint or endpoint == RIGHTCODE_GROK_BASE_URL:
        # Grok CLI 1.0.4 requires XAI_API_KEY for custom-endpoint model
        # discovery. Keep the compatibility alias pinned to RightAPI so the
        # RightCode credential cannot fall through to the first-party endpoint.
        result["GROK_MODELS_BASE_URL"] = RIGHTCODE_GROK_BASE_URL
        result["XAI_API_KEY"] = rightcode_key
    return result


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = child_environment(os.environ)
    os.execve(str(REAL_GROK), [str(REAL_GROK), *arguments], environment)


if __name__ == "__main__":
    main()
