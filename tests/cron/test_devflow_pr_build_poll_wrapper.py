"""Output contract for the DevFlow PR/build cron wrapper.

The cron ``script:`` slot delivers stdout VERBATIM, so anything printed on a
quiet tick becomes a Telegram message. At this job's cadence that is hundreds
of no-op notifications a day. The contract is therefore: stdout is EMPTY
unless something actually changed or broke; diagnostics go to stderr.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

WRAPPER = (
    pathlib.Path.home() / ".hermes" / "profiles" / "main" / "scripts"
    / "devflow_pr_build_poll.py"
)


def _load_wrapper():
    if not WRAPPER.is_file():
        pytest.skip(f"wrapper not present at {WRAPPER}")
    spec = importlib.util.spec_from_file_location("devflow_pr_build_poll", WRAPPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def wrapper(monkeypatch):
    mod = _load_wrapper()
    monkeypatch.setattr(mod, "EventBus", lambda db_path: None)
    return mod


def test_no_change_is_silent(wrapper, monkeypatch, capsys):
    monkeypatch.setattr(wrapper, "load_config", lambda: object())
    monkeypatch.setattr(wrapper, "run_poll", lambda bus, config: wrapper.PollSummary())

    assert wrapper.main() == 0
    assert capsys.readouterr().out == ""


def test_not_configured_is_silent(wrapper, monkeypatch, capsys):
    monkeypatch.setattr(wrapper, "load_config", lambda: None)

    assert wrapper.main() == 0
    assert capsys.readouterr().out == ""


def test_transition_produces_alert(wrapper, monkeypatch, capsys):
    summary = wrapper.PollSummary(
        repos_polled=1,
        transitions_emitted=1,
        transitions=[{"kind": "pr", "repo": "o/r", "id": "12", "state": "merged"}],
    )
    monkeypatch.setattr(wrapper, "load_config", lambda: object())
    monkeypatch.setattr(wrapper, "run_poll", lambda bus, config: summary)

    assert wrapper.main() == 0
    out = capsys.readouterr().out
    assert "o/r#12" in out
    assert "merged" in out


def test_build_transition_is_named_as_a_build(wrapper, monkeypatch, capsys):
    summary = wrapper.PollSummary(
        repos_polled=1,
        transitions_emitted=1,
        transitions=[{"kind": "build", "repo": "o/r", "id": "9001", "state": "failed"}],
    )
    monkeypatch.setattr(wrapper, "load_config", lambda: object())
    monkeypatch.setattr(wrapper, "run_poll", lambda bus, config: summary)

    wrapper.main()
    out = capsys.readouterr().out
    assert "o/r build 9001" in out
    assert "failed" in out


def test_errors_still_alert_even_with_no_transitions(wrapper, monkeypatch, capsys):
    """A broken poll must never be mistaken for a quiet one."""
    summary = wrapper.PollSummary(repos_polled=1, errors=["owner/repo: 403 forbidden"])
    monkeypatch.setattr(wrapper, "load_config", lambda: object())
    monkeypatch.setattr(wrapper, "run_poll", lambda bus, config: summary)

    assert wrapper.main() == 0
    out = capsys.readouterr().out
    assert "403 forbidden" in out


def test_config_error_alerts_on_stdout(wrapper, monkeypatch, capsys):
    def _raise():
        raise wrapper.PollerConfigError("repos.json is malformed")

    monkeypatch.setattr(wrapper, "load_config", _raise)

    assert wrapper.main() == 0
    out = capsys.readouterr().out
    assert "repos.json is malformed" in out
