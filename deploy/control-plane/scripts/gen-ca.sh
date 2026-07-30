#!/usr/bin/env bash
# Generate CA, Mongo server cert, and replica-set keyfile under ./certs
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CERTS="$ROOT/certs"
mkdir -p "$CERTS"
cd "$CERTS"

if [[ -f ca.crt && -f ca.key && -f mongo-server.pem && -f mongo-keyfile ]]; then
  echo "Certs already present in $CERTS — skipping generation."
  exit 0
fi

echo "→ Generating Hermes control-plane CA and server certificates…"

openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 \
  -subj "/O=Hermes/OU=ControlPlane/CN=Hermes Agent CA" \
  -out ca.crt

openssl genrsa -out mongo-server.key 2048
openssl req -new -key mongo-server.key \
  -subj "/O=Hermes/OU=Mongo/CN=hermes-mongo" \
  -out mongo-server.csr

cat > mongo-server.ext <<'EOF'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=DNS:localhost,DNS:mongo1,DNS:mongo2,DNS:mongo3,DNS:hermes-mongo1,DNS:hermes-mongo2,DNS:hermes-mongo3,IP:127.0.0.1
EOF

openssl x509 -req -in mongo-server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out mongo-server.crt -days 825 -sha256 -extfile mongo-server.ext

cat mongo-server.crt mongo-server.key > mongo-server.pem
chmod 600 mongo-server.key mongo-server.pem ca.key

# Replica set keyfile (shared secret between mongo nodes)
openssl rand -base64 756 > mongo-keyfile
chmod 400 mongo-keyfile

# Placeholder so compose can mount even before enroll
touch .gitkeep

echo "✓ Wrote CA + server PEM + keyfile to $CERTS"
echo "  Keep ca.key secret. Distribute only ca.crt + agent client certs."
