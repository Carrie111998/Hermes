"""Every process termination must name the call site that asked for it.

`gateway.status.terminate_pid` is the single chokepoint all in-tree kills funnel
through -- there are a dozen callers (`hermes_cli/gateway.py`,
`hermes_cli/main.py`'s update-pause path, `hermes_cli/profiles.py`,
`gateway/run.py`, `gateway_windows._force_terminate_known_gateway_pids`, ...).

Attribution has to live HERE rather than at any one caller. Windows
`TerminateProcess` does not run `atexit`, so a force-killed gateway writes none
of its own exit records and simply vanishes: the killer is the only party that
can say a kill happened. Instrumenting a single call site was not enough --
after doing exactly that on 2026-08-18, the gateway turned over three more
times (25844 -> 29460 -> 36088) with no `gateway.force_kill` record, proving
the kills were coming through one of the OTHER callers.
"""

from __future__ import annotations

import gateway.status as gateway_status


def test_terminate_pid_records_the_kill_and_its_caller(monkeypatch):
    records: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        gateway_status,
        "write_diag",
        lambda tag, **kw: records.append((tag, kw)),
        raising=False,
    )
    monkeypatch.setattr(gateway_status, "_IS_WINDOWS", False)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(gateway_status.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    gateway_status.terminate_pid(4321, force=True)

    assert killed and killed[0][0] == 4321
    kills = [r for r in records if r[0] == "process.terminate"]
    assert kills, f"no process.terminate record; got {[r[0] for r in records]}"
    payload = kills[0][1]
    assert payload.get("victim_pid") == 4321
    assert payload.get("force") is True
    # The field that makes the record actionable: which of the dozen callers.
    caller = payload.get("caller")
    assert caller and "test_terminate_pid_attribution" in str(caller), (
        f"caller attribution missing or wrong: {caller!r}"
    )


def test_graceful_terminations_are_recorded_too(monkeypatch):
    """A graceful stop still ends a gateway; the log must not imply only force kills."""
    records: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        gateway_status, "write_diag", lambda tag, **kw: records.append((tag, kw)), raising=False
    )
    monkeypatch.setattr(gateway_status, "_IS_WINDOWS", False)
    monkeypatch.setattr(gateway_status.os, "kill", lambda pid, sig: None)

    gateway_status.terminate_pid(999, force=False)

    kills = [r for r in records if r[0] == "process.terminate"]
    assert kills and kills[0][1].get("force") is False
