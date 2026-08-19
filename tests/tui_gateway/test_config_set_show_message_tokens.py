"""config.set show_message_tokens — the persistent half of the TUI /tokens toggle.

Regression cover for the review finding on PR #55805: the Ink side
(``ui-tui/src/app/slash/commands/core.ts``) sends ``config.set`` with key
``show_message_tokens`` when the user runs ``/tokens always``, but the RPC had
no handler and fell through to ``unknown config key``. Because the caller
swallowed the rejection, ``/tokens always`` reported a persistence that never
happened and the preference silently vanished on restart.

The key writes ``display.show_message_tokens``, which is what
``useConfigSync``'s ``applyDisplay`` reads back into ``ui.showTokens``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tui_gateway.server as srv


def _call(params: dict) -> dict:
    """Invoke the config.set RPC and return the raw JSON-RPC envelope."""
    return srv._methods["config.set"](1, params)


@pytest.fixture
def cfg(monkeypatch):
    """Capture _write_config_key writes over a mutable in-memory config."""
    state: dict = {"display": {}}
    written: list[tuple[str, object]] = []

    def _fake_write(key_path: str, value):
        written.append((key_path, value))
        node = state
        parts = key_path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    monkeypatch.setattr(srv, "_load_cfg", lambda: state)
    monkeypatch.setattr(srv, "_write_config_key", _fake_write)
    return state, written


# ---------------------------------------------------------------------------
# The regression the review flagged
# ---------------------------------------------------------------------------


def test_key_is_not_rejected_as_unknown(cfg):
    """The exact payload the Ink /tokens always sends must be accepted."""
    env = _call({"key": "show_message_tokens", "value": "on"})
    assert "error" not in env, env
    assert env["result"]["key"] == "show_message_tokens"


def test_always_persists_under_display(cfg):
    """`/tokens always` -> display.show_message_tokens = True (what applyDisplay reads)."""
    state, written = cfg
    env = _call({"key": "show_message_tokens", "value": "on"})
    assert env["result"]["value"] == "on"
    assert ("display.show_message_tokens", True) in written
    assert state["display"]["show_message_tokens"] is True


# ---------------------------------------------------------------------------
# Value parsing — same grammar as the sibling boolean display keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["on", "true", "yes"])
def test_truthy_aliases(cfg, value):
    _, written = cfg
    assert _call({"key": "show_message_tokens", "value": value})["result"]["value"] == "on"
    assert written[-1] == ("display.show_message_tokens", True)


@pytest.mark.parametrize("value", ["off", "false", "no"])
def test_falsy_aliases(cfg, value):
    _, written = cfg
    assert _call({"key": "show_message_tokens", "value": value})["result"]["value"] == "off"
    assert written[-1] == ("display.show_message_tokens", False)


@pytest.mark.parametrize("value", ["", "toggle"])
def test_toggle_flips_current(cfg, value):
    state, written = cfg
    state["display"]["show_message_tokens"] = True
    assert _call({"key": "show_message_tokens", "value": value})["result"]["value"] == "off"
    assert written[-1] == ("display.show_message_tokens", False)


def test_toggle_from_absent_defaults_off_then_on(cfg):
    """No stored key reads as False, so a bare toggle turns it on."""
    _, written = cfg
    assert _call({"key": "show_message_tokens", "value": ""})["result"]["value"] == "on"
    assert written[-1] == ("display.show_message_tokens", True)


def test_invalid_value_errors_and_writes_nothing(cfg):
    _, written = cfg
    env = _call({"key": "show_message_tokens", "value": "sometimes"})
    assert env["error"]["code"] == 4002
    # Must be the value complaint, NOT the unknown-key fall-through — both
    # share code 4002, so assert on the message or this passes vacuously.
    assert env["error"]["message"] == "unknown show_message_tokens value: sometimes"
    assert written == []


# ---------------------------------------------------------------------------
# Restart / persistence — nothing below the RPC is stubbed
# ---------------------------------------------------------------------------
#
# The cases above stub ``_write_config_key`` and therefore prove only that the
# handler *asks* for a write. What the bug actually cost the user was the
# preference not being there after a restart, so that is what these pin: a real
# config.yaml on disk, and a real re-read through the path a fresh gateway
# process takes on boot.


def _reset_cfg_cache() -> None:
    """Drop the module-level config cache — the state a fresh process starts in.

    ``_load_cfg_raw`` memoises the parsed file under ``(path, mtime)``; clearing
    the three globals is what makes the next read hit the disk, so a test that
    skips this would be reading its own write back out of memory.
    """
    with srv._cfg_lock:
        srv._cfg_cache = None
        srv._cfg_mtime = None
        srv._cfg_path = None


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    """Point the gateway at a throwaway profile home holding a real config.yaml."""
    (tmp_path / "config.yaml").write_text(
        "display:\n  battery: false\n", encoding="utf-8"
    )
    monkeypatch.setattr(srv, "_hermes_home", tmp_path)
    monkeypatch.setattr(srv, "get_hermes_home_override", lambda: "")
    _reset_cfg_cache()
    yield tmp_path
    _reset_cfg_cache()


def test_survives_restart(profile_home):
    """`/tokens always` → still on after the process restarts."""
    env = _call({"key": "show_message_tokens", "value": "on"})
    assert "error" not in env, env

    # Durable on disk, not just in the cache.
    on_disk = (profile_home / "config.yaml").read_text(encoding="utf-8")
    assert "show_message_tokens" in on_disk

    _reset_cfg_cache()  # the restart
    assert srv._load_cfg()["display"]["show_message_tokens"] is True


def test_off_survives_restart(profile_home):
    """`/tokens off` must clear the persisted preference just as durably."""
    _call({"key": "show_message_tokens", "value": "on"})
    _reset_cfg_cache()
    _call({"key": "show_message_tokens", "value": "off"})

    _reset_cfg_cache()
    assert srv._load_cfg()["display"]["show_message_tokens"] is False


def test_restart_read_does_not_disturb_neighbours(profile_home):
    """The write is a round-trip, so a sibling display key must survive it."""
    _call({"key": "show_message_tokens", "value": "on"})

    _reset_cfg_cache()
    display = srv._load_cfg()["display"]
    assert display["show_message_tokens"] is True
    assert display["battery"] is False


def test_written_key_path_is_the_one_the_tui_reads(profile_home):
    """Pin the two halves of the round trip to the same literal.

    ``useConfigSync``'s ``applyDisplay`` reads ``show_message_tokens`` off the
    display block; if either side is renamed without the other, the toggle goes
    back to silently forgetting itself. Cheap to assert, so assert it.
    """
    _call({"key": "show_message_tokens", "value": "on"})
    _reset_cfg_cache()
    assert "show_message_tokens" in srv._load_cfg()["display"]

    sync = (
        Path(srv.__file__).resolve().parent.parent
        / "ui-tui"
        / "src"
        / "app"
        / "useConfigSync.ts"
    ).read_text(encoding="utf-8")
    assert "d.show_message_tokens" in sync
