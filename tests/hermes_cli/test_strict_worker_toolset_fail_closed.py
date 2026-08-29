"""B2 — strict toolset resolution must fail closed BEFORE subprocess.Popen.

Behavior contract (PR #90820 Round 3):

A strict-readonly worker MUST NOT reach subprocess creation if the
reduced toolset cannot be resolved or validated. The strict path is
`_default_spawn(strict_readonly=True)`, which calls
`_resolve_strict_worker_toolsets(hermes_home)` BEFORE
``subprocess.Popen``. That resolver:

  * missing configuration (no HERMES_HOME) → raises ValueError
  * resolver exception (load_config / _get_platform_tools) → re-raises
  * malformed/empty unusable result → raises ValueError
  * valid reduced toolset → returns sorted list, cmdline gets --toolsets

Ordinary (non-strict) `_default_spawn` behavior is unchanged: it falls
back to the legacy resolver that returns None on failure and proceeds
without `--toolsets` — preserving the prior contract for non-strict
workers.

Tests exercise the REAL `_default_spawn` function with a stubbed
``subprocess.Popen`` so we can prove Popen is reachable in success
cases and UNREACHABLE in failure cases.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


# --------------------------------------------------------------------
# Direct resolver tests — these prove the resolver fails closed BEFORE
# _default_spawn gets to subprocess.Popen.
# --------------------------------------------------------------------

def test_strict_resolver_missing_hermes_home_raises(tmp_path, monkeypatch):
    """No HERMES_HOME → resolver raises ValueError before any worker spawn."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    from hermes_cli import kanban_db

    with pytest.raises(ValueError, match="HERMES_HOME"):
        kanban_db._resolve_strict_worker_toolsets(None)
    with pytest.raises(ValueError, match="HERMES_HOME"):
        kanban_db._resolve_strict_worker_toolsets("")


def test_strict_resolver_empty_unusable_result_raises(tmp_path, monkeypatch):
    """Profile resolves to NO toolsets → fail closed."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    # Create the kanban directory the production path expects, even though
    # the strict resolver does not touch it (it calls load_config which may).
    (home / "kanban").mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import kanban_db

    # Mock _get_platform_tools to return []
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools", lambda cfg, platform: []
    )
    # Drop the memo so get_default_hermes_root() honors our HERMES_HOME.
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None

    with pytest.raises(ValueError, match="NO"):
        kanban_db._resolve_strict_worker_toolsets(str(home))


def test_strict_resolver_exception_propagates(tmp_path, monkeypatch):
    """load_config or _get_platform_tools raises → strict resolver re-raises."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import kanban_db
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None

    def _boom(cfg, platform):
        raise RuntimeError("simulated resolver failure")

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools", _boom
    )

    with pytest.raises(RuntimeError, match="simulated resolver failure"):
        kanban_db._resolve_strict_worker_toolsets(str(home))


def test_strict_resolver_all_forbidden_raises(tmp_path, monkeypatch):
    """Every resolved toolset is in the strict-forbidden set → fail closed."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import kanban_db
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda cfg, platform: ["terminal", "code_execution"],
    )

    with pytest.raises(ValueError, match="forbidden"):
        kanban_db._resolve_strict_worker_toolsets(str(home))


def test_strict_resolver_filters_forbidden_toolsets(tmp_path, monkeypatch):
    """Valid profile: forbidden toolsets are filtered out, kanban preserved."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import kanban_db
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda cfg, platform: ["terminal", "kanban", "web", "code_execution"],
    )

    reduced = kanban_db._resolve_strict_worker_toolsets(str(home))
    assert "kanban" in reduced, "kanban must be preserved so worker can self-complete"
    assert "terminal" not in reduced
    assert "code_execution" not in reduced
    # Output is sorted.
    assert reduced == sorted(reduced)


# --------------------------------------------------------------------
# Spawn-path tests — instrument subprocess.Popen to prove strict
# failures block Popen and successes reach Popen with --toolsets.
# --------------------------------------------------------------------

class _FakePopen:
    """Tracks whether Popen was called and with what argv."""
    instances = []
    popen_calls = 0

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        type(self).popen_calls += 1
        type(self).instances.append(self)
        self.pid = 99999 + len(type(self).instances)
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


