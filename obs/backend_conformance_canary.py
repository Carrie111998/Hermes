"""Synthetic backend-conformance canary (SR-470 / ADR-0024 §2).

R57 (2026-05-28): the Codex /responses backend began streaming a
``response.completed`` whose ``output`` is ``None`` (the model's real text
arrives via ``response.output_text.delta`` events; only the final aggregate
snapshot is ``None``). Unguarded, the openai SDK parser crashed, the agent loop
classified it non-retryable, and 14/31 crons went down for hours with no alert.
Version-currency monitoring cannot catch this class — no component version
changed.

The SR-467 output=None guard + run_agent's delta-backfill now absorb that shape
transparently, so it became the *permanent, handled* steady state. A canary that
asserted "the stock parser survives output=None" therefore fired permanently on
an already-handled condition (alarm fatigue — it could no longer distinguish the
known/handled drift from a genuinely new failure).

Re-calibrated 2026-05-30 (ADR-0024 §2 addendum): this canary now applies the
SAME guard production uses and asserts the backend actually DELIVERS CONTENT —
not that the raw aggregate is non-None. It (run by a Windows Scheduled Task
every ~10 min):
  1. builds a RAW Codex client + a native Anthropic client (read-only),
  2. issues one minimal request per backend through the production guard,
  3. asserts usable content arrived: non-empty assistant text from
     ``output_text.delta`` (or a message item / non-empty ``output`` list) for
     Codex; a non-None ``content`` list for Anthropic,
  4. writes a sentinel JSON ``~/.hermes/canary/backend_conformance.json`` that
     laptop-monitor.ps1 surfaces as a probe, and
  5. EDGE-TRIGGERS a BACKEND_CONTRACT_DRIFT bus event on healthy->down (and at
     most one re-page/hour while down) so notification volume is bounded.

``healthy`` = content delivered (even if the aggregate is ``None`` — the guard
handles it). ``down`` = a genuinely empty/blocked response the guard cannot
backfill, OR a NEW shape that breaks even the guarded parser. A network/auth
error is NOT contract drift (that is R20 OAuth) — it is reported ``unknown``
(inconclusive), never ``down``.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Re-page interval while a backend stays in drift (seconds). Bounds volume.
_REPAGE_SECONDS = 3600

# Manifest "Laptop Monitor" harness (Diego, 2026-08-12): the canary's
# Anthropic-shaped arm now routes through this harness instead of hitting
# api.anthropic.com directly (which 429s under the background agent fleet).
# The harness uses Manifest's OpenAI-compatible /v1/responses surface with
# model "auto", so Manifest's own fallback chain picks a healthy provider.
_MANIFEST_HARNESS_KEY_FILE = Path("C:/Users/diego/manifest/.mnfst-harness-key")
_MANIFEST_HARNESS_BASE_URL = "http://localhost:2099/v1"
_MANIFEST_HARNESS_MODEL = "auto"


@dataclass
class ProbeResult:
    healthy: Optional[bool]   # True=ok, False=drift(down), None=inconclusive
    detail: str

    @property
    def state(self) -> str:
        if self.healthy is True:
            return "healthy"
        if self.healthy is False:
            return "down"
        return "unknown"


# canonical paths (mirror the .codex_refresh_failed sentinel pattern)
def _hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root())


def _sentinel_path() -> Path:
    return _hermes_root() / "canary" / "backend_conformance.json"


# per-backend conformance checks
def check_codex_conformance(client: Any, model: str) -> ProbeResult:
    """Assert the Codex backend DELIVERS CONTENT through the production guard.

    healthy = usable content arrived (non-empty assistant text via
    ``output_text.delta``, a message item, or a non-empty ``output`` list) — even
    when the aggregate ``output`` is ``None`` (the guard-handled steady state).
    down = a genuinely empty/blocked response the guard cannot backfill, OR a NEW
    shape that breaks the guarded parser. unknown = network/auth (R20).
    """
    # Mirror production: ensure the output=None guard is ACTIVE (do not strip it).
    try:
        from agent.openai_codex_compat import apply_codex_output_none_guard
        apply_codex_output_none_guard()
    except Exception:  # pragma: no cover - guard import must never break the probe
        pass
    try:
        delta_text: list[str] = []
        message_items = 0
        with client.responses.stream(
            model=model,
            instructions="Reply with the single token: ok",
            input=[{"role": "user", "content": "ping"}],
            store=False,
        ) as stream:
            for event in stream:
                etype = getattr(event, "type", "")
                if etype == "response.output_text.delta":
                    d = getattr(event, "delta", None)
                    if isinstance(d, str) and d:
                        delta_text.append(d)
                elif etype == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if getattr(item, "type", None) == "message":
                        message_items += 1
            final = stream.get_final_response()  # guarded: no crash on output=None
        out = getattr(final, "output", None)
        if out is not None and not isinstance(out, list):
            return ProbeResult(False, f"Codex /responses output not a list: {type(out).__name__}")
        list_items = len(out) if isinstance(out, list) else 0
        text = "".join(delta_text) or (getattr(final, "output_text", "") or "")
        if (isinstance(text, str) and text.strip()) or message_items or list_items:
            shape = f"output[{list_items}]" if list_items else "output=None+deltas (guard active)"
            return ProbeResult(True, f"Codex /responses delivers content ({len(text)} chars, {shape})")
        # Round-tripped but produced nothing the guard can backfill.
        return ProbeResult(False, "Codex /responses returned no content (empty output, 0 deltas/items)")
    except TypeError as exc:
        # A TypeError even WITH the guard applied = a NEW unhandled shape, not R57.
        return ProbeResult(False, f"Codex parse failed despite output=None guard (new shape?): {str(exc)[:140]}")
    except Exception as exc:
        return ProbeResult(None, f"Codex probe inconclusive: {type(exc).__name__}: {str(exc)[:140]}")


def check_anthropic_conformance(client: Any, model: str) -> ProbeResult:
    try:
        resp = client.messages.create(
            model=model, max_tokens=16,
            messages=[{"role": "user", "content": "ping"}],
        )
        content = getattr(resp, "content", None)
        if content is None:
            return ProbeResult(False, "Anthropic Messages content=None (contract drift)")
        if not isinstance(content, list):
            return ProbeResult(False, f"Anthropic content not a list: {type(content).__name__}")
        return ProbeResult(True, f"Anthropic content ok ({len(content)} blocks)")
    except Exception as exc:
        return ProbeResult(None, f"Anthropic probe inconclusive: {type(exc).__name__}: {str(exc)[:160]}")


def _read_manifest_harness_key() -> Optional[str]:
    try:
        raw = _MANIFEST_HARNESS_KEY_FILE.read_text(encoding="utf-8").strip()
        return raw or None
    except Exception:
        return None


def build_manifest_harness_probe_client() -> "Optional[tuple[Any, str]]":
    """Return a raw OpenAI client pointed at the Manifest "Laptop Monitor"
    harness (base_url /v1/responses, model "auto") + model, or None if the
    harness key file is missing/empty. Read-only; the caller issues one cheap
    request per run.

    This replaces the raw-Anthropic arm so the canary no longer hammers
    api.anthropic.com directly (429 under the background agent fleet) — the
    harness lets Manifest's fallback chain route to a healthy provider.
    """
    key = _read_manifest_harness_key()
    if not key:
        return None
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=_MANIFEST_HARNESS_BASE_URL)
    return client, _MANIFEST_HARNESS_MODEL


def check_manifest_harness_conformance(client: Any, model: str) -> ProbeResult:
    """Assert the Manifest harness DELIVERS CONTENT via its OpenAI-compatible
    /v1/responses endpoint (model "auto"). healthy = status 'completed' with a
    non-empty assistant text; down = completed but empty text; unknown =
    network/auth (R20, never contract drift)."""
    try:
        resp = client.responses.create(model=model, input="ping", store=False)
        status = getattr(resp, "status", None)
        out_text = ""
        for item in getattr(resp, "output", None) or []:
            if getattr(item, "type", None) == "message":
                for c in getattr(item, "content", None) or []:
                    if getattr(c, "type", None) == "output_text":
                        out_text += getattr(c, "text", "") or ""
        routed = getattr(resp, "model", model)
        if status == "completed" and out_text.strip():
            return ProbeResult(True, f"Manifest harness delivers content via {routed} ({len(out_text)} chars)")
        if status == "completed":
            return ProbeResult(False, f"Manifest harness completed with no text (model {routed})")
        return ProbeResult(False, f"Manifest harness status not completed: {status}")
    except Exception as exc:
        return ProbeResult(None, f"Manifest harness probe inconclusive: {type(exc).__name__}: {str(exc)[:160]}")


# sentinel + edge-triggered emit
def _load_state() -> dict:
    try:
        return json.loads(_sentinel_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_sentinel(results: dict, emit_meta: Optional[dict]) -> None:
    path = _sentinel_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = _load_state()
    backends = {}
    for backend, res in results.items():
        backends[backend] = {"state": res.state, "detail": res.detail}
    payload = {
        "ts": _now().isoformat(),
        "backends": backends,
        "emit_meta": emit_meta if emit_meta is not None else prev.get("emit_meta", {}),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _maybe_emit(bus: Any, backend: str, res: ProbeResult, prev: dict, emit_meta: dict) -> None:
    """Edge-trigger BACKEND_CONTRACT_DRIFT and update emit_meta IN PLACE.

    Emits at Priority.HIGH on healthy->down, then at most one re-page per
    _REPAGE_SECONDS while still down; emits a 'recovered' event on down->healthy.
    Mutates the SHARED ``emit_meta`` dict in place (NOT a per-call copy) so that
    when both backends change state in one cycle, neither reverts the other's
    ``last_drift_emit`` (which would break the hourly re-page cap). ``prev`` is
    the loaded prior sentinel (read-only, used only for prev_state)."""
    from events.schema import EventType, Priority

    prev_backends = (prev or {}).get("backends", {})
    prev_state = (prev_backends.get(backend) or {}).get("state", "healthy")
    last_emit_iso = emit_meta.get(backend, {}).get("last_drift_emit")

    def _emit(state: str):
        bus.emit(
            event_type=EventType.BACKEND_CONTRACT_DRIFT,
            source="backend-canary",
            payload={"backend": backend, "state": state, "detail": res.detail},
            priority=Priority.HIGH,
        )

    if res.state == "down":
        should = prev_state != "down"
        if not should and last_emit_iso:
            try:
                age = (_now() - datetime.fromisoformat(last_emit_iso)).total_seconds()
                should = age >= _REPAGE_SECONDS
            except Exception:
                should = True
        if should:
            _emit("down")
            emit_meta[backend] = {"last_drift_emit": _now().isoformat()}
    elif res.state == "healthy" and prev_state == "down":
        _emit("recovered")
        emit_meta[backend] = {}


def run_canary(*, bus: Any = None) -> dict:
    """Run both backend probes, emit on transitions, write the sentinel.
    Defensive: a failure in one backend never blocks the other or the sentinel."""
    if bus is None:
        try:
            from events.bus import EventBus
            bus = EventBus()
        except Exception:
            bus = None

    from agent.auxiliary_client import build_codex_probe_client

    results: dict = {}
    cc = None
    try:
        cc = build_codex_probe_client()
    except Exception as exc:
        logger.debug("codex probe client build failed: %s", exc)
    results["codex"] = (check_codex_conformance(*cc) if cc
                        else ProbeResult(None, "no Codex token configured"))

    mh = None
    try:
        mh = build_manifest_harness_probe_client()
    except Exception as exc:
        logger.debug("manifest harness probe client build failed: %s", exc)
    results["anthropic"] = (check_manifest_harness_conformance(*mh) if mh
                            else ProbeResult(None, "Manifest Laptop Monitor harness not configured"))

    prev = _load_state()
    emit_meta = dict(prev.get("emit_meta", {}))
    if bus is not None:
        for backend, res in results.items():
            try:
                _maybe_emit(bus, backend, res, prev, emit_meta)
            except Exception:
                logger.debug("emit failed for %s", backend, exc_info=True)
    _write_sentinel(results, emit_meta=emit_meta)

    for backend, res in results.items():
        logger.info("canary %s: %s — %s", backend, res.state, res.detail)
    return results


def main() -> int:
    # stream=sys.stdout, NOT logging's default stderr. The wrapper
    # (~/.hermes/ops/canary/canary-backend-conformance.ps1) runs this under
    # PowerShell 5.1 as `& $python -m ... *>> $log`, and PS 5.1 wraps EVERY
    # native-command stderr write in a NativeCommandError ErrorRecord — ~5 lines
    # of `python.exe : ` / `At <script>:36 char:5` / CategoryInfo ceremony around
    # each ordinary INFO line (2872 of them in the log by 2026-07-28). This is
    # NOT a suppression: `*>>` still captures every stream, StreamHandler flushes
    # on each record, and the `logger.exception("canary run failed")` traceback
    # below now lands on stdout as plain readable text rather than a wrapped
    # ErrorRecord. stderr is deliberately left untouched, so an interpreter-level
    # failure (import error, hard crash) still emits a NativeCommandError — which
    # from here on actually MEANS something instead of firing every 10 minutes.
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        results = run_canary()
        return 1 if any(r.state == "down" for r in results.values()) else 0
    except Exception:
        logger.exception("canary run failed")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
