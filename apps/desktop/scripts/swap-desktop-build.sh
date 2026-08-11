#!/usr/bin/env bash
# Swap the rebuilt Hermes desktop into the live install dir.
# Run AFTER the Hermes desktop app has fully quit (it locks win-unpacked).
set -euo pipefail

cd /c/Users/diego/.hermes/agent-src/apps/desktop

NEW="release/win-unpacked-new/win-unpacked"
LIVE="release/win-unpacked"

# Safety: refuse if the desktop is still running
if powershell -NoProfile -Command "Get-Process -Id 31464 -ErrorAction SilentlyContinue" 2>/dev/null | grep -q Hermes; then
  echo "ERROR: Hermes desktop (PID 31464) is still running. Quit it first." >&2
  exit 1
fi
if tasklist 2>/dev/null | grep -qi "Hermes.exe"; then
  echo "ERROR: Hermes.exe is still running. Quit the desktop app first." >&2
  exit 1
fi

echo "Backing up live win-unpacked -> release/win-unpacked-live-bak-$(date +%Y%m%d_%H%M%S)"
mv "$LIVE" "$LIVE-live-bak-$(date +%Y%m%d_%H%M%S)"

echo "Installing new build"
mv "$NEW" "$LIVE"

echo "Verify:"
ls -la "$LIVE/resources/" | head
grep -c "bridge_provider" "$LIVE/resources/app.asar.unpacked/dist/assets/"*.js 2>/dev/null | head -1 || true

echo "DONE. Relaunch the Hermes desktop app."
