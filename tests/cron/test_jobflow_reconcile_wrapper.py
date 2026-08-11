"""Cron wrapper for JobFlow reconciliation.

Same stdout contract as the other converted wrappers: the cron script slot
delivers stdout verbatim and its last non-empty line is read as the wake gate.
An idle reconcile must therefore emit exactly ``{"wakeAgent": false}`` and
nothing else, or every run becomes a message and an agent session.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

WRAPPER = (
    pathlib.Path.home() / ".hermes" / "profiles" / "main" / "scripts"
    / "jobflow_reconcile.py"
)


def _load():
    if not WRAPPER.is_file():
        pytest.skip(f"wrapper not present at {WRAPPER}")
    spec = importlib.util.spec_from_file_location("jobflow_reconcile", WRAPPER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return mod


@pytest.fixture
def wrapper(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "MAILBOX_ROOT", tmp_path / "mailbox")
    monkeypatch.setattr(mod, "LEDGER_PATH", tmp_path / "dispatch.db")
    return mod


def _last_line(out: str) -> dict:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return json.loads(lines[-1])


def test_nothing_to_do_emits_only_the_closed_gate(wrapper, capsys):
    assert wrapper.main() == 0
    out = capsys.readouterr().out
    assert _last_line(out) == {"wakeAgent": False}
    assert out.strip() == '{"wakeAgent": false}'


def test_pending_work_opens_the_gate_and_names_the_activity(wrapper, capsys):
    inbox = wrapper.MAILBOX_ROOT / "tailor" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "20260810T01_TAILOR_REQUEST_main_aa.json").write_text("{}", encoding="utf-8")

    assert wrapper.main() == 0
    out = capsys.readouterr().out
    assert _last_line(out) == {"wakeAgent": True}
    assert "jobflow.tailor.generate" in out


def test_message_bodies_never_reach_stdout(wrapper, capsys):
    inbox = wrapper.MAILBOX_ROOT / "tailor" / "inbox"
    inbox.mkdir(parents=True)
    secret = "sk-live-must-not-appear"
    (inbox / "20260810T01_TAILOR_REQUEST_main_aa.json").write_text(
        json.dumps({"token": secret}), encoding="utf-8"
    )

    wrapper.main()
    assert secret not in capsys.readouterr().out


def test_scan_failure_opens_the_gate_rather_than_going_quiet(wrapper, monkeypatch, capsys):
    """A broken reconciler must never look identical to a clean one."""
    def _boom(*_a, **_k):
        raise OSError("disk gone with secret=must-not-appear")

    monkeypatch.setattr(wrapper, "scan_actionable", _boom)

    assert wrapper.main() == 0
    out = capsys.readouterr().out
    assert _last_line(out)["wakeAgent"] is True
    assert _last_line(out)["errors"] == 1
    assert "must-not-appear" not in out
