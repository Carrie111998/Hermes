#!/usr/bin/env bash
# ============================================================================
# Hermes Agent installer (PC) — Mongo remote mode
# ============================================================================
# Installs Hermes on this machine and wires bootstrap.yaml + X.509 certs
# so the agent talks to the control-plane MongoDB (no local brain).
#
# Prerequisites:
#   - Control plane already installed (scripts/install-control-plane.sh)
#   - Enrollment bundle for THIS PC (from enroll-agent.sh)
#
# Usage:
#   ./scripts/install-agent.sh --bundle /path/to/home-pc
#   ./scripts/install-agent.sh --bundle ./home-pc.tar.gz
#   ./scripts/install-agent.sh --enroll-url https://cp:8743/enroll --token SECRET --name home-pc
#   ./scripts/install-agent.sh --bundle /path/to/home-pc --skip-hermes-install
#
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE=""
ENROLL_URL=""
ENROLL_TOKEN=""
AGENT_NAME=""
PROFILE="default"
SKIP_HERMES=0
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle) BUNDLE="$2"; shift 2 ;;
    --enroll-url) ENROLL_URL="$2"; shift 2 ;;
    --token) ENROLL_TOKEN="$2"; shift 2 ;;
    --name) AGENT_NAME="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --hermes-home) HERMES_HOME_DIR="$2"; shift 2 ;;
    --skip-hermes-install) SKIP_HERMES=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo ""
echo "⚕ Hermes Agent installer (PC)"
echo "  HERMES_HOME=$HERMES_HOME_DIR"
echo ""

mkdir -p "$HERMES_HOME_DIR/certs"

# --- Obtain enrollment bundle ---
TMP_BUNDLE=""
if [[ -n "$ENROLL_URL" ]]; then
  [[ -n "$ENROLL_TOKEN" && -n "$AGENT_NAME" ]] || {
    echo "--enroll-url requires --token and --name"; exit 1
  }
  TMP_BUNDLE=$(mktemp -d)
  echo "→ Requesting enrollment from $ENROLL_URL …"
  curl -fsSL -X POST "$ENROLL_URL" \
    -H "Authorization: Bearer $ENROLL_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$AGENT_NAME\",\"profile\":\"$PROFILE\"}" \
    -o "$TMP_BUNDLE/bundle.tar.gz"
  mkdir -p "$TMP_BUNDLE/out"
  tar -xzf "$TMP_BUNDLE/bundle.tar.gz" -C "$TMP_BUNDLE/out"
  # tar contains <name>/...
  BUNDLE=$(find "$TMP_BUNDLE/out" -mindepth 1 -maxdepth 1 -type d | head -1)
elif [[ -n "$BUNDLE" ]]; then
  if [[ -f "$BUNDLE" && "$BUNDLE" == *.tar.gz ]]; then
    TMP_BUNDLE=$(mktemp -d)
    tar -xzf "$BUNDLE" -C "$TMP_BUNDLE"
    BUNDLE=$(find "$TMP_BUNDLE" -mindepth 1 -maxdepth 1 -type d | head -1)
  elif [[ -f "$BUNDLE" && "$BUNDLE" == *.zip ]]; then
    TMP_BUNDLE=$(mktemp -d)
    unzip -q "$BUNDLE" -d "$TMP_BUNDLE"
    BUNDLE=$(find "$TMP_BUNDLE" -mindepth 1 -maxdepth 1 -type d | head -1)
  elif [[ ! -d "$BUNDLE" ]]; then
    echo "Bundle not found: $BUNDLE"; exit 1
  fi
else
  echo "Provide --bundle DIR|archive  OR  --enroll-url + --token + --name"
  exit 1
fi

[[ -f "$BUNDLE/bootstrap.yaml" ]] || {
  echo "bootstrap.yaml missing in bundle"; exit 1
}

echo "→ Installing bootstrap + certs into $HERMES_HOME_DIR"
cp "$BUNDLE/bootstrap.yaml" "$HERMES_HOME_DIR/bootstrap.yaml"
if [[ -d "$BUNDLE/certs" ]]; then
  cp -f "$BUNDLE/certs/"* "$HERMES_HOME_DIR/certs/" 2>/dev/null || true
fi
chmod 600 "$HERMES_HOME_DIR/bootstrap.yaml" 2>/dev/null || true
chmod 600 "$HERMES_HOME_DIR/certs/agent.pem" 2>/dev/null || true

export HERMES_HOME="$HERMES_HOME_DIR"

# --- Install Hermes code/deps ---
if [[ $SKIP_HERMES -eq 0 ]]; then
  if [[ -x "$ROOT/setup-hermes.sh" ]]; then
    echo "→ Running setup-hermes.sh (local checkout)…"
    (cd "$ROOT" && ./setup-hermes.sh) || true
  elif [[ -f "$ROOT/scripts/install.sh" ]]; then
    echo "→ Running scripts/install.sh…"
    bash "$ROOT/scripts/install.sh" || true
  else
    echo "→ No local installer found; assuming hermes is already on PATH."
  fi
fi

if command -v hermes >/dev/null 2>&1; then
  echo "→ hermes storage status"
  hermes storage status || true
  echo "→ Registering cluster presence"
  hermes cluster status || true
else
  echo "NOTE: 'hermes' not on PATH yet — open a new shell, then run:"
  echo "  hermes storage status"
fi

[[ -n "$TMP_BUNDLE" ]] && rm -rf "$TMP_BUNDLE"

echo ""
echo "✓ Agent PC configured for Mongo remote storage."
echo "  Auth: see auth_mode in $HERMES_HOME_DIR/bootstrap.yaml (prefer x509)."
echo "  Only this file + certs/ are local; memory/skills/sessions live in Mongo."
echo ""
