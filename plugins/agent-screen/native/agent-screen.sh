#!/bin/bash
# agent-screen.sh — start Agent Screen (one process): virtual display
# + native window + MJPEG stream on loopback :8788.
#
# Usage:  ./agent-screen.sh
# Install dir is ~/.hermes/agent-screen (written by build-app.sh).
set -u

INSTALL_DIR="${HOME}/.hermes/agent-screen"
APP_BUNDLE="$INSTALL_DIR/app/Agent Screen.app"
BINARY="$APP_BUNDLE/Contents/MacOS/agent-screen-app"

if [ ! -x "$BINARY" ]; then
  echo "[agent-screen] ERROR: binary missing at $BINARY" >&2
  echo "[agent-screen] Build first: ./build-app.sh (see README)" >&2
  exit 1
fi

# Exact process-name match. Never pgrep -f — that kills editors / compilers
# whose argv happens to contain "agent-screen-app".
if ! pgrep -x "agent-screen-app" > /dev/null; then
  echo "[agent-screen] starting Agent Screen…"
  "$BINARY" > /tmp/agent-screen-app.log 2>&1 &
  sleep 2
fi

if curl -s --max-time 1 http://127.0.0.1:8788/ping > /dev/null 2>&1; then
  echo "[agent-screen] running — window open, stream on 127.0.0.1:8788"
else
  echo "[agent-screen] WARNING: stream not reachable (log: /tmp/agent-screen-app.log)" >&2
fi
