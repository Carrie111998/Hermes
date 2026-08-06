"""The lifecycle guard must return a verdict, not raise, when `~` cannot expand.

`_resolve_terminal_script_path()` called `Path(candidate).expanduser()`, which
raises `RuntimeError` — not `OSError` — when the home directory cannot be
determined. The guard runs on *every* terminal command, so a command mentioning
a `~/`-relative script in an environment without HOME/USERPROFILE aborted the
whole tool call instead of producing a scan verdict.

Environments that hit this: processes started with `env -i`, service accounts,
stripped containers, and CI runners.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cron.lifecycle_guard import (
    contains_gateway_lifecycle_command_or_referenced_script,
)

HOME_VARS = ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")


@pytest.fixture
def no_home(monkeypatch):
    """Remove every variable Path.expanduser() consults."""
    for var in HOME_VARS:
        monkeypatch.delenv(var, raising=False)
    # Guard the premise: if expanduser still resolves, the test proves nothing.
    with pytest.raises(RuntimeError):
        Path("~/anything").expanduser()
    yield


@pytest.mark.parametrize(
    "command",
    [
        "bash ~/script.sh",
        "sh ~/nested/run.sh",
        "bash ~/a.sh && echo done",
    ],
)
def test_tilde_script_does_not_raise_without_home(no_home, command, tmp_path):
    """A `~`-relative script reference must not abort the guard."""
    verdict = contains_gateway_lifecycle_command_or_referenced_script(
        command, cwd=str(tmp_path)
    )
    assert verdict is False


def test_unrelated_command_unaffected_without_home(no_home, tmp_path):
    verdict = contains_gateway_lifecycle_command_or_referenced_script(
        "echo hello", cwd=str(tmp_path)
    )
    assert verdict is False


def test_dangerous_command_still_detected_without_home(no_home, tmp_path):
    """Degrading on `~` must not weaken detection of the real thing."""
    verdict = contains_gateway_lifecycle_command_or_referenced_script(
        "hermes gateway restart", cwd=str(tmp_path)
    )
    assert verdict is True


def test_referenced_script_still_scanned_without_home(tmp_path, monkeypatch):
    """With HOME absent, a *relative* script must still be read and scanned.

    Confirms the fallback only affects `~` expansion — the rest of the
    resolution path keeps working, so the guard does not silently stop
    inspecting script bodies.
    """
    for var in HOME_VARS:
        monkeypatch.delenv(var, raising=False)

    script = tmp_path / "danger.sh"
    script.write_text("hermes gateway restart\n", encoding="utf-8")

    verdict = contains_gateway_lifecycle_command_or_referenced_script(
        "bash danger.sh", cwd=str(tmp_path)
    )
    assert verdict is True


def test_home_present_behaviour_unchanged(tmp_path, monkeypatch):
    """Baseline: with HOME set, a tilde path resolves and yields a verdict."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    verdict = contains_gateway_lifecycle_command_or_referenced_script(
        "bash ~/missing.sh", cwd=str(tmp_path)
    )
    assert verdict is False


def test_tilde_script_body_scanned_when_home_present(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    script = tmp_path / "boom.sh"
    script.write_text("hermes gateway stop\n", encoding="utf-8")

    verdict = contains_gateway_lifecycle_command_or_referenced_script(
        "bash ~/boom.sh", cwd=str(tmp_path)
    )
    assert verdict is True


def test_missing_cwd_does_not_raise(no_home, tmp_path):
    """A deleted cwd must not turn into an exception either."""
    gone = tmp_path / "gone"
    gone.mkdir()
    os.rmdir(gone)

    verdict = contains_gateway_lifecycle_command_or_referenced_script(
        "bash ~/x.sh", cwd=str(gone)
    )
    assert verdict is False
