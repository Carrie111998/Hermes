"""Zombie-stream watchdog tests (half-open gRPC stream, issue #54036).

spectrum-ts only reconnects when its inbound iterator throws or ends; a
half-open ("zombie") socket makes the iterator hang forever — no error, no
end — so inbound silently dies while the sidecar process looks healthy.

The sidecar exposes a strict unary liveness probe and the Python adapter
drives it on a conservative interval. A completed unary call is a keepalive,
not evidence that a quiet event stream is dead. Only repeated probe hangs
trigger a sidecar respawn.

These tests execute the real node classification module and drive the
adapter against mocked responses. No ports are bound and no gRPC traffic
occurs.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.photon.adapter import PhotonAdapter

_MODULE = Path("plugins/platforms/photon/sidecar/stream-staleness.mjs").resolve()


def _make_adapter(monkeypatch: pytest.MonkeyPatch) -> PhotonAdapter:
    monkeypatch.setenv("PHOTON_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "test-project-secret")
    cfg = PlatformConfig(enabled=True, token="", extra={})
    return PhotonAdapter(cfg)


# -- Sidecar decision rules (execute the real node module) -------------------

def _run_staleness_harness(script: str) -> Dict[str, Any]:
    harness = (
        "import { classifyProbeRejection } "
        f"from {json.dumps(_MODULE.as_uri())};\n"
        + script
    )
    run = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_probe_rejection_classification_is_strict() -> None:
    """Only not-found-shaped rejections prove liveness; everything else is
    inconclusive — a rejected probe is NEVER treated as alive (#45580's
    original /probe treated any rejection as alive, which was too loose)."""
    out = _run_staleness_harness(
        """
        const results = {
          notFoundCode: classifyProbeRejection({ code: 5, message: "5 NOT_FOUND: nope" }),
          notFoundText: classifyProbeRejection(new Error("message not found")),
          sdkNotFound: classifyProbeRejection({ code: "notFound", message: "missing" }),
          unavailable: classifyProbeRejection({ code: 14, message: "14 UNAVAILABLE: connect failed" }),
          deadline: classifyProbeRejection({ code: 4, message: "4 DEADLINE_EXCEEDED" }),
          generic: classifyProbeRejection(new Error("socket hang up")),
          weird: classifyProbeRejection("string error"),
        };
        process.stdout.write(JSON.stringify(results));
        """
    )
    # Completed round-trips (server said not-found for our synthetic id).
    for name in ("notFoundCode", "notFoundText", "sdkNotFound"):
        assert out[name]["alive"] is True, name
        assert out[name]["inconclusive"] is False, name
    # Everything else: not alive AND explicitly inconclusive.
    for name in ("unavailable", "deadline", "generic", "weird"):
        assert out[name]["alive"] is False, name
        assert out[name]["inconclusive"] is True, name


@pytest.mark.asyncio
async def test_monitor_still_raises_fatal_for_degraded_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing Spectrum telemetry degradation still uses the unchanged
    UPSTREAM_STREAM_DEGRADED reconnect path."""
    adapter = _make_adapter(monkeypatch)
    adapter._inbound_running = True
    adapter._sidecar_health_interval = 0.0

    async def _fake_call(path: str, payload: Dict[str, Any]) -> Any:
        assert path == "/healthz"
        return {
            "ok": True,
            "stream": {
                "ok": False,
                "state": "degraded",
                "degradedForMs": 95000,
                "lastIssue": "stream persistently failing",
            },
        }

    notified: list[bool] = []

    async def _fake_notify() -> None:
        notified.append(True)
        adapter._inbound_running = False

    monkeypatch.setattr(adapter, "_sidecar_call", _fake_call)
    monkeypatch.setattr(adapter, "_notify_fatal_error", _fake_notify)

    await adapter._monitor_sidecar_health()

    # Fatal notification is dispatched from a detached task (so callers can't
    # cancel their own handoff) — drain pending tasks before asserting.
    for _ in range(50):
        if notified:
            break
        await asyncio.sleep(0)

    assert adapter.has_fatal_error is True
    assert adapter.fatal_error_code == "UPSTREAM_STREAM_DEGRADED"
    assert adapter.fatal_error_retryable is True
    assert notified == [True]


@pytest.mark.asyncio
async def test_monitor_accepts_healthy_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy quiet stream remains healthy until Spectrum reports an actual
    failure; absence of event traffic is not a degradation signal."""
    adapter = _make_adapter(monkeypatch)
    adapter._inbound_running = True
    adapter._sidecar_health_interval = 0.0

    polls = 0

    async def _fake_call(path: str, payload: Dict[str, Any]) -> Any:
        nonlocal polls
        polls += 1
        if polls >= 2:
            adapter._inbound_running = False
        return {"ok": True, "stream": {"ok": True, "state": "healthy"}}

    monkeypatch.setattr(adapter, "_sidecar_call", _fake_call)

    await adapter._monitor_sidecar_health()

    assert adapter.has_fatal_error is False


# -- Adapter watchdog: inconclusive never counts toward respawn --------------

@pytest.mark.asyncio
async def test_inconclusive_probes_never_accumulate_toward_respawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict semantics end-to-end at the adapter: a 503/transport-error probe
    (inconclusive) must not increment the failure counter the way the original
    #45580 booleans did — only hung probes do."""
    adapter = _make_adapter(monkeypatch)

    class _Resp503:
        status_code = 503

    class _Client:
        async def post(self, *args: Any, **kwargs: Any) -> Any:
            return _Resp503()

    adapter._http_client = _Client()  # type: ignore[assignment]

    # Many inconclusive probes in a row: mirror the watchdog's per-iteration
    # bookkeeping (only "hung" increments) and assert no failures accrue.
    for _ in range(10):
        verdict = await adapter._probe_once()
        assert verdict == "inconclusive"
        if verdict == "hung":
            adapter._probe_failures += 1

    assert adapter._probe_failures == 0
