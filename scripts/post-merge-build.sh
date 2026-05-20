#!/usr/bin/env bash
# Post-merge auto-build script for hermes-agent Docker deployment.
# Intended to run on main branch after merging feat/line-adapter.
#
# What this script does:
#   1. Verify we're on main (abort otherwise)
#   2. Pull latest from origin/main
#   3. Run test suite (abort on failure)
#   4. Build Docker image
#   5. Restart gateway + dashboard containers
#
# Usage:
#   scripts/post-merge-build.sh              # full build + redeploy
#   scripts/post-merge-build.sh --skip-tests # skip test suite (emergency deploy)
#   scripts/post-merge-build.sh --dry-run    # print steps without executing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

DRY_RUN=false
SKIP_TESTS=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --skip-tests) SKIP_TESTS=true ;;
    *) echo "usage: $0 [--dry-run] [--skip-tests]" >&2; exit 1 ;;
  esac
done

run() {
  if $DRY_RUN; then
    echo -e "${YELLOW}[dry-run]${NC} $*"
  else
    echo -e "${GREEN}[exec]${NC} $*"
    "$@"
  fi
}

# ── Step 1: Verify branch ─────────────────────────────────────────────────
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "main" ]; then
  echo -e "${RED}error: must be on main branch (currently: $BRANCH)${NC}" >&2
  echo "  Run: git checkout main" >&2
  exit 1
fi
echo -e "${GREEN}✓${NC} on main branch"

# ── Step 2: Pull latest ───────────────────────────────────────────────────
run "git pull origin main"

# ── Step 3: Run tests ─────────────────────────────────────────────────────
if $SKIP_TESTS; then
  echo -e "${YELLOW}⚠${NC} skipping tests (--skip-tests)"
elif [ -x "$SCRIPT_DIR/run_tests.sh" ]; then
  echo ""
  echo "── Running test suite ──────────────────────────────────────────────"
  bash "$SCRIPT_DIR/run_tests.sh" || {
    echo -e "${RED}error: tests failed — aborting build${NC}" >&2
    exit 1
  }
else
  echo -e "${YELLOW}⚠${NC} run_tests.sh not found, running pytest directly"
  if command -v uv >/dev/null 2>&1; then
    run "uv run pytest tests/ -q --tb=short -m 'not integration' -n 4"
  fi
fi

# ── Step 4: Build Docker image ─────────────────────────────────────────────
echo ""
echo "── Building Docker image ───────────────────────────────────────────"
HERMES_UID=$(id -u)
HERMES_GID=$(id -g)
export HERMES_UID HERMES_GID

if $DRY_RUN; then
  echo -e "${YELLOW}[dry-run]${NC} docker compose build"
else
  docker compose build || {
    echo ""
    echo -e "${YELLOW}⚠${NC} Docker build failed."
    echo "  If this is a macOS keychain issue, deploy with --no-build instead:"
    echo "    docker compose up -d --no-build"
    exit 1
  }
fi

# ── Step 5: Restart containers ─────────────────────────────────────────────
echo ""
echo "── Restarting containers ───────────────────────────────────────────"
run "docker compose up -d --no-build"

# ── Step 6: Health check ──────────────────────────────────────────────────
echo ""
echo "── Waiting for health check ────────────────────────────────────────"
if $DRY_RUN; then
  echo -e "${YELLOW}[dry-run]${NC} health check (skipped)"
else
  for i in $(seq 1 15); do
    STATUS=$(docker inspect gateway --format '{{.State.Health.Status}}' 2>/dev/null || echo "unknown")
    if [ "$STATUS" = "healthy" ]; then
      echo -e "${GREEN}✓${NC} gateway healthy"
      break
    fi
    echo "  ($i/15) gateway status: $STATUS — waiting..."
    sleep 2
  done
  if [ "$STATUS" != "healthy" ]; then
    echo -e "${RED}error: gateway health check failed (status: $STATUS)${NC}" >&2
    exit 1
  fi
fi

echo ""
echo -e "${GREEN}── Build complete ────────────────────────────────────────────────${NC}"
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E 'gateway|dashboard' || true
