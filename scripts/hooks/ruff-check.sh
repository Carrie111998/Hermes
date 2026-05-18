#!/usr/bin/env bash
# Pre-commit hook helper: find ruff in venv or PATH, run check --fix.
# lint-staged passes staged filenames as arguments.
set -euo pipefail

# Prefer project venv first for consistent results across contributors.
for candidate in .venv/bin/ruff venv/bin/ruff; do
  if [ -x "$candidate" ]; then
    exec "$candidate" check --fix --force-exclude -- "$@"
  fi
done

# Fall back to PATH (global install).
if command -v ruff &>/dev/null; then
  exec ruff check --fix --force-exclude -- "$@"
fi

echo "ERROR: ruff not found. Install dev deps: uv pip install -e '.[dev]'" >&2
exit 1
