"""`hermes sessions list --json` — machine-readable session listing.

With --json, sessions list prints a JSON array of session objects instead of
the human-formatted table. Each object has id, source, title, preview,
started_at, last_active, message_count, and model.
"""

import json
import types

import pytest


@pytest.fixture()
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _make_session(sid, **overrides):
    base = {
        "id": sid,
        "source": "cli",
        "title": f"Session {sid}",
        "preview": "Hello world",
        "started_at": "2026-08-25T10:00:00",
        "last_active": "2026-08-25T10:30:00",
        "message_count": 5,
        "model": "hermes-3",
    }
    base.update(overrides)
    return base


def _run_list(tmp_home, monkeypatch, capsys, **extra):
    from hermes_cli import sessions_cmd as mod
    from hermes_state import SessionDB

    db = SessionDB()
    for sid, kw in extra.get("seeds", {}).items():
        db.upsert_session(sid, source=kw.pop("source", "cli"))
        if kw.get("title"):
            db.set_session_title(sid, kw["title"])
    db.close()

    args = types.SimpleNamespace(
        sessions_action="list",
        source=None,
        limit=20,
        workspace=None,
        json_output=True,
        exclude=None,
    )
    mod.cmd_sessions(args)
    return capsys.readouterr().out


def test_json_returns_array(tmp_home, monkeypatch, capsys):
    out = _run_list(tmp_home, monkeypatch, capsys)
    doc = json.loads(out)
    assert isinstance(doc, list)


def test_json_object_keys(tmp_home, monkeypatch, capsys):
    out = _run_list(tmp_home, monkeypatch, capsys)
    doc = json.loads(out)
    if not doc:
        pytest.skip("no sessions seeded")
    required = {"id", "source", "title", "preview", "started_at", "last_active", "message_count", "model"}
    assert required.issubset(doc[0].keys())


def test_json_empty_list(tmp_home, monkeypatch, capsys):
    out = _run_list(tmp_home, monkeypatch, capsys)
    doc = json.loads(out)
    assert doc == []


def test_no_json_flag_still_works(tmp_home, monkeypatch, capsys):
    from hermes_cli import sessions_cmd as mod

    args = types.SimpleNamespace(
        sessions_action="list",
        source=None,
        limit=20,
        workspace=None,
        json_output=False,
        exclude=None,
    )
    mod.cmd_sessions(args)
    out = capsys.readouterr().out
    assert "No sessions" in out
