r"""Windows-path viability and venv CLI resolution for bot relay.

Two failures on a Windows desktop install talking to a remote gateway:

1. ``waiter_command`` passes reply paths and labels as quoted argv values to
   the first-party ``bot_relay.py --wait-for-reply`` entrypoint. This avoids
   both Windows backslash folding and the generic approval gate for ``-c``.

2. ``local_delivery_command`` hardcoded ``"hermes"``, relying on PATH —
   which service contexts (systemd units, desktop launchers, non-login
   SSH shells) do not provide, so delivery died with ENOENT. It now
   resolves the CLI next to this gateway's own interpreter (the venv
   bin/Scripts sibling), falling back to the bare name. The #93091
   turn-lock recognition in bot_mode_dm matches the CLI element by
   basename so resolved absolute paths (and ``hermes.exe``) still take
   the per-profile lock.
"""

import shlex
import sys
from pathlib import Path

import tools.bot_mode_dm as bot_mode_dm
import tools.bot_relay as bot_relay


ENV = {"id": "d" * 32, "target_handle": "researcher", "target_connection": "ssh-vps"}


def _waiter_parts(root, env=None) -> list[str]:
    cmd = bot_relay.waiter_command(root, env or ENV)
    return shlex.split(cmd)


def test_waiter_windows_path_roundtrips_as_argv():
    parts = _waiter_parts(r"C:\Users\joshu\.hermes")
    assert parts[2] == "--wait-for-reply"
    assert parts[3] == rf"C:\Users\joshu\.hermes\bot_relay\replies\{ENV['id']}.json"
    assert "-c" not in parts


def test_waiter_posix_path_and_label_values_roundtrip():
    root = Path("/tmp/hermes-home")
    parts = _waiter_parts(root)
    expected = str(root / "bot_relay" / "replies" / f"{ENV['id']}.json")
    assert parts[3] == expected
    assert parts[4] == "@researcher on ssh-vps"


def test_waiter_argv_keeps_injection_defense():
    """Hostile roster fields stay one inert argv value."""
    inj = {
        "id": "e" * 32,
        "target_handle": "researcher",
        "target_connection": "x'); __import__('sys').exit(2); print('x",
    }
    parts = _waiter_parts(Path("/tmp/hermes-home"), inj)
    assert len(parts) == 5
    assert parts[4] == "@researcher on x'); __import__('sys').exit(2); print('x"


def test_local_delivery_resolves_sibling_hermes(tmp_path, monkeypatch):
    bin_dir = tmp_path / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
    bin_dir.mkdir(parents=True)
    sibling = bin_dir / ("hermes.exe" if sys.platform == "win32" else "hermes")
    sibling.touch()
    sibling.chmod(0o755)
    monkeypatch.setattr("sys.executable", str(bin_dir / "python"))

    argv = bot_relay.local_delivery_command("ops", "query.json")
    assert argv[0] == str(sibling)
    assert argv[1:3] == ["-p", "ops"]
    assert argv[argv.index("--query-file") + 1] == "query.json"


def test_local_delivery_uses_shutil_which_when_no_sibling(tmp_path, monkeypatch):
    """Without a venv sibling, a PATH hit (shutil.which) wins next —
    interactive shells keep resolving exactly what they resolve today."""
    empty = tmp_path / "nowhere"
    empty.mkdir(parents=True)
    monkeypatch.setattr("sys.executable", str(empty / "python"))
    which_hit = str(tmp_path / "usr-local-bin" / "hermes")
    monkeypatch.setattr(
        bot_relay.shutil, "which", lambda name: which_hit if name == "hermes" else None
    )

    argv = bot_relay.local_delivery_command("ops", "query.json")
    assert argv[0] == which_hit


def test_local_delivery_falls_back_to_bare_name(tmp_path, monkeypatch):
    empty = tmp_path / "nowhere"
    empty.mkdir(parents=True)
    monkeypatch.setattr("sys.executable", str(empty / "python"))
    monkeypatch.setattr(bot_relay.shutil, "which", lambda name: None)

    argv = bot_relay.local_delivery_command("ops", "query.json")
    assert argv[0] == "hermes"
    assert argv[1:3] == ["-p", "ops"]


def test_delivery_lock_recognizes_resolved_cli_paths(tmp_path, monkeypatch):
    """The #93091 per-profile turn lock must keep matching delivery argvs
    now that argv[0] may be a resolved absolute path (or hermes.exe)."""
    acquired = []

    class _Ctx:
        def __enter__(self):
            acquired.append("locked")
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(bot_relay, "acquire_turn_lock", lambda root, profile: _Ctx())
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with bot_mode_dm._delivery_lock(
        [str(tmp_path / "venv" / "bin" / "hermes"), "-p", "ops", "chat"],
        stdin_file=False,
    ):
        pass
    with bot_mode_dm._delivery_lock(["hermes", "-p", "ops", "chat"], stdin_file=False):
        pass
    with bot_mode_dm._delivery_lock(
        ["C:\\venv\\Scripts\\hermes.exe", "-p", "ops", "chat"], stdin_file=False
    ):
        pass
    assert acquired == ["locked", "locked", "locked"]

    # Unrelated argvs still bypass the lock entirely.
    with bot_mode_dm._delivery_lock(["python", "-m", "whatever"], stdin_file=False):
        pass
    assert acquired == ["locked", "locked", "locked"]
