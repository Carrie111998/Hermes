#!/usr/bin/env bash
# Christopher CAPTURE-FRESHNESS check.
#
# WHY THIS EXISTS: on 2026-07-11 an unguarded media download hung the WA bridge's
# event loop. For 34 hours it captured ZERO messages -- while /health reported
# "connected" (true: the socket was up) and pa.tgg.christopher.runtime_invariants
# passed (true: config intact, cursor unmoved, no production turns). Every
# instrument was honest. NONE of them asked whether data was still ARRIVING.
#
# This asks that, and only that.
#
# FAIL LOUD: a false alarm on a genuinely quiet weekend costs one look.
# A missed wedge costs a paying client 26 hours of their operational history.
#
# -----------------------------------------------------------------------------
# 2026-07-14 (edna): QUEUE SATURATION IS NOW GATE-AWARE AND CAP-RELATIVE.
#
# The old rule was `QLEN >= 100 -> FAIL`. It paged a BLOCKER on a live client
# agent while nothing was wrong. It was wrong twice:
#
#   1. WRONG CAP. 100 is the bridge's default MAX_QUEUE_SIZE only when
#      SYNC_FULL_HISTORY is OFF. Christopher runs WHATSAPP_SYNC_FULL_HISTORY=true,
#      so its real cap is 5000. The check called 190/5000 -- 3.8% full --
#      "saturated".
#
#   2. WRONG QUESTION. messageQueue is drained ONLY by a consumer polling
#      GET /messages. Christopher's processing is GATED OFF by design pre-go-live;
#      verify_runtime.sh asserts gate["enabled"] is False as an INVARIANT. With no
#      consumer by design, the queue necessarily grows to its cap and pins there.
#      Queue depth is a MEANINGLESS health signal while the gate is closed -- the
#      check would fire forever, and harder over time.
#
# A blocker check that always fails is an alarm nobody hears, which is precisely
# how the 34-hour wedge above went unnoticed. So saturation only FAILS when a
# consumer is EXPECTED (gate open). While the gate is closed the depth is still
# REPORTED -- it is not hidden -- we simply do not fail on it.
#
# The durable path is independent: appendCaptureEvent() writes events.jsonl BEFORE
# messageQueue.push(), so even a full ring buffer drops nothing from capture.
# Capture-freshness -- the question this check exists to ask -- is unchanged.
# -----------------------------------------------------------------------------
set -uo pipefail

HOST="${TGG_HOST:-tgg-app-1}"
EVENTS="/var/lib/tgg-capture/whatsapp/capture/events.jsonl"
MAX_STALE_HOURS="${CAPTURE_MAX_STALE_HOURS:-12}"
HERMES_HOME="${HERMES_HOME:-/home/pclaw/.hermes-christopher-tgg}"
GATE_FILE="$HERMES_HOME/runtime/processing-gate.json"
# Fail on the queue only when it is genuinely near its cap, never at an
# arbitrary absolute count.
SATURATION_PCT="${CAPTURE_QUEUE_SATURATION_PCT:-90}"

OUT=$(ssh -o ConnectTimeout=15 -o BatchMode=yes "$HOST" "
  M=\$(sudo stat -c %Y '$EVENTS' 2>/dev/null || echo 0)
  H=\$(curl -s -m 6 http://127.0.0.1:3011/health 2>/dev/null)
  Q=\$(printf '%s' \"\$H\" | sed -n 's/.*\"queueLength\":\([0-9]*\).*/\1/p')
  S=\$(printf '%s' \"\$H\" | sed -n 's/.*\"status\":\"\([a-z]*\)\".*/\1/p')

  # Resolve the bridge's REAL cap from the running process, never a guess.
  P=\$(pgrep -f 'whatsapp-bridge/bridge.js' | head -1)
  ENVV=\$(sudo tr '\0' '\n' < /proc/\$P/environ 2>/dev/null)
  CAP=\$(printf '%s' \"\$ENVV\" | sed -n 's/^WHATSAPP_MAX_QUEUE_SIZE=\([0-9][0-9]*\)\$/\1/p' | head -1)
  if [ -z \"\$CAP\" ]; then
    if printf '%s' \"\$ENVV\" | grep -q '^WHATSAPP_SYNC_FULL_HISTORY=true\$'; then CAP=5000; else CAP=100; fi
  fi

  # Is a consumer EXPECTED at all? Only then is queue depth a health signal.
  G=\$(sudo sed -n 's/.*\"enabled\"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p' '$GATE_FILE' 2>/dev/null | head -1)
  echo \"\$M \${Q:-x} \${S:-x} \${CAP:-x} \${G:-unknown}\"
" 2>/dev/null)

read -r MTIME QLEN STATUS CAP GATE <<<"$OUT"

if [[ -z "${MTIME:-}" || "$MTIME" == "0" ]]; then
  echo "{\"ok\": false, \"reason\": \"could not read capture log on $HOST\"}"
  exit 1
fi

NOW=$(date +%s)
STALE_H=$(( (NOW - MTIME) / 3600 ))

# Saturation is relative to the ACTUAL cap.
SATURATED=false
SAT_THRESHOLD="unknown"
if [[ "$QLEN" =~ ^[0-9]+$ && "$CAP" =~ ^[0-9]+$ ]]; then
  SAT_THRESHOLD=$(( CAP * SATURATION_PCT / 100 ))
  [ "$QLEN" -ge "$SAT_THRESHOLD" ] && SATURATED=true
fi

# Gate closed => no consumer BY DESIGN => queue depth is informational only.
# Unknown gate => assume a consumer is expected (fail closed).
CONSUMER_EXPECTED=true
[ "$GATE" = "false" ] && CONSUMER_EXPECTED=false

# The stall signature: nothing written for longer than a plausible quiet window.
# The wedge signature: queue pinned at its REAL cap while a consumer is expected.
FAIL=false
[ "$STALE_H" -ge "$MAX_STALE_HOURS" ] && FAIL=true
[ "$SATURATED" = "true" ] && [ "$CONSUMER_EXPECTED" = "true" ] && FAIL=true

printf '{"capture_stale_hours": %s, "queue_length": "%s", "queue_cap": "%s", "saturation_threshold": "%s", "socket_status": "%s", "queue_saturated": %s, "processing_gate_enabled": "%s", "consumer_expected": %s, "threshold_hours": %s}\n' \
  "$STALE_H" "$QLEN" "$CAP" "$SAT_THRESHOLD" "$STATUS" "$SATURATED" "$GATE" "$CONSUMER_EXPECTED" "$MAX_STALE_HOURS"

if [ "$FAIL" = "true" ]; then
  if [ "$STALE_H" -ge "$MAX_STALE_HOURS" ]; then
    echo "FAIL: christopher has captured NOTHING for ${STALE_H}h (threshold ${MAX_STALE_HOURS}h). Capture is stalled." >&2
  else
    echo "FAIL: christopher's queue is saturated at ${QLEN}/${CAP} (>= ${SAT_THRESHOLD}) while a consumer IS expected (processing gate enabled=${GATE}). The poller has stopped draining." >&2
  fi
  exit 1
fi
exit 0
