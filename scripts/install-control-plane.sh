#!/usr/bin/env bash
# ============================================================================
# Hermes Control Plane installer
# ============================================================================
# Installs MongoDB replica set (HA) + TLS CA used to authorize agent PCs.
# Run this ONCE on the server / always-on machine that hosts the database.
#
# Usage:
#   ./scripts/install-control-plane.sh
#   ./scripts/install-control-plane.sh --with-enroll-api
#   ./scripts/install-control-plane.sh --enroll home-pc
#
# After install, enroll each agent PC:
#   cd deploy/control-plane && ./scripts/enroll-agent.sh --name home-pc
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CP="$ROOT/deploy/control-plane"
WITH_ENROLL=0
ENROLL_NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-enroll-api) WITH_ENROLL=1; shift ;;
    --enroll) ENROLL_NAME="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo ""
echo "⚕ Hermes Control Plane installer"
echo "  MongoDB replica set + CA for agent X.509 auth"
echo ""

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required."; exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: docker compose plugin is required."; exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "ERROR: openssl is required."; exit 1
fi

mkdir -p "$CP/certs" "$CP/bundles"
chmod +x "$CP/scripts/"*.sh || true

if [[ ! -f "$CP/.env" ]]; then
  cp "$CP/.env.example" "$CP/.env"
  # Generate strong passwords
  ROOT_PASS=$(openssl rand -hex 16)
  ENROLL_TOKEN=$(openssl rand -hex 24)
  if grep -q '^MONGO_ROOT_PASSWORD=' "$CP/.env"; then
    sed -i.bak "s/^MONGO_ROOT_PASSWORD=.*/MONGO_ROOT_PASSWORD=$ROOT_PASS/" "$CP/.env"
  fi
  if grep -q '^HERMES_ENROLL_TOKEN=' "$CP/.env"; then
    sed -i.bak "s/^HERMES_ENROLL_TOKEN=.*/HERMES_ENROLL_TOKEN=$ENROLL_TOKEN/" "$CP/.env"
  fi
  rm -f "$CP/.env.bak"
  echo "→ Wrote $CP/.env (passwords generated)"
else
  echo "→ Using existing $CP/.env"
fi

echo "→ Generating CA / server certificates…"
bash "$CP/scripts/gen-ca.sh"

# keyfile must be readable by mongo container user (uid 999)
chmod 400 "$CP/certs/mongo-keyfile" 2>/dev/null || true
# Docker on Linux often needs 999:999 ownership for keyfile
if command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$CP/certs:/certs" mongo:7 bash -c \
    'chown 999:999 /certs/mongo-keyfile /certs/mongo-server.pem /certs/ca.crt 2>/dev/null || true; chmod 400 /certs/mongo-keyfile; chmod 444 /certs/ca.crt; chmod 400 /certs/mongo-server.pem' \
    >/dev/null 2>&1 || true
fi

echo "→ Starting MongoDB replica set…"
cd "$CP"
if [[ $WITH_ENROLL -eq 1 ]]; then
  docker compose --profile enroll up -d
else
  docker compose up -d mongo1 mongo2 mongo3 orchestrator
fi

bash "$CP/scripts/init-replica.sh"

if [[ -n "$ENROLL_NAME" ]]; then
  bash "$CP/scripts/enroll-agent.sh" --name "$ENROLL_NAME"
fi

echo ""
echo "✓ Control plane is up."
echo ""
echo "  MongoDB   : ports 27017–27019 (X.509 / SCRAM)"
echo "  Orchestrator (mTLS): https://<this-host>:8744"
echo "    → without a valid agent client cert the connection is DROPPED"
echo "  Enroll code API   : http://<this-host>:8743  (hermes agent add)"
echo ""
echo "Next steps:"
echo "  1. Edit HERMES_MONGO_HOSTS in deploy/control-plane/.env"
echo "  2. hermes agent add          # one-time code for a PC"
echo "  3. On the PC: hermes db connect"
echo ""
