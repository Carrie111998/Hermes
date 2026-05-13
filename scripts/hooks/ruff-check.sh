#!/usr/bin/env bash
# Pre-commit hook helper: find ruff in venv or PATH, run check --fix.
# lint-staged passes staged filenames as arguments.
set -euo pipefail

if command -v ruff &>/dev/null; then
  exec ruff check --fix "$@"
fi

# Common venv locations for this repo
for candidate in .venv/bin/ruff venv/bin/ruff; do
  if [ -x "$candidate" ]; then
    exec "$candidate" check --fix "$@"
  fi
done

echo "ERROR: ruff not found. Install dev deps: uv pip install -e '.[dev]'" >&2
exit 1
