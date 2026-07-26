#!/usr/bin/env bash
# Christopher CAPTURE-FRESHNESS check.
#
# Row age is evidence, not a verdict: routine overnight/weekend quiet can be
# older than CAPTURE_MAX_STALE_HOURS without any capture failure. The verdict
# comes from the bridge's inbound boundary plus capture/inbox lockstep.
set -uo pipefail

HOST="${TGG_HOST:-tgg-app-1}"
EVENTS="/var/lib/tgg-capture/whatsapp/capture/events.jsonl"
MAX_STALE_HOURS="${CAPTURE_MAX_STALE_HOURS:-12}"
FRESHNESS_SLA_SECONDS=7200
HERMES_HOME="${HERMES_HOME:-/home/pclaw/.hermes-christopher-tgg}"
GATE_FILE="$HERMES_HOME/runtime/processing-gate.json"
INBOX_DB="$HERMES_HOME/runtime/capture-inbox.db"
SATURATION_PCT="${CAPTURE_QUEUE_SATURATION_PCT:-90}"
PROBE_FILE=""

usage() {
  echo "usage: $0 [--probe-file <snapshot.json>]" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --probe-file)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      PROBE_FILE="$2"
      shift 2
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

collect_live_probe() {
  ssh -o ConnectTimeout=15 -o BatchMode=yes "$HOST" \
    sudo python3 - "$EVENTS" "$GATE_FILE" "$INBOX_DB" <<'PY'
import json
import pathlib
import sqlite3
import sys
import time
import urllib.request


def last_capture(path):
    last = b""
    with path.open("rb") as handle:
        for line in handle:
            if line.strip():
                last = line
    if not last:
        return None
    item = json.loads(last)
    if isinstance(item, dict) and isinstance(item.get("normalized"), dict):
        item = item["normalized"]
    return item.get("messageId") if isinstance(item, dict) else None


def process_environment():
    for proc in pathlib.Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            if "whatsapp-bridge/bridge.js" not in command:
                continue
            values = {}
            for entry in (proc / "environ").read_bytes().split(b"\0"):
                if b"=" in entry:
                    key, value = entry.split(b"=", 1)
                    values[key.decode()] = value.decode()
            return values
        except (OSError, UnicodeDecodeError):
            continue
    return {}


events = pathlib.Path(sys.argv[1])
gate_file = pathlib.Path(sys.argv[2])
inbox_db = pathlib.Path(sys.argv[3])
now = time.time()
health = json.load(urllib.request.urlopen("http://127.0.0.1:3011/health", timeout=6))
env = process_environment()
queue_cap = env.get("WHATSAPP_MAX_QUEUE_SIZE")
if not queue_cap:
    queue_cap = 5000 if env.get("WHATSAPP_SYNC_FULL_HISTORY") == "true" else 100

gate_enabled = None
try:
    gate_enabled = bool(json.loads(gate_file.read_text())["enabled"])
except (OSError, ValueError, KeyError, TypeError):
    pass

inbox_last_message_id = None
inbox_last_created_at = None
if inbox_db.exists():
    conn = sqlite3.connect(f"file:{inbox_db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT message_id,created_at FROM ingress_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row:
            inbox_last_message_id, inbox_last_created_at = row
    finally:
        conn.close()

payload = {
    "probe_epoch": now,
    "capture_mtime_epoch": events.stat().st_mtime if events.exists() else 0,
    "capture_last_message_id": last_capture(events) if events.exists() else None,
    "inbox_last_message_id": inbox_last_message_id,
    "inbox_last_created_at": inbox_last_created_at,
    "queue_length": health.get("queueLength"),
    "socket_status": health.get("status"),
    "queue_cap": int(queue_cap),
    "processing_gate_enabled": gate_enabled,
    "inboundProgress": health.get("inboundProgress"),
    "bridge_health_probe": "loopback-/health",
}
print(json.dumps(payload, separators=(",", ":")))
PY
}

if [ -n "$PROBE_FILE" ]; then
  [ -r "$PROBE_FILE" ] || {
    echo "probe file is not readable: $PROBE_FILE" >&2
    exit 2
  }
  PROBE_JSON=$(cat "$PROBE_FILE")
else
  PROBE_JSON=$(collect_live_probe 2>/dev/null)
  probe_rc=$?
  if [ "$probe_rc" -ne 0 ] || [ -z "$PROBE_JSON" ]; then
    printf '{"ok":false,"condition":"bridge-health-unavailable","escalation_target":"edna-central","reason":"live bridge health probe failed"}\n'
    echo "FAIL bridge-health-unavailable: live bridge health probe failed" >&2
    exit 1
  fi
fi

PROBE_JSON="$PROBE_JSON" python3 - "$MAX_STALE_HOURS" \
  "$FRESHNESS_SLA_SECONDS" "$SATURATION_PCT" <<'PY'
import datetime
import json
import os
import sys

max_stale_hours = int(sys.argv[1])
freshness_sla_seconds = int(sys.argv[2])
saturation_pct = int(sys.argv[3])

try:
    probe = json.loads(os.environ["PROBE_JSON"])
except (KeyError, TypeError, ValueError) as exc:
    reason = f"probe snapshot is not valid JSON: {exc}"
    print(json.dumps({
        "ok": False,
        "condition": "probe-invalid",
        "escalation_target": "edna-central",
        "reason": reason,
    }, separators=(",", ":")))
    print(f"FAIL probe-invalid: {reason}", file=sys.stderr)
    raise SystemExit(1)

now = probe.get("probe_epoch")
mtime = probe.get("capture_mtime_epoch")
try:
    now = float(now)
    mtime = float(mtime)
    if mtime <= 0 or now < mtime:
        raise ValueError("invalid capture clock")
except (TypeError, ValueError):
    reason = "capture timestamp is missing or invalid"
    print(json.dumps({
        "ok": False,
        "condition": "capture-evidence-missing",
        "escalation_target": "edna-central",
        "reason": reason,
    }, separators=(",", ":")))
    print(f"FAIL capture-evidence-missing: {reason}", file=sys.stderr)
    raise SystemExit(1)

stale_seconds = int(now - mtime)
stale_hours = stale_seconds // 3600
progress = probe.get("inboundProgress")
required_ints = (
    "receivedBatches", "completedBatches", "pendingBatches",
    "receivedMessages", "completedMessages", "pendingMessages",
    "failedBatches", "failedMessages",
    "failedBatchesSinceLastCompletion", "failedMessagesSinceLastCompletion",
)
telemetry_error = None
if not isinstance(progress, dict):
    telemetry_error = "inboundProgress is absent"
else:
    for key in required_ints:
        value = progress.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            telemetry_error = f"inboundProgress.{key} is missing or invalid"
            break
    if telemetry_error is None:
        pending_batches = progress["pendingBatches"]
        pending_messages = progress["pendingMessages"]
        oldest = progress.get("oldestPendingAgeSeconds")
        if pending_batches == 0 and oldest is not None:
            telemetry_error = "oldestPendingAgeSeconds must be null with no pending batches"
        elif pending_batches > 0 and (
            not isinstance(oldest, (int, float))
            or isinstance(oldest, bool)
            or oldest < 0
        ):
            telemetry_error = "oldestPendingAgeSeconds is required for pending batches"
        elif progress["completedBatches"] > progress["receivedBatches"]:
            telemetry_error = "completedBatches exceeds receivedBatches"
        elif progress["completedMessages"] > progress["receivedMessages"]:
            telemetry_error = "completedMessages exceeds receivedMessages"
        elif (
            progress["receivedBatches"]
            != progress["completedBatches"]
            + progress["failedBatches"]
            + pending_batches
        ):
            telemetry_error = (
                "receivedBatches does not match completed/failed/pending counters"
            )
        elif (
            progress["receivedMessages"]
            != progress["completedMessages"]
            + progress["failedMessages"]
            + pending_messages
        ):
            telemetry_error = (
                "receivedMessages does not match completed/failed/pending counters"
            )
        elif (
            progress["failedBatchesSinceLastCompletion"]
            > progress["failedBatches"]
        ):
            telemetry_error = "failedBatchesSinceLastCompletion exceeds failedBatches"
        elif (
            progress["failedMessagesSinceLastCompletion"]
            > progress["failedMessages"]
        ):
            telemetry_error = "failedMessagesSinceLastCompletion exceeds failedMessages"
        else:
            for count_key, timestamp_key in (
                ("receivedBatches", "lastReceivedAt"),
                ("completedBatches", "lastCompletedAt"),
            ):
                timestamp = progress.get(timestamp_key)
                if progress[count_key] == 0 and timestamp is not None:
                    telemetry_error = (
                        f"inboundProgress.{timestamp_key} must be null "
                        f"when {count_key} is zero"
                    )
                    break
                if progress[count_key] > 0:
                    if not isinstance(timestamp, str):
                        telemetry_error = (
                            f"inboundProgress.{timestamp_key} is missing or invalid"
                        )
                        break
                    try:
                        datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    except ValueError:
                        telemetry_error = (
                            f"inboundProgress.{timestamp_key} is missing or invalid"
                        )
                        break
            failed_at = progress.get("lastFailedAt")
            if telemetry_error is None:
                if progress["failedBatches"] == 0 and failed_at is not None:
                    telemetry_error = (
                        "inboundProgress.lastFailedAt must be null "
                        "when failedBatches is zero"
                    )
                elif progress["failedBatches"] > 0:
                    if not isinstance(failed_at, str):
                        telemetry_error = (
                            "inboundProgress.lastFailedAt is missing or invalid"
                        )
                    else:
                        try:
                            datetime.datetime.fromisoformat(
                                failed_at.replace("Z", "+00:00")
                            )
                        except ValueError:
                            telemetry_error = (
                                "inboundProgress.lastFailedAt is missing or invalid"
                            )

queue_length = probe.get("queue_length")
queue_cap = probe.get("queue_cap")
queue_valid = (
    isinstance(queue_length, int)
    and not isinstance(queue_length, bool)
    and queue_length >= 0
    and isinstance(queue_cap, int)
    and not isinstance(queue_cap, bool)
    and queue_cap > 0
)
saturation_threshold = queue_cap * saturation_pct // 100 if queue_valid else None
queue_saturated = bool(queue_valid and queue_length >= saturation_threshold)
gate = probe.get("processing_gate_enabled")
consumer_expected = gate is not False
capture_id = probe.get("capture_last_message_id")
inbox_id = probe.get("inbox_last_message_id")
lockstep = capture_id == inbox_id
socket_status = probe.get("socket_status")
row_age_exceeds_threshold = stale_hours >= max_stale_hours

condition = None
reason = None
if telemetry_error:
    condition = "inbound-telemetry-missing"
    reason = telemetry_error
elif socket_status != "connected":
    condition = "socket-disconnected"
    reason = f"bridge socket status is {socket_status!r}"
elif (
    progress["pendingBatches"] > 0
    and progress["oldestPendingAgeSeconds"] >= freshness_sla_seconds
):
    condition = "inbound-arrived-not-written"
    reason = (
        f"oldest inbound batch has remained pending for "
        f"{int(progress['oldestPendingAgeSeconds'])}s"
    )
elif progress["failedBatchesSinceLastCompletion"] > 0:
    condition = "inbound-arrived-not-written"
    reason = (
        f"{progress['failedBatchesSinceLastCompletion']} inbound batch(es) "
        "failed since the last successful capture completion"
    )
elif consumer_expected and not lockstep and stale_seconds >= freshness_sla_seconds:
    condition = "capture-inbox-diverged"
    reason = (
        f"capture last message {capture_id!r} has not reached inbox "
        f"(latest {inbox_id!r}) within {freshness_sla_seconds}s"
    )
elif consumer_expected and queue_saturated:
    condition = "queue-saturated"
    reason = f"queue is saturated at {queue_length}/{queue_cap}"

failed = condition is not None
weekday = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).weekday()
# Trailing-14-day maxima measured in the plan. This labels context only; it
# never changes the verdict or either configured threshold.
baseline_limit_hours = 34.59 if weekday >= 5 else 14.60
if not failed:
    if row_age_exceeds_threshold:
        condition = (
            "quiet-within-measured-pattern"
            if stale_seconds <= baseline_limit_hours * 3600
            else "quiet-outside-measured-pattern"
        )
    else:
        condition = "capture-active"

result = {
    "ok": not failed,
    "condition": condition,
    "capture_stale_hours": stale_hours,
    "capture_stale_seconds": stale_seconds,
    "row_age_exceeds_threshold": row_age_exceeds_threshold,
    "threshold_hours": max_stale_hours,
    "freshness_sla_seconds": freshness_sla_seconds,
    "quiet_baseline_limit_hours": baseline_limit_hours,
    "socket_status": socket_status,
    "inbound_progress": progress,
    "capture_last_message_id": capture_id,
    "inbox_last_message_id": inbox_id,
    "capture_inbox_lockstep": lockstep,
    "queue_length": queue_length,
    "queue_cap": queue_cap,
    "saturation_threshold": saturation_threshold,
    "queue_saturated": queue_saturated,
    "processing_gate_enabled": gate,
    "consumer_expected": consumer_expected,
    "bridge_health_probe": probe.get("bridge_health_probe"),
    "escalation_target": "edna-central" if failed else None,
}
if reason:
    result["reason"] = reason
print(json.dumps(result, separators=(",", ":")))
if failed:
    print(f"FAIL {condition}: {reason}", file=sys.stderr)
raise SystemExit(1 if failed else 0)
PY
