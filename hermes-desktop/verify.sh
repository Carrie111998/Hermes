#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

pass_count=0
fail_count=0
warn_count=0

say() {
  if [ "$QUIET" -eq 0 ]; then
    printf "%b%s%b %s\n" "$1" "$2" "$NC" "$3"
  fi
  return 0
}
pass() { pass_count=$((pass_count + 1)); say "$GREEN" "PASS" "$1"; return 0; }
warn() { warn_count=$((warn_count + 1)); say "$YELLOW" "WARN" "$1"; return 0; }
fail_check() { fail_count=$((fail_count + 1)); say "$RED" "FAIL" "$1"; return 0; }

cd "$(dirname "$0")"
[ -f .env ] && . ./.env

container="hermes-desktop"
novnc_port="${NOVNC_PORT:-6080}"

if ! command -v docker >/dev/null 2>&1; then
  fail_check "Docker is not installed. Install Docker first."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  fail_check "Docker Compose v2 is not installed. Install the Docker Compose plugin."
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  fail_check "Docker is installed but not running. Start Docker and retry."
  exit 1
fi
pass "Docker and Docker Compose are available"

if docker ps --format '{{.Names}}' | grep -qx "$container"; then
  pass "Container ${container} is running"
else
  fail_check "Container ${container} is not running. Fix: docker compose up -d desktop"
fi

if docker ps --format '{{.Names}}' | grep -qx "$container"; then
  novnc_ok=0
  if command -v curl >/dev/null 2>&1; then
    if curl -I -fsS "http://127.0.0.1:${novnc_port}/vnc.html" >/dev/null 2>&1; then
      novnc_ok=1
    fi
  fi
  if [ "$novnc_ok" -ne 1 ] && docker exec "$container" sh -lc 'pgrep -af websockify >/dev/null 2>&1' >/dev/null 2>&1; then
    warn "noVNC HTTP probe failed in this environment, but websockify is running. Try opening http://localhost:${novnc_port}/vnc.html manually."
    novnc_ok=2
  fi
  if [ "$novnc_ok" -eq 1 ]; then
    pass "noVNC responds on http://localhost:${novnc_port}/vnc.html"
  elif [ "$novnc_ok" -eq 0 ]; then
    fail_check "noVNC is not reachable on port ${novnc_port}. Fix: check port conflicts and docker logs ${container}."
  fi
else
  warn "Skipping noVNC HTTP check because the container is not running"
fi

if docker ps --format '{{.Names}}' | grep -qx "$container"; then
  if docker exec "$container" bash -lc 'pgrep -u hermes -f "Xtigervnc :1" >/dev/null || pgrep -u hermes -f "Xvnc :1" >/dev/null' >/dev/null 2>&1; then
    pass "VNC server process is running"
  else
    fail_check "VNC server process is not running. Fix: docker logs ${container}."
  fi

  if docker exec "$container" bash -lc 'pgrep -u hermes -f "/usr/bin/google-chrome|/opt/google/chrome/chrome|chromium" >/dev/null' >/dev/null 2>&1; then
    pass "Browser process is running"
  else
    warn "Browser is not currently running. This is OK before first use. Start it with /home/hermes/bin/open-chrome or from the desktop icon."
  fi

  if docker exec "$container" su - hermes -c 'env HOME=/home/hermes DISPLAY=:1 XAUTHORITY=/home/hermes/.Xauthority /usr/bin/xdotool getdisplaygeometry >/dev/null 2>&1' >/dev/null 2>&1; then
    pass "Desktop display :1 accepts xdotool control"
  else
    fail_check "Desktop display :1 is not controllable. Fix: docker restart ${container}, then check docker logs ${container}."
  fi

  if docker exec "$container" bash -lc 'install -d -m 700 /home/hermes/.config/google-chrome && test -w /home/hermes/.config/google-chrome' >/dev/null 2>&1; then
    pass "Browser profile directory is writable and persistent under /home/hermes"
  else
    fail_check "Browser profile directory is not writable. Fix volume permissions for desktop_home."
  fi

  if docker exec "$container" bash -lc 'test -x /usr/bin/xdotool && test -x /usr/bin/scrot' >/dev/null 2>&1; then
    pass "Desktop control tools are installed: xdotool and scrot"
  else
    fail_check "xdotool or scrot missing. Rebuild image: docker compose build --no-cache desktop"
  fi
fi

if docker compose exec -T --user hermes desktop cua-driver-desktop --version >/dev/null 2>&1; then
  pass "The official cua-driver is installed in the desktop container"
else
  fail_check "cua-driver is unavailable. Fix: docker compose build --no-cache desktop"
fi

if [ "$QUIET" -eq 0 ]; then
  printf "\nSummary: %s pass, %s warn, %s fail\n" "$pass_count" "$warn_count" "$fail_count"
fi

[ "$fail_count" -eq 0 ]
