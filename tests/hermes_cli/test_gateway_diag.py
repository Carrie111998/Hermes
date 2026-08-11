"""Tests for hermes_cli.gateway_diag — the gateway lifecycle diagnostic log.

The invariant under test is *when* records are written, not what they say. A
``gateway.start`` record that arrives after the expensive startup work is
useless for its actual purpose: detecting a double-spawn whose losing racer
dies during that very work (2026-08-10, PID 54392 — killed by the duplicate
guard, no trace in any log).
"""

import json
import os
import sys

import pytest

from hermes_cli import gateway_diag


def _records():
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "logs" / gateway_diag.DIAG_LOG_NAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["hermes", "gateway", "run"], True),
        (["hermes", "gateway"], True),  # bare `gateway` defaults to run
        (["hermes", "gateway", "run", "--replace"], True),
        (["hermes", "--profile", "main", "gateway", "run"], True),
        (["hermes", "gateway", "-v", "run"], True),
        # The detached Windows launcher's real argv.
        (["main.py", "--profile", "main", "gateway", "run"], True),
        (["hermes", "gateway", "status"], False),
        (["hermes", "gateway", "restart"], False),
        (["hermes", "gateway", "stop"], False),
        (["hermes", "gateway", "install", "--start-now"], False),
        (["hermes", "chat"], False),
        (["pytest", "tests/gateway/test_run.py"], False),
        (["hermes"], False),
        ([], False),
    ],
)
def test_argv_selects_gateway_run(argv, expected):
    assert gateway_diag.argv_selects_gateway_run(argv) is expected


def test_emit_gateway_spawn_diag_writes_for_a_gateway_run():
    argv = ["hermes", "--profile", "main", "gateway", "run"]

    assert gateway_diag.emit_gateway_spawn_diag(argv) is True

    records = _records()
    assert len(records) == 1
    record = records[0]
    assert record["tag"] == "gateway.spawn"
    assert record["pid"] == os.getpid()
    # The pre-strip argv: `_apply_profile_override()` removes `--profile` from
    # sys.argv before this runs, so reading sys.argv here would silently drop
    # it and misreport how the process was launched.
    assert record["argv"] == argv
    assert record["python"] == sys.version.split()[0]


def test_emit_gateway_spawn_diag_stays_silent_for_other_commands():
    assert gateway_diag.emit_gateway_spawn_diag(["hermes", "gateway", "status"]) is False
    assert _records() == []


def test_spawn_tag_is_distinct_from_start_tag():
    """The double-spawn signature is a PAIR of ``gateway.start`` records.

    A ``gateway.spawn`` record must never be mistakable for one, or the early
    record would manufacture the exact false positive it exists to catch.
    """
    gateway_diag.emit_gateway_spawn_diag(["hermes", "gateway", "run"])
    gateway_diag.write_diag("gateway.start", replace=False)

    tags = [r["tag"] for r in _records()]
    assert tags == ["gateway.spawn", "gateway.start"]
    assert tags.count("gateway.start") == 1


def test_diag_opt_out_silences_every_record(monkeypatch):
    monkeypatch.setenv(gateway_diag.DIAG_ENV_VAR, "0")

    assert gateway_diag.diag_enabled() is False
    gateway_diag.emit_gateway_spawn_diag(["hermes", "gateway", "run"])
    gateway_diag.write_diag("gateway.start")

    assert _records() == []


def test_write_diag_never_raises(monkeypatch):
    """A diagnostic must not be able to take the gateway down with it."""

    def exploding_home():
        raise RuntimeError("no home for you")

    monkeypatch.setattr("hermes_constants.get_hermes_home", exploding_home)

    gateway_diag.write_diag("gateway.start")  # must not raise


def test_process_start_age_is_a_small_positive_number():
    age = gateway_diag.process_start_age_s()

    if age is None:  # psutil unavailable — documented degradation
        pytest.skip("psutil not available")
    assert age >= 0
    # Sanity bound: this test process cannot have been alive for a day.
    assert age < 86_400
