"""Tests for the complete.suggest RPC (composer ghost suggestions, slice 1)."""

from __future__ import annotations

import tui_gateway.server as srv


def _call(params: dict) -> dict:
    envelope = srv._methods["complete.suggest"](1, params)
    return envelope["result"]


def _install_session(monkeypatch, history, version=7):
    session = {"history": history, "history_version": version, "history_lock": None}
    monkeypatch.setitem(srv._sessions, "sess-1", session)
    return session


def _cfg_on(monkeypatch, enabled=True):
    monkeypatch.setattr(
        srv, "_load_cfg", lambda: {"display": {"composer_suggestions": enabled}}
    )


def test_unknown_session_returns_empty(monkeypatch):
    _cfg_on(monkeypatch)
    res = _call({"session_id": "nope"})
    assert res == {"candidates": [], "history_version": 0}


def test_config_off_returns_empty(monkeypatch):
    _cfg_on(monkeypatch, enabled=False)
    _install_session(
        monkeypatch,
        [{"role": "assistant", "content": "Want me to apply the fix?"}],
    )
    res = _call({"session_id": "sess-1"})
    assert res["candidates"] == []


def test_confirm_ask_yields_candidates_and_version(monkeypatch):
    _cfg_on(monkeypatch)
    _install_session(
        monkeypatch,
        [
            {"role": "user", "content": "fix it"},
            {"role": "assistant", "content": "Done. Want me to apply the fix?"},
        ],
        version=42,
    )
    res = _call({"session_id": "sess-1"})
    assert res["history_version"] == 42
    assert {"text": "Yes, go ahead", "kind": "confirm"} in res["candidates"]


def test_last_assistant_message_wins_over_earlier_ones(monkeypatch):
    _cfg_on(monkeypatch)
    _install_session(
        monkeypatch,
        [
            {"role": "assistant", "content": "Want me to delete everything?"},
            {"role": "user", "content": "no"},
            {"role": "assistant", "content": "Fair enough. All done here."},
        ],
    )
    res = _call({"session_id": "sess-1"})
    assert res["candidates"] == []


def test_structured_content_blocks_are_coerced(monkeypatch):
    _cfg_on(monkeypatch)
    _install_session(
        monkeypatch,
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Report saved to `~/out/report.xlsx`."},
                    {"type": "text", "text": "Is that the right file?"},
                ],
            }
        ],
    )
    res = _call({"session_id": "sess-1"})
    assert any(c["text"] == "~/out/report.xlsx" for c in res["candidates"])
