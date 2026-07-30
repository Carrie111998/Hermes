#!/usr/bin/env bash
# Issue an X.509 client cert for one agent PC and write a bootstrap bundle.
#
# Usage:
#   ./enroll-agent.sh --name home-pc [--profile default] [--out ./bundles/home-pc]
#
# Bundle contents (copy to the PC):
#   bootstrap.yaml
#   certs/ca.crt
#   certs/agent.pem          # cert + key (combined)
#   README.txt
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME=""
PROFILE="default"
OUT=""
HOSTS="${HERMES_MONGO_HOSTS:-}"
REPLICA="${HERMES_REPLICA_SET:-rs0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --hosts) HOSTS="$2"; shift 2 ;;
    --replica-set) REPLICA="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$NAME" ]]; then
  echo "Usage: $0 --name <machine-name> [--profile default] [--out DIR]"
  exit 1
fi

# Sanitize CN
CN=$(echo "$NAME" | tr -c 'A-Za-z0-9._-' '-' | sed 's/^-*//;s/-*$//')
[[ -n "$CN" ]] || { echo "Invalid --name"; exit 1; }

OUT="${OUT:-$ROOT/bundles/$CN}"
CERTS="$ROOT/certs"
[[ -f "$CERTS/ca.crt" && -f "$CERTS/ca.key" ]] || {
  echo "CA missing — run ./scripts/gen-ca.sh first"; exit 1
}

# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && set -a && source "$ROOT/.env" && set +a
[[ -f "$CERTS/app-credentials.txt" ]] && set -a && source "$CERTS/app-credentials.txt" && set +a

if [[ -z "$HOSTS" ]]; then
  HOSTS="${HERMES_MONGO_HOSTS:-localhost:27017,localhost:27018,localhost:27019}"
fi

mkdir -p "$OUT/certs" "$CERTS/agents"
AGENT_DIR="$CERTS/agents/$CN"
mkdir -p "$AGENT_DIR"

echo "→ Issuing client certificate CN=$CN …"
openssl genrsa -out "$AGENT_DIR/agent.key" 2048
openssl req -new -key "$AGENT_DIR/agent.key" \
  -subj "/O=Hermes/OU=hermes-agents/CN=$CN" \
  -out "$AGENT_DIR/agent.csr"

cat > "$AGENT_DIR/agent.ext" <<EOF
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
EOF

openssl x509 -req -in "$AGENT_DIR/agent.csr" -CA "$CERTS/ca.crt" -CAkey "$CERTS/ca.key" \
  -CAcreateserial -out "$AGENT_DIR/agent.crt" -days 825 -sha256 -extfile "$AGENT_DIR/agent.ext"

cat "$AGENT_DIR/agent.crt" "$AGENT_DIR/agent.key" > "$AGENT_DIR/agent.pem"
chmod 600 "$AGENT_DIR/agent.key" "$AGENT_DIR/agent.pem"

cp "$CERTS/ca.crt" "$OUT/certs/ca.crt"
cp "$AGENT_DIR/agent.pem" "$OUT/certs/agent.pem"

SUBJECT="CN=$CN,OU=hermes-agents,O=Hermes"
ROOT_USER="${MONGO_ROOT_USER:-hermesRoot}"
ROOT_PASS="${MONGO_ROOT_PASSWORD:-changeMeNow}"

echo "→ Registering X.509 user in Mongo (\$external): $SUBJECT"
docker exec hermes-mongo1 mongosh --quiet \
  -u "$ROOT_USER" -p "$ROOT_PASS" --authenticationDatabase admin \
  --eval "
const subject = '$SUBJECT';
const ext = db.getSiblingDB('\$external');
try {
  ext.createUser({
    user: subject,
    roles: [
      { role: 'readWrite', db: 'hermes_shared' },
      { role: 'readWrite', db: 'hermes_profile_${PROFILE}' },
      { role: 'dbAdmin', db: 'hermes_profile_${PROFILE}' }
    ]
  });
  print('created X509 user');
} catch (e) {
  print('X509 user: ' + e.message);
}
db.getSiblingDB('hermes_shared').agent_registry.updateOne(
  { machine_id: '$CN' },
  { \$set: {
      machine_id: '$CN',
      cert_cn: '$CN',
      cert_subject: subject,
      profile: '$PROFILE',
      enrolled_at: new Date(),
      auth_mode: 'x509'
  }},
  { upsert: true }
);
" || echo "WARNING: could not register X509 user (is replica initialized?)."

URI="mongodb://${HOSTS}/?replicaSet=${REPLICA}&tls=true&authMechanism=MONGODB-X509&authSource=%24external"

# Orchestrator mTLS endpoint (same host as first Mongo host by default)
FIRST_HOST="${HOSTS%%,*}"
ORCH_HOST="${FIRST_HOST%%:*}"
ORCH_URL="${HERMES_ORCHESTRATOR_URL:-https://${ORCH_HOST}:8744}"

cat > "$OUT/bootstrap.yaml" <<EOF
# Hermes agent bootstrap — generated $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Auth: X.509 client certificate (do not share agent.pem)
# Mongo + orchestrator BOTH require this cert — no cert ⇒ connection dropped
mongo_uri: "${URI}"
profile: ${PROFILE}
machine_id: ${CN}
auth_mode: x509
shared_db: hermes_shared
tls:
  ca_file: certs/ca.crt
  cert_key_file: certs/agent.pem
orchestrator:
  url: "${ORCH_URL}"
EOF

cat > "$OUT/README.txt" <<EOF
Hermes agent enrollment bundle for: $CN
Profile: $PROFILE
Auth: X.509

On the agent PC:
  1. Install Hermes (scripts/install-agent.sh or install-agent.ps1)
  2. Copy this folder's contents into HERMES_HOME:
       Windows: %LOCALAPPDATA%\\hermes\\
       Linux/macOS: ~/.hermes/
     So you have:
       HERMES_HOME/bootstrap.yaml
       HERMES_HOME/certs/ca.crt
       HERMES_HOME/certs/agent.pem
  3. hermes storage status
  4. hermes cluster status

Revoke: delete the \$external user for subject:
  $SUBJECT
EOF

# Also write a zip if possible
if command -v zip >/dev/null 2>&1; then
  (cd "$OUT/.." && zip -qr "${CN}.zip" "$CN")
  echo "✓ Bundle: $OUT  (+ ${OUT}.zip)"
else
  echo "✓ Bundle: $OUT"
fi
echo "  Subject: $SUBJECT"
echo "  Copy bootstrap.yaml + certs/ to the agent PC HERMES_HOME."
