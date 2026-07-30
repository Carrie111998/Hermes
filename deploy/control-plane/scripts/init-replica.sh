#!/usr/bin/env bash
# Initiate replica set and create admin + app roles for Hermes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && set -a && source "$ROOT/.env" && set +a

ROOT_USER="${MONGO_ROOT_USER:-hermesRoot}"
ROOT_PASS="${MONGO_ROOT_PASSWORD:-changeMeNow}"
APP_USER="${HERMES_APP_USER:-hermesApp}"
APP_PASS="${HERMES_APP_PASSWORD:-$(openssl rand -hex 16)}"

echo "→ Waiting for mongo1…"
for i in $(seq 1 60); do
  if docker exec hermes-mongo1 mongosh --quiet --eval 'db.runCommand({ ping: 1 }).ok' >/dev/null 2>&1; then
    break
  fi
  sleep 2
  if [[ $i -eq 60 ]]; then
    echo "Mongo1 did not become ready"; exit 1
  fi
done

echo "→ Initiating replica set rs0…"
docker exec hermes-mongo1 mongosh --quiet --eval '
try {
  rs.status();
  print("replica set already initiated");
} catch (e) {
  rs.initiate({
    _id: "rs0",
    members: [
      { _id: 0, host: "mongo1:27017" },
      { _id: 1, host: "mongo2:27017" },
      { _id: 2, host: "mongo3:27017" }
    ]
  });
  print("replica set initiated");
}
' || true

echo "→ Waiting for PRIMARY…"
for i in $(seq 1 60); do
  STATE=$(docker exec hermes-mongo1 mongosh --quiet --eval 'try { rs.isMaster().ismaster } catch(e) { false }' 2>/dev/null || echo false)
  if [[ "$STATE" == "true" ]]; then
    break
  fi
  sleep 2
done

echo "→ Creating admin / app users (localhost exception if first run)…"
docker exec hermes-mongo1 mongosh --quiet --eval "
const rootUser = '$ROOT_USER';
const rootPass = '$ROOT_PASS';
const appUser = '$APP_USER';
const appPass = '$APP_PASS';
const admin = db.getSiblingDB('admin');
try {
  admin.createUser({
    user: rootUser,
    pwd: rootPass,
    roles: [ { role: 'root', db: 'admin' } ]
  });
  print('created root user');
} catch (e) {
  print('root user: ' + e.message);
}
" || true

# Authenticated setup of app user + DBs
docker exec hermes-mongo1 mongosh --quiet -u "$ROOT_USER" -p "$ROOT_PASS" --authenticationDatabase admin --eval "
const appUser = '$APP_USER';
const appPass = '$APP_PASS';
const admin = db.getSiblingDB('admin');
try {
  admin.createUser({
    user: appUser,
    pwd: appPass,
    roles: [
      { role: 'readWrite', db: 'hermes_shared' },
      { role: 'dbAdmin', db: 'hermes_shared' },
      { role: 'readWrite', db: 'hermes_profile_default' },
      { role: 'dbAdmin', db: 'hermes_profile_default' },
      { role: 'readWriteAnyDatabase', db: 'admin' },
      { role: 'dbAdminAnyDatabase', db: 'admin' }
    ]
  });
  print('created app user ' + appUser);
} catch (e) {
  print('app user: ' + e.message);
}
db.getSiblingDB('hermes_shared').cluster_state.updateOne(
  { _id: 'default' },
  { \$setOnInsert: {
      _id: 'default',
      active_node_id: null,
      messaging_owner: null,
      handoff_state: 'idle',
      failover: 'auto',
      history: []
  }},
  { upsert: true }
);
print('hermes_shared ready');
"

# Persist app password for enroll scripts (gitignored)
umask 077
cat > "$ROOT/certs/app-credentials.txt" <<EOF
HERMES_APP_USER=$APP_USER
HERMES_APP_PASSWORD=$APP_PASS
MONGO_ROOT_USER=$ROOT_USER
MONGO_ROOT_PASSWORD=$ROOT_PASS
EOF

echo "✓ Replica set ready."
echo "  App SCRAM user saved to certs/app-credentials.txt (keep secret)."
echo "  Prefer X.509 agent enroll via: ./scripts/enroll-agent.sh --name <pc>"