@pytest.fixture
def fake_popen(monkeypatch):
    """Replace subprocess.Popen with a tracker so we can prove strict failures
    do NOT reach Popen while non-strict + valid paths DO reach Popen."""
    # Reset call counter
    _FakePopen.popen_calls = 0
    _FakePopen.instances = []

    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    return _FakePopen


def _make_task(strict_readonly: bool = False):
    """Build a minimal Task stub compatible with ``_default_spawn``.

    All required positional/keyword fields are filled with inert defaults
    (None / 0 / "") so we exercise the spawn path with the smallest
    real-shape Task the production code accepts.
    """
    from hermes_cli.kanban_db import Task

    return Task(
        id="t_b2",
        title="strict demo",
        body="",
        assignee="worker",
        status="running",
        priority=0,
        created_by="test",
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path="/tmp/does-not-matter",
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        strict_readonly=strict_readonly,
    )


def _ensure_fake_home(tmp_path):
    """Set HERMES_HOME to a writable tempdir so _resolve_* doesn't blow up
    on missing dirs. We mock _get_platform_tools separately."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "kanban").mkdir(exist_ok=True)
    (home / "profiles" / "worker").mkdir(parents=True, exist_ok=True)
    (home / "profiles" / "worker" / "config.yaml").write_text(
        "model: anthropic/claude-sonnet-4\n", encoding="utf-8"
    )
    os.environ["HERMES_HOME"] = str(home)
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None


def test_strict_worker_without_toolset_pin_does_not_spawn(
    tmp_path, monkeypatch, fake_popen
):
    """Strict worker with missing HERMES_HOME → ValueError, Popen NEVER called."""
    _ensure_fake_home(tmp_path)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    from hermes_cli import kanban_db

    task = _make_task(strict_readonly=True)
    with pytest.raises(ValueError, match="HERMES_HOME"):
        kanban_db._default_spawn(
            task, "/tmp/workspace", board="b_demo", strict_readonly=True
        )
    # The contract: Popen was NEVER reached.
    assert fake_popen.popen_calls == 0, (
        f"strict worker with missing HERMES_HOME must NOT reach Popen; "
        f"saw {fake_popen.popen_calls} Popen call(s)."
    )


def test_strict_worker_with_resolver_exception_does_not_spawn(
    tmp_path, monkeypatch, fake_popen
):
    """Strict worker whose resolver raises → exception propagates, Popen NEVER called."""
    _ensure_fake_home(tmp_path)

    def _boom(cfg, platform):
        raise RuntimeError("resolver explosion")

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools", _boom
    )

    from hermes_cli import kanban_db

    task = _make_task(strict_readonly=True)
    with pytest.raises(RuntimeError, match="resolver explosion"):
        kanban_db._default_spawn(
            task, "/tmp/workspace", board="b_demo", strict_readonly=True
        )
    assert fake_popen.popen_calls == 0, (
        "strict worker with a resolver exception must NOT reach Popen"
    )


def test_strict_worker_with_empty_toolsets_does_not_spawn(
    tmp_path, monkeypatch, fake_popen
):
    """Strict worker whose resolver returns empty → ValueError, Popen NEVER called."""
    _ensure_fake_home(tmp_path)
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools", lambda cfg, platform: []
    )

    from hermes_cli import kanban_db

    task = _make_task(strict_readonly=True)
    with pytest.raises(ValueError, match="NO"):
        kanban_db._default_spawn(
            task, "/tmp/workspace", board="b_demo", strict_readonly=True
        )
    assert fake_popen.popen_calls == 0


def test_strict_worker_with_all_forbidden_does_not_spawn(
    tmp_path, monkeypatch, fake_popen
):
    """Strict worker whose resolved toolset is entirely forbidden → fail closed."""
    _ensure_fake_home(tmp_path)
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda cfg, platform: ["terminal", "code_execution"],
    )

    from hermes_cli import kanban_db

    task = _make_task(strict_readonly=True)
    with pytest.raises(ValueError, match="forbidden"):
        kanban_db._default_spawn(
            task, "/tmp/workspace", board="b_demo", strict_readonly=True
        )
    assert fake_popen.popen_calls == 0


def test_strict_worker_with_valid_toolsets_spawns_with_pin(
    tmp_path, monkeypatch, fake_popen
):
    """Strict worker with a valid reduced toolset → Popen IS called and the
    cmdline carries the strict ``--toolsets`` flag with forbidden toolsets
    removed."""
    _ensure_fake_home(tmp_path)
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda cfg, platform: ["terminal", "kanban", "web", "code_execution"],
    )

    from hermes_cli import kanban_db

    task = _make_task(strict_readonly=True)
    pid = kanban_db._default_spawn(
        task, "/tmp/workspace", board="b_demo", strict_readonly=True
    )
    assert fake_popen.popen_calls == 1
    argv = fake_popen.instances[0].argv
    # Find the --toolsets arg and the following one.
    assert "--toolsets" in argv, f"strict worker must carry --toolsets; argv={argv}"
    idx = argv.index("--toolsets")
    toolsets_value = argv[idx + 1]
    toolsets_list = toolsets_value.split(",")
    assert "terminal" not in toolsets_list, "terminal must be filtered out"
    assert "code_execution" not in toolsets_list
    assert "kanban" in toolsets_list, "kanban must be preserved"
    # The strict env pin is set.
    env = fake_popen.instances[0].kwargs["env"]
    assert env.get("HERMES_KANBAN_STRICT_READONLY") == "1"
    assert pid == fake_popen.instances[0].pid


def test_non_strict_worker_unchanged_on_resolver_failure(
    tmp_path, monkeypatch, fake_popen
):
    """Non-strict worker: legacy resolver failure → Popen still fires (no --toolsets)."""
    _ensure_fake_home(tmp_path)
    # Simulate the legacy resolver returning None by passing an empty
    # HERMES_HOME so _resolve_worker_cli_toolsets returns None.
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda cfg, platform: [],  # empty → legacy returns None
    )

    from hermes_cli import kanban_db

    task = _make_task(strict_readonly=False)
    pid = kanban_db._default_spawn(
        task, "/tmp/workspace", board="b_demo", strict_readonly=False
    )
    # Legacy: Popen still happens. No --toolsets (since resolver returned None).
    assert fake_popen.popen_calls == 1
    argv = fake_popen.instances[0].argv
    assert "--toolsets" not in argv
    # And the strict env pin is NOT set for non-strict workers.
    env = fake_popen.instances[0].kwargs["env"]
    assert "HERMES_KANBAN_STRICT_READONLY" not in env
    assert pid == fake_popen.instances[0].pid


def test_strict_task_production_dispatch_retains_authority_to_popen(
    tmp_path, monkeypatch, fake_popen
):
    """A strict task survives create -> reload -> claim -> dispatcher -> Popen.

    This deliberately uses the real task persistence and dispatch loop rather
    than calling ``_default_spawn(..., strict_readonly=True)`` directly.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "platform_toolsets:\n  cli:\n    - terminal\n    - kanban\n    - file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None

    from hermes_cli import kanban_db

    kanban_db.init_db()
    monkeypatch.setattr(kanban_db, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "worker")
    monkeypatch.setattr(kanban_db, "_memory_pressure_level", lambda: "unknown")

    conn = kanban_db.connect()
    try:
        task_id = kanban_db.create_task(
            conn,
            title="strict lifecycle",
            assignee="worker",
            strict_readonly=True,
        )
        dispatched = kanban_db._dispatch_once_locked(
            conn, max_spawn=1, reconcile_orphans=False
        )
        assert dispatched.spawned and dispatched.spawned[0][0] == task_id
    finally:
        conn.close()

    assert fake_popen.popen_calls == 1
    env = fake_popen.instances[0].kwargs["env"]
    argv = fake_popen.instances[0].argv
    assert env["HERMES_KANBAN_STRICT_READONLY"] == "1"
    assert "--toolsets" in argv
    pinned = argv[argv.index("--toolsets") + 1].split(",")
    assert "terminal" not in pinned
    assert "kanban" in pinned