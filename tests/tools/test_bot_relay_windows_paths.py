r"""Windows-path viability and venv CLI resolution for bot relay (#93590).

Two failures on a Windows desktop install talking to a remote gateway:

1. ``waiter_command`` embeds the reply path into generated ``python -c``
   source with ``!r``. repr escapes each backslash, but the Windows
   execution layer the waiter runs under folds ``\\`` back to ``\`` —
   ``\\U`` in ``C:\\Users\\...`` then parses as a unicode escape and
   SyntaxErrors the whole script. The raw-string prefix keeps the folded
   single backslash a literal; POSIX paths contain no backslashes, so it
   is a no-op there, and ``\\'`` inside a raw literal still cannot
   terminate the string, so the injection defense from #93091's
   python -c hardening is unchanged.

2. ``local_delivery_command`` hardcoded ``"hermes"``, relying on PATH —
   which service contexts (systemd units, desktop launchers, non-login
   SSH shells) do not provide, so delivery died with ENOENT. It now
   resolves the CLI next to this gateway's own interpreter (the venv
   bin/Scripts sibling), falling back to the bare name. The #93091
   turn-lock recognition in bot_mode_dm matches the CLI element by
   basename so resolved absolute paths (and ``hermes.exe``) still take
   the per-profile lock.
"""

import ast
import shlex
from pathlib import Path

import tools.bot_mode_dm as bot_mode_dm
import tools.bot_relay as bot_relay


ENV = {"id": "d" * 32, "target_handle": "researcher", "target_connection": "ssh-vps"}


def _waiter_code(root, env=None) -> str:
    cmd = bot_relay.waiter_command(root, env or ENV)
    parts = shlex.split(cmd)
    return parts[parts.index("-c") + 1]


def _waiter_argv(root, env=None):
    """The argv payload after the `-c` code: [reply_path, label, wait_s, state_db, event_id].

    The merged waiter passes all user/route data as argv, never interpolated
    into the generated Python source (the #93590 backslash-folding and
    #93091 injection defects are structurally eliminated by that design).
    """
    cmd = bot_relay.waiter_command(root, env or ENV)
    parts = shlex.split(cmd)
    # parts: [python, -c, <code>, reply_path, label, wait_s, state_db, event_id]
    return parts[parts.index("-c") + 2 :]


def test_waiter_windows_path_compiles_after_backslash_folding():
    """A Windows reply path rides as argv, so no backslash is ever embedded
    in the generated Python source — the ``\\\\U`` SyntaxError class from
    #93590 cannot occur because the path is sys.argv[1], not a literal."""
    root = "C:\\Users\\joshu\\.hermes"
    code = _waiter_code(root)
    assert "C:" not in code  # the path is NOT in the source
    argv = _waiter_argv(root)
    # Build the expected value the same way relay_root() does — Path joins
    # with "/" even when the root string carries literal backslashes (Linux
    # CI treats them as ordinary characters, Windows does not).
    assert argv[0] == str(Path(root) / "bot_relay" / "replies" / f"{ENV['id']}.json")
    compile(code, "<waiter>", "exec")  # source stays clean regardless of path


def test_waiter_posix_path_and_label_values_roundtrip():
    """On POSIX the reply path and label ride argv unchanged."""
    root = Path("/tmp/hermes-home")
    argv = _waiter_argv(root)
    assert argv[0] == str(root / "bot_relay" / "replies" / f"{ENV['id']}.json")
    assert argv[1] == "@researcher on ssh-vps"
    assert argv[4] == ENV["id"]
    # The generated source has no user-data interpolation at all.
    code = _waiter_code(root)
    assert "sys.argv[1]" in code
    assert "sys.argv[2]" in code


def test_waiter_raw_prefix_keeps_injection_defense():
    """Hostile roster fields stay data: they ride argv, never source."""
    inj = {
        "id": "e" * 32,
        "target_handle": "researcher",
        "target_connection": "x'); __import__('sys').exit(2); print('x",
    }
    code = _waiter_code(Path("/tmp/hermes-home"), inj)
    compile(code, "<waiter>", "exec")
    calls = [
        n.func.id
        for n in ast.walk(ast.parse(code))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    # The generated waiter only calls stdlib by name; the payload's
    # __import__ must remain argv data, not a live call in source.
    assert "__import__" not in calls
    assert "__import__('sys')" not in code
    argv = _waiter_argv(Path("/tmp/hermes-home"), inj)
    assert argv[1] == "@researcher on x'); __import__('sys').exit(2); print('x"


def test_local_delivery_resolves_sibling_hermes(tmp_path, monkeypatch):
    import sys as _sys

    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    # On Windows the sibling entrypoint is hermes.exe; elsewhere hermes.
    sibling_name = "hermes.exe" if _sys.platform == "win32" else "hermes"
    sibling = bin_dir / sibling_name
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
