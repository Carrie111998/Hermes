#!/usr/bin/env bash
# Install an independently built Charterforge checkout or artifact.
#
# This is intentionally local-source based: it never fetches or executes the
# upstream Hermes installer. Set CHARTERFORGE_SOURCE to a wheel, sdist, or
# checkout, and CHARTERFORGE_INSTALL_DIR to choose the isolated venv location.

set -euo pipefail

SOURCE="${CHARTERFORGE_SOURCE:-${1:-$PWD}}"
PREFIX="${CHARTERFORGE_INSTALL_DIR:-${HOME}/.local/share/charterforge}"
# The project deliberately caps Python below 3.14; ask uv for a compatible
# interpreter instead of silently creating an unsupported environment from the
# host's `python3` alias.
PYTHON="${CHARTERFORGE_PYTHON:-3.13}"

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' "charterforge installer requires uv (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

if [[ ! -e "$SOURCE" ]]; then
  printf 'source does not exist: %s\n' "$SOURCE" >&2
  exit 1
fi

if [[ -e "$PREFIX" && ! -x "$PREFIX/bin/python" && -n "$(find "$PREFIX" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
  printf 'refusing to reuse non-venv install directory: %s\n' "$PREFIX" >&2
  exit 1
fi

mkdir -p "$(dirname "$PREFIX")"
if [[ ! -x "$PREFIX/bin/python" ]]; then
  uv venv "$PREFIX" --python "$PYTHON"
fi

uv pip install --python "$PREFIX/bin/python" --upgrade "$SOURCE"

printf '\nCharterforge installed successfully.\n'
printf 'CLI: %s\n' "$PREFIX/bin/charterforge"
printf 'State: %s\n' "${CHARTERFORGE_HOME:-$HOME/.charterforge}"
printf 'Verify: CHARTERFORGE_HOME=%s %s --version\n' \
  "${CHARTERFORGE_HOME:-$HOME/.charterforge}" "$PREFIX/bin/charterforge"
