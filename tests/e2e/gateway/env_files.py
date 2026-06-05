"""Tiny stdlib ``.env`` loader for the gateway e2e suite.

The matrix is driven entirely by environment variables (see ``providers.py``).
To avoid pasting keys onto the command line every run, the suite loads a single
``.env.test`` file from the repo root before discovering the matrix.

``.env.test`` is gitignored — it holds your real provider keys. The real
process environment always wins: a variable already set in your shell is never
overwritten by the file. No third-party dependency — the parser handles
``KEY=value``, ``export KEY=value``, ``#`` comments, blank lines, and
single/double-quoted values.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_TEST_FILE = ".env.test"


def _parse(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs[key] = value
    return pairs


def load_test_env(base_dir: Path) -> None:
    """Load ``base_dir/.env.test`` into the environment without clobbering it.

    setdefault semantics: a variable already present in the environment (your
    shell) takes precedence over the file.
    """
    path = base_dir / ENV_TEST_FILE
    if not path.is_file():
        return
    for key, value in _parse(path.read_text(encoding="utf-8")).items():
        os.environ.setdefault(key, value)
