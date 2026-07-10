#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/home/pclaw/apps/hermes-pcl}"
HERMES_HOME="${HERMES_HOME:-/home/pclaw/.hermes-christopher-tgg}"
TEST_HOME="${TEST_HOME:-/home/pclaw/.hermes-christopher-tgg-test}"
CAPTURE_SOURCE="${CAPTURE_SOURCE:-/var/lib/tgg-capture/whatsapp/capture/events.jsonl}"
DEPLOY_ROOT="$APP_ROOT/deploy/tgg/christopher"
RUNTIME_ROOT="$HERMES_HOME/runtime"

if ! getent passwd pclaw >/dev/null; then
  useradd --create-home --shell /bin/bash pclaw
fi
if getent group tggcapture >/dev/null; then
  usermod -a -G tggcapture pclaw
fi

if ! dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'install ok installed'; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3-venv
fi

install -d -m 0755 -o pclaw -g pclaw /home/pclaw /home/pclaw/apps
install -d -m 0750 -o pclaw -g pclaw "$HERMES_HOME" "$RUNTIME_ROOT" "$TEST_HOME"
test -s "$HERMES_HOME/.env" || {
  echo "missing $HERMES_HOME/.env; run prepare_host_secrets.sh first" >&2
  exit 20
}
grep -qE '^OPENAI_API_KEY=' "$HERMES_HOME/.env" || {
  echo "missing OPENAI_API_KEY in Hermes env" >&2
  exit 20
}
grep -qE '^GEMINI_API_KEY=' "$HERMES_HOME/.env" || {
  echo "missing GEMINI_API_KEY in Hermes env" >&2
  exit 20
}
chmod 0600 "$HERMES_HOME/.env"
chown pclaw:pclaw "$HERMES_HOME/.env"

if [[ ! -x "$APP_ROOT/.venv/bin/python" ]]; then
  python3 -m venv "$APP_ROOT/.venv"
fi
chown -R pclaw:pclaw "$APP_ROOT/.venv"
runuser -u pclaw -- "$APP_ROOT/.venv/bin/python" -m pip install \
  --disable-pip-version-check --no-input --editable "$APP_ROOT"

"$APP_ROOT/.venv/bin/python" \
  "$DEPLOY_ROOT/scripts/validate_deployment_spec.py" \
  --app-root "$APP_ROOT" \
  --spec "$DEPLOY_ROOT/client-agent-deployment.yaml"

install -m 0640 -o root -g pclaw "$DEPLOY_ROOT/SOUL.md" "$HERMES_HOME/SOUL.md"

python3 - "$RUNTIME_ROOT/processing-gate.json" <<'PY'
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
if path.exists():
    state = json.loads(path.read_text())
    if state.get("enabled") is not False:
        raise SystemExit("processing gate is not disabled; sprint deploy refuses")
else:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    state = {
        "version": 1,
        "enabled": False,
        "generation": 0,
        "initial_state": "disabled",
        "initial_disabled_boundary": now,
        "disabled_at": now,
        "last_transition": None,
        "source": "ClientAgentDeployment",
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
PY
chown root:pclaw "$RUNTIME_ROOT/processing-gate.json"
chmod 0640 "$RUNTIME_ROOT/processing-gate.json"

"$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/apply_engine_slot.py" \
  --app-root "$APP_ROOT" \
  --hermes-home "$HERMES_HOME"

if [[ ! -e "$RUNTIME_ROOT/capture-cursor.json" ]]; then
  runuser -u pclaw -- "$APP_ROOT/.venv/bin/python" \
    "$APP_ROOT/gateway/durable_jsonl_consumer.py" init-cursor \
    --source "$CAPTURE_SOURCE" \
    --cursor "$RUNTIME_ROOT/capture-cursor.json" \
    --position end >/dev/null
fi

for unit in \
  christopher-tgg-hermes.service \
  christopher-tgg-hermes-health.service \
  christopher-tgg-hermes-health.timer; do
  ln -sfn "$DEPLOY_ROOT/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable christopher-tgg-hermes.service >/dev/null
systemctl enable --now christopher-tgg-hermes-health.timer >/dev/null

echo "Christopher Hermes runtime bootstrap complete"
