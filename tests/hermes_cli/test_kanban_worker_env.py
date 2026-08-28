import os
from pathlib import Path

from hermes_cli.kanban_worker_env import pin_worker_cwd_env

SOT = str(Path.home() / ".hermes/profiles/genealogy/sot")

def test_pins_abs_existing_dir(tmp_path):
    ws = str(tmp_path)
    env = {"TERMINAL_CWD": SOT}
    out = pin_worker_cwd_env(env, ws)
    assert out["HERMES_KANBAN_WORKSPACE"] == ws
    assert out["TERMINAL_CWD"] == ws
    assert out["TERMINAL_CWD"] != SOT

def test_skips_terminal_cwd_if_workspace_missing():
    env = {"TERMINAL_CWD": "/old"}
    ws = "/no/such/lab-workspace-dir"
    out = pin_worker_cwd_env(env, ws)
    assert out["HERMES_KANBAN_WORKSPACE"] == ws
    assert out["TERMINAL_CWD"] == "/old"


def test_kanban_db_imports_pin_helper():
    import inspect
    from hermes_cli import kanban_db
    src = inspect.getsource(kanban_db)
    assert "pin_worker_cwd_env" in src
