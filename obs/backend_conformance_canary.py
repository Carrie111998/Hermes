"""Synthetic backend-conformance canary (SR-470 / ADR-0024 §2).

R57 (2026-05-28): the Codex /responses backend began streaming a
``response.completed`` whose ``output`` is ``None``; the openai SDK parser
crashed, the agent loop classified it non-retryable, and 14/31 crons went down
for hours with no alert. Version-currency monitoring cannot catch this class —
no component version changed. The only detector that would have caught it is a
synthetic canary that issues one real round-trip and asserts on *shape*.

This canary (run by a Windows Scheduled Task every ~10 min):
  1. builds a RAW Codex client + a native Anthropic client (read-only),
  2. forces the STOCK (un-guarded) openai parser in-process so the SR-467
     output=None guard cannot mask the drift,
  3. issues one minimal request per backend and asserts ``output`` (Codex) /
     ``content`` (Anthropic) is a non-None iterable,
  4. writes a sentinel JSON ``~/.hermes/canary/backend_conformance.json`` that
     laptop-monitor.ps1 surfaces as a probe, and
  5. EDGE-TRIGGERS a BACKEND_CONTRACT_DRIFT bus event on healthy->down (and at
     most one re-page/hour while down) so notification volume is bounded.

A network/auth error is NOT contract drift (that is R20 OAuth) — it is reported
``unknown`` (inconclusive), never ``down``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Re-page interval while a backend stays in drift (seconds). Bounds volume.
_REPAGE_SECONDS = 3600


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


# force stock parser so the SR-467 guard never masks real drift
def _ensure_stock_parser() -> None:
    """Undo any output=None guard installed in THIS process so the canary sees
    the backend's raw shape through the SDK's OWN parser (ADR-0024 §2)."""
    targets = (
        "openai.lib._parsing._responses",
        "openai.lib.streaming.responses._responses",
        "openai.resources.responses.responses",
    )
    import importlib
    base = None
    try:
        src = importlib.import_module("openai.lib._parsing._responses")
        fn = getattr(src, "parse_response", None)
        base = getattr(fn, "__wrapped__", None)
    except Exception:
        return
    if base is None:
        return  # already stock
    import sys
    for name in targets:
        mod = sys.modules.get(name)
        if mod is not None and getattr(getattr(mod, "parse_response", None),
                                      "_hermes_codex_output_none_guard", False):
            mod.parse_response = base


# per-backend conformance checks
def check_codex_conformance(client: Any, model: str) -> ProbeResult:
    _ensure_stock_parser()
    try:
        _UNSET = object()
        raw_completed = _UNSET
        with client.responses.stream(
            model=model,
            instructions="Reply with the single token: ok",
            input=[{"role": "user", "content": "ping"}],
            store=False,
        ) as stream:
            for event in stream:
                if getattr(event, "type", "") == "response.completed":
                    raw_completed = getattr(getattr(event, "response", None),
                                            "output", _UNSET)
            final = stream.get_final_response()  # stock parser raises if output=None
        out = getattr(final, "output", None)
        if out is None or raw_completed is None:
            return ProbeResult(False, "Codex /responses completed with output=None (R57 signature)")
        if not isinstance(out, list):
            return ProbeResult(False, f"Codex /responses output not a list: {type(out).__name__}")
        return ProbeResult(True, f"Codex /responses output ok ({len(out)} items)")
    except TypeError as exc:
        # Stock parser choking on output=None — the exact R57 crash.
        return ProbeResult(False, f"Codex stock parser crashed: {str(exc)[:160]}")
    except Exception as exc:
        return ProbeResult(None, f"Codex probe inconclusive: {type(exc).__name__}: {str(exc)[:160]}")


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


def _maybe_emit(bus: Any, backend: str, res: ProbeResult, _load_prev: dict) -> Optional[dict]:
    """Edge-trigger: emit BACKEND_CONTRACT_DRIFT on healthy->down and at most
    one re-page/hour while down; emit a recovery on down->healthy. Returns the
    updated emit_meta dict for this backend (or None to leave unchanged)."""
    from events.schema import EventType, Priority

    prev_backends = (_load_prev or {}).get("backends", {})
    prev_state = (prev_backends.get(backend) or {}).get("state", "healthy")
    emit_meta = dict((_load_prev or {}).get("emit_meta", {}))
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
    return emit_meta


def run_canary(*, bus: Any = None) -> dict:
    """Run both backend probes, emit on transitions, write the sentinel.
    Defensive: a failure in one backend never blocks the other or the sentinel."""
    if bus is None:
        try:
            from events.bus import EventBus
            bus = EventBus()
        except Exception:
            bus = None

    from agent.auxiliary_client import build_codex_probe_client, build_anthropic_probe_client

    results: dict = {}
    cc = None
    try:
        cc = build_codex_probe_client()
    except Exception as exc:
        logger.debug("codex probe client build failed: %s", exc)
    results["codex"] = (check_codex_conformance(*cc) if cc
                        else ProbeResult(None, "no Codex token configured"))

    ac = None
    try:
        ac = build_anthropic_probe_client()
    except Exception as exc:
        logger.debug("anthropic probe client build failed: %s", exc)
    results["anthropic"] = (check_anthropic_conformance(*ac) if ac
                            else ProbeResult(None, "Anthropic not configured"))

    prev = _load_state()
    merged_meta = dict(prev.get("emit_meta", {}))
    if bus is not None:
        for backend, res in results.items():
            try:
                m = _maybe_emit(bus, backend, res, _load_prev=prev)
                if m is not None:
                    merged_meta.update(m)
            except Exception:
                logger.debug("emit failed for %s", backend, exc_info=True)
    _write_sentinel(results, emit_meta=merged_meta)

    for backend, res in results.items():
        logger.info("canary %s: %s — %s", backend, res.state, res.detail)
    return results


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        results = run_canary()
        return 1 if any(r.state == "down" for r in results.values()) else 0
    except Exception:
        logger.exception("canary run failed")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
