from argparse import Namespace
import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest


def _chat_args(**overrides):
    values = {"continue_last": None, "in_dir": None, "resume": "routed-session"}
    values.update(overrides)
    return Namespace(**values)


def test_cmd_chat_refuses_routed_session_even_with_legacy_environment_override(
    monkeypatch, capsys
):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(main_mod, "_resolve_use_tui", lambda _args: False)
    monkeypatch.setattr(main_mod, "_apply_safe_mode", lambda _args: None)
    monkeypatch.setattr(main_mod, "_resolve_continue_arg", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_mod, "_gateway_routed_session_owner", lambda _session_id: 4242)
    monkeypatch.setattr(
        main_mod,
        "_resolve_session_by_name_or_id",
        lambda _session_id: pytest.fail("legacy environment override bypassed refusal"),
    )
    monkeypatch.setenv("HERMES_ALLOW_GATEWAY_SESSION", "1")

    with pytest.raises(SystemExit) as excinfo:
        main_mod.cmd_chat(_chat_args())

    assert excinfo.value.code == 3
    assert capsys.readouterr().err == (
        "Refusing to attach to a session currently served by the gateway.\n"
    )


def test_gateway_session_attach_refusal_allows_when_no_live_owner(monkeypatch, capsys):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(main_mod, "_gateway_routed_session_owner", lambda _session_id: None)

    assert main_mod._refuse_gateway_routed_session_attach("routed-session") is None
    assert capsys.readouterr().err == ""


def test_gateway_routed_session_owner_reads_state_db_from_hermes_home(
    tmp_path, monkeypatch
):
    import hermes_cli.main as main_mod

    home = tmp_path / "profile"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE gateway_routing (entry_json TEXT)")
        conn.execute(
            "INSERT INTO gateway_routing VALUES (?)",
            (json.dumps({"session_id": "routed-session"}),),
        )

    def fake_run(command, **_kwargs):
        assert command == ["launchctl", "list", "ai.hermes.gateway"]
        return SimpleNamespace(stdout='"PID" = 4242;')

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(main_mod.os, "kill", lambda pid, signal: None)

    assert main_mod._gateway_routed_session_owner("routed-session") == 4242
