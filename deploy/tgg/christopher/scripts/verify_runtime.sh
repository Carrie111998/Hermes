#!/usr/bin/env bash
set -euo pipefail

MODE="${1:---quick}"
APP_ROOT="${APP_ROOT:-/home/pclaw/apps/hermes-pcl}"
HERMES_HOME="${HERMES_HOME:-/home/pclaw/.hermes-christopher-tgg}"
TEST_HOME="${TEST_HOME:-/home/pclaw/.hermes-christopher-tgg-test}"
DEPLOY_ROOT="$APP_ROOT/deploy/tgg/christopher"
RUNTIME_ROOT="$HERMES_HOME/runtime"

if [[ "$MODE" == "--check-mode" ]]; then
  raw="$(pcl service locate --system christopher --domain pa)"
  target="$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["data"]["system"]["liveFacts"]["host"]["sshTargetAlias"])' <<<"$raw")"
  exec ssh "$target" "$DEPLOY_ROOT/scripts/verify_runtime.sh" --full
fi
if [[ "$MODE" != "--quick" && "$MODE" != "--full" ]]; then
  echo "usage: $0 --quick|--full|--check-mode" >&2
  exit 2
fi

hostname
systemctl is-active --quiet christopher-tgg-hermes.service
systemctl is-active --quiet christopher-tgg-hermes-health.timer
test -x "$APP_ROOT/.venv/bin/python"
test -s "$HERMES_HOME/.env"
test -s "$HERMES_HOME/config.yaml"
test -s "$HERMES_HOME/christopher_tgg_constitution.yaml"
test -s "$RUNTIME_ROOT/engine-slot"
test -s "$RUNTIME_ROOT/processing-gate.json"
test -s "$RUNTIME_ROOT/capture-cursor.json"
for _ in $(seq 1 30); do
  [[ -s "$RUNTIME_ROOT/capture-consumer-status.json" ]] && break
  sleep 1
done
test -s "$RUNTIME_ROOT/capture-consumer-status.json"
grep -qE '^OPENAI_API_KEY=' "$HERMES_HOME/.env"
grep -qE '^GEMINI_API_KEY=' "$HERMES_HOME/.env"

"$APP_ROOT/.venv/bin/python" \
  "$DEPLOY_ROOT/scripts/validate_deployment_spec.py" \
  --app-root "$APP_ROOT" \
  --spec "$DEPLOY_ROOT/client-agent-deployment.yaml" >/dev/null

if grep -q '/messages' "$DEPLOY_ROOT/systemd/christopher-tgg-hermes.service"; then
  echo "consumer unit must never reference destructive /messages" >&2
  exit 31
fi
grep -q '/var/lib/tgg-capture/whatsapp/capture/events.jsonl' \
  "$DEPLOY_ROOT/systemd/christopher-tgg-hermes.service"

main_pid="$(systemctl show -p MainPID --value christopher-tgg-hermes.service)"
if [[ ! "$main_pid" =~ ^[1-9][0-9]*$ ]]; then
  echo "Christopher consumer has no live MainPID" >&2
  exit 32
