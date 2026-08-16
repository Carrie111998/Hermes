#!/usr/bin/env bash
set -euo pipefail

export TZ=Asia/Taipei
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV_PY="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/bin/python}"
RELEASE_PATH="@RELEASE_PATH@"
RELEASE_PYTHON="@PYTHON@"

if [[ ! -d "$RELEASE_PATH" ]]; then
  RELEASE_PATH="$HERMES_HOME/hermes-agent"
fi
export PYTHONPATH="$RELEASE_PATH${PYTHONPATH:+:$PYTHONPATH}"

if [[ "$RELEASE_PYTHON" != "@PYTHON@" && -x "$RELEASE_PYTHON" ]]; then
  VENV_PY="$RELEASE_PYTHON"
fi

if [[ ! -x "$VENV_PY" ]]; then
  VENV_PY="${PYTHON:-python3}"
fi

exec "$VENV_PY" -m hermes_cli.calendar_guard --check