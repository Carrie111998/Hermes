#!/bin/bash
# Hermes Desktop launcher — single instance, no restart loop.
# Kills stale processes + systemd scopes, shows window immediately on Linux/Wayland.

SENTINEL="/tmp/hermes-desktop-quit.sentinel"
PIDFILE="/tmp/hermes-desktop.pid"
DESKTOP_DIR="$HOME/.hermes/hermes-agent/apps/desktop"
ELECTRON="$HOME/.hermes/hermes-agent/node_modules/.bin/electron"

# GPU stays enabled but merged into main process via --in-process-gpu (below).
# --single-process (triggered by HERMES_DESKTOP_DISABLE_GPU=1) SIGTRAPs on kernel 6.17+.
export HERMES_DESKTOP_DISABLE_GPU=
export ELECTRON_OZONE_PLATFORM_HINT=auto

# Stale-dist guard: rebuild if main process source is newer than dist bundle.
# ponytail: one sentinel file (electron/main.ts) vs dist. npm run build
# rebuilds both main + renderer together, so a single check catches all.
cd "$DESKTOP_DIR" || exit 1

# Merge-artifact guard: check for *** / conflict markers before rebuilding.
# These come from git stash merge during hermes update — upstream's ***
# placeholders leak into valid variable names. Restore from git if found.
MAIN_TS="$DESKTOP_DIR/electron/main.ts"

# HTML default guard: Hermes.desktop claims text/html in MimeType, it overrides
# Chrome as the default HTML handler after desktop-file scans. Strip it here so
# it auto-heals on the next Hermes launch if an update re-introduces the claim.
HERMES_DESKTOP_FILE="$HOME/.local/share/applications/Hermes.desktop"
if grep -q 'text/html' "$HERMES_DESKTOP_FILE" 2>/dev/null; then
  sed -i 's/;text\/html//g; s/text\/html;//g; s/^MimeType=;/MimeType=/g' "$HERMES_DESKTOP_FILE"
fi
if grep -q '<<<<<<<\|>>>>>>>\|token: \*\*\*\|wsUrl.*token=\*\*\*\|supportsPassword: \*\*\*' "$MAIN_TS" 2>/dev/null; then
  echo "[hermes-launcher] Merge artifacts detected in main.ts — restoring clean copy..."
  git checkout HEAD -- "$MAIN_TS" 2>/dev/null
  echo "[hermes-launcher] Restored. Rebuilding..."
fi

if [ "electron/main.ts" -nt "dist/electron-main.mjs" ] 2>/dev/null; then
  echo "[hermes-launcher] Source newer than dist — rebuilding..."
  npm run build 2>&1 | tail -5
  echo "[hermes-launcher] Rebuild complete."
fi

# NVIDIA driver mismatch guard — detect loaded vs on-disk version mismatch
if [ -f /proc/driver/nvidia/version ] && command -v modinfo >/dev/null 2>&1; then
  LOADED=$(sed -n 's/.*Kernel Module  *\([0-9.]*\) .*/\1/p' /proc/driver/nvidia/version 2>/dev/null | head -1)
  ONDISK=$(modinfo -F version nvidia 2>/dev/null | head -1)
  if [ -n "$LOADED" ] && [ -n "$ONDISK" ] && [ "$LOADED" != "$ONDISK" ]; then
    echo "[hermes-launcher] ERROR: NVIDIA driver mismatch — loaded: $LOADED, on-disk: $ONDISK."
    echo "[hermes-launcher] Reboot required. GPU-dependent apps cannot start."
    exit 1
  fi
fi

# Cleanup before launch — kill any previous hermes electron/processes
kill -9 $(ps aux | grep 'hermes-agent.*electron' | grep -v grep | awk '{print $2}') 2>/dev/null
kill -9 $(ps aux | grep 'electron.*desktop' | grep -v grep | awk '{print $2}') 2>/dev/null
systemctl --user reset-failed 2>/dev/null
# Kill stale systemd scopes via their PIDs
for pid in $(systemctl --user show 'app-org.chromium.Chromium-*.scope' -p MainPID --value 2>/dev/null | grep -v '^0$'); do
  kill $pid 2>/dev/null
done
sleep 1
systemctl --user kill app-org.chromium.Chromium-*.scope 2>/dev/null
systemctl --user reset-failed 2>/dev/null
rm -f /tmp/hermes-desktop*.pid /tmp/hermes-desktop-quit.sentinel

# PID lock
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Already running (PID $(cat "$PIDFILE")). Exiting."
  exit 0
fi
echo $$ > "$PIDFILE"

cleanup() {
  rm -f "$SENTINEL" "$PIDFILE"
  exit 0
}
trap cleanup EXIT INT TERM

cd "$DESKTOP_DIR" && "$ELECTRON" . --no-sandbox --in-process-gpu --disable-gpu-compositing --disable-dev-shm-usage --no-zygote --disable-features=UseSystemdServiceManager --ozone-platform-hint=auto --enable-features=UseOzonePlatform