fi
for fd in /proc/"$main_pid"/fd/*; do
  target="$(readlink "$fd" 2>/dev/null || true)"
  if [[ "$target" == /var/lib/tgg-capture/* ]]; then
    echo "disabled consumer unexpectedly opened capture state: $target" >&2
    exit 33
  fi
done

"$APP_ROOT/.venv/bin/python" - "$APP_ROOT" "$HERMES_HOME" <<'PY'
import datetime, hashlib, json, os, pathlib, sqlite3, stat, sys, yaml

app = pathlib.Path(sys.argv[1])
home = pathlib.Path(sys.argv[2])
deploy = app / "deploy/tgg/christopher"
runtime = home / "runtime"
slot = (runtime / "engine-slot").read_text().strip()
# slot id -> model. gpt-5.6-luna-low runs gpt-5.6-luna at reasoning_effort low.
SLOT_MODELS = {
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5.6-luna-low": "gpt-5.6-luna",
}
SLOT_REASONING_EFFORT = {"gpt-5.6-luna-low": "low"}
assert slot in SLOT_MODELS, slot
slot_model = SLOT_MODELS[slot]
slot_effort = SLOT_REASONING_EFFORT.get(slot)

expected = {}
for line in (deploy / "runtime-slots/SHA256SUMS").read_text().splitlines():
    digest, relative = line.split(None, 1)
    expected[relative.strip()] = digest
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

assert sha(home / "config.yaml") == expected[f"{slot}/config.yaml"]
assert sha(home / "christopher_tgg_constitution.yaml") == expected[f"{slot}/christopher_tgg_constitution.yaml"]
config = yaml.safe_load((home / "config.yaml").read_text())
constitution = yaml.safe_load((home / "christopher_tgg_constitution.yaml").read_text())
assert config["pa"]["enabled"] is False
assert config["group_sessions_per_user"] is False
assert config["platforms"]["whatsapp"]["enabled"] is False
assert config["model"]["provider"] == "openai-direct-primary"
assert config["model"]["default"] == slot_model
if slot_effort is None:
    assert "reasoning_effort" not in config["agent"]
else:
    assert config["agent"]["reasoning_effort"] == slot_effort
assert constitution["runtime"] == {"provider": "openai-direct-primary", "model": slot_model}

gate = json.loads((runtime / "processing-gate.json").read_text())
assert gate["enabled"] is False
assert gate["generation"] == 0
assert gate["last_transition"] is None
assert gate["disabled_at"] == gate["initial_disabled_boundary"]
boundary = datetime.datetime.fromisoformat(gate["disabled_at"])
assert boundary.tzinfo is not None
assert boundary <= datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=5)

cursor = json.loads((runtime / "capture-cursor.json").read_text())
assert cursor["source_path"] == "/var/lib/tgg-capture/whatsapp/capture/events.jsonl"
assert int(cursor["offset"]) == int(cursor["initial_offset"])

status = json.loads((runtime / "capture-consumer-status.json").read_text())
assert status["state"] == "standby"
assert status["processing_enabled"] is False
assert status["config_enabled"] is False
assert status["gate_enabled"] is False
assert status["gate_generation"] == 0
assert status["source_opened"] is False
assert status["cursor_advanced"] is False

state_db = home / "state.db"
production_turns = 0
if state_db.exists():
    conn = sqlite3.connect(state_db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "pa_turns" in tables:
            production_turns = conn.execute(
                "SELECT COUNT(*) FROM pa_turns WHERE replay_run_id IS NULL AND started_at > ?",
                (boundary.timestamp(),),
            ).fetchone()[0]
    finally:
        conn.close()
assert production_turns == 0, production_turns

inbox_db = runtime / "capture-inbox.db"
inbox_rows = 0
if inbox_db.exists():
    conn = sqlite3.connect(inbox_db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ingress_events" in tables:
            inbox_rows = conn.execute("SELECT COUNT(*) FROM ingress_events").fetchone()[0]
    finally:
        conn.close()
assert inbox_rows == 0, inbox_rows

config_mode = stat.S_IMODE((home / "config.yaml").stat().st_mode)
assert config_mode == 0o640, oct(config_mode)
assert (home / "config.yaml").stat().st_uid == 0
print(json.dumps({
    "quick_verify": "pass",
    "slot": slot,
    "provider": "openai-direct-primary",
    "model": slot_model,
    "reasoning_effort": slot_effort,
    "config_sha256": sha(home / "config.yaml"),
    "constitution_sha256": sha(home / "christopher_tgg_constitution.yaml"),
    "production_turns_after_disabled_boundary": production_turns,
    "production_inbox_rows": inbox_rows,
    "cursor_unchanged_while_disabled": True,
}, sort_keys=True))
PY

if [[ "$MODE" == "--quick" ]]; then
  exit 0
fi

# Verify the configured auxiliary model and its provider credential without
# producing client-visible output or mutating any client system.
runuser -u pclaw -- env \
  HERMES_HOME="$HERMES_HOME" \
  "$APP_ROOT/.venv/bin/python" - <<'PY'
import json, urllib.request
from pathlib import Path
from dotenv import dotenv_values

values = dotenv_values(Path.home() / ".hermes-christopher-tgg/.env")
key = values.get("GEMINI_API_KEY")
assert key
request = urllib.request.Request(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite",
    headers={"x-goog-api-key": key},
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.load(response)
assert payload.get("name", "").endswith("gemini-3.1-flash-lite"), payload.get("name")
print(json.dumps({"auxiliary_provider": "gemini", "auxiliary_model": "gemini-3.1-flash-lite", "reachable": True}))
PY

full_report="$TEST_HOME/latest-full-verification.json"
runuser -u pclaw -- env \
  HERMES_HOME="$HERMES_HOME" \
  "$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/run_isolated_smoke.py" \
  --app-root "$APP_ROOT" \
  --live-home "$HERMES_HOME" \
  --test-root "$TEST_HOME" \
  --slot-file "$RUNTIME_ROOT/engine-slot" \
  --report "$full_report"

"$APP_ROOT/.venv/bin/python" - "$full_report" "$RUNTIME_ROOT/engine-slot" <<'PY'
import json, pathlib, sys
p = json.loads(pathlib.Path(sys.argv[1]).read_text())
slot = pathlib.Path(sys.argv[2]).read_text().strip()
SLOT_MODELS = {
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5.6-luna-low": "gpt-5.6-luna",
}
SLOT_REASONING_EFFORT = {"gpt-5.6-luna-low": "low"}
assert slot in SLOT_MODELS, slot
assert p["ok"] is True
assert p["mode"] == "fixture-only"
assert p["result"]["processed"] == 1
assert p["result"]["turn_id"]
assert p["result"]["provider"] == "openai-direct-primary"
assert p["result"]["model"] == SLOT_MODELS[slot], (p["result"]["model"], slot)
assert p["client_mutation_requests"] == 0
assert p["external_outbound_sent"] == 0
print(json.dumps({
    "full_verify": "pass",
    "slot": slot,
    "turn_id": p["result"]["turn_id"],
    "provider": p["result"]["provider"],
    "model": p["result"]["model"],
    "reasoning_effort": SLOT_REASONING_EFFORT.get(slot),
    "client_mutation_requests": 0,
    "external_outbound_sent": 0,
}, sort_keys=True))
PY
