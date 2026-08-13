#!/bin/bash
# agent-screen.sh — startet den Agent Screen (EIN Prozess): virtuelles Display
# + natives Fenster + MJPEG-Stream auf :8788.
#
# Nutzung:  ./agent-screen.sh
# Pfad:     $AGENT_SCREEN_DIR (Default: ~/.hermes/agent-screen) — dorthin hat
#           build-app.sh die App gebaut.
set -u

INSTALL_DIR="${AGENT_SCREEN_DIR:-$HOME/.hermes/agent-screen}"
APP_BUNDLE="$INSTALL_DIR/app/Agent Screen.app"
BINARY="$APP_BUNDLE/Contents/MacOS/agent-screen-app"

if [ ! -x "$BINARY" ]; then
  echo "[agent-screen] FEHLER: Binary fehlt unter $BINARY"
  echo "[agent-screen] Erst bauen: ./build-app.sh (siehe README)"
  exit 1
fi

if ! pgrep -f "agent-screen-app" > /dev/null; then
  echo "[agent-screen] Starte Agent-Screen-App …"
  "$BINARY" > /tmp/agent-screen-app.log 2>&1 &
  sleep 2
fi

if curl -s --max-time 1 http://127.0.0.1:8788/ping > /dev/null 2>&1; then
  echo "[agent-screen] Läuft ✓ — Fenster offen, Stream auf :8788"
else
  echo "[agent-screen] WARNUNG: Stream nicht erreichbar (Log: /tmp/agent-screen-app.log)"
fi
