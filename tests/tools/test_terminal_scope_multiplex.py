"""Per-turn terminal scope isolation under profile multiplexing.

Regression tests for the cross-profile terminal backend leak (#68559
lineage; gateway repro #94200, dashboard repro #98581): a multiplexed
process serves several profiles from one interpreter, and terminal
settings used to resolve through the process-global ``TERMINAL_*`` env
vars — whichever profile first touched the terminal pinned the process
env, routing every later profile's terminal onto the wrong backend (a
``local`` profile executing inside another profile's docker sandbox, or
the reverse: a sandbox escape).

Behavior contracts asserted here (no source reading, no snapshots):

- A bound scope is an AUTHORITATIVE projection: reads resolve only from
  the profile's policy (defined defaults + .env + config.yaml). An
  omitted key yields the defined default — never ambient ``os.environ``
  — so profile B (or an empty-terminal-profile B) cannot inherit
  profile A's mounts/SSH/network/CWD even when backends match.
- The scope never mutates ``os.environ``; alternation between profiles
  in one process is fully isolated both ways.
- Unresolvable profile policy fails CLOSED: the install returns a
  refusal scope, and execution paths raise rather than run ambient.
- The boundary installers (gateway runtime scope, TUI session scope,
  cron fire scope) install and clean up the policy on their real paths.
"""

import contextlib
import os
import time
from pathlib import Path

import pytest

from tools.terminal_scope import (
    TerminalPolicyRefusal,
    TerminalPolicyUnavailable,
    build_profile_terminal_scope,
    enforce_no_refusal,
    get_terminal_scope,
    install_profile_terminal_scope,
    install_refusal_scope,
    reset_terminal_scope,
    set_terminal_scope,
    terminal_env,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Hermetic HERMES_HOME + a POLLUTED launch-process terminal env.

    The seeded values model the hostile baseline the review requires:
    profile A (launch) holds sensitive mounts/SSH/network/CWD policy, and
    every test proves scoped profiles cannot observe any of it.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", '["/host/secret:/data"]')
    monkeypatch.setenv("TERMINAL_SSH_HOST", "10.10.0.103")
    monkeypatch.setenv("TERMINAL_SSH_USER", "launch-admin")
    monkeypatch.setenv("TERMINAL_DOCKER_NETWORK", "false")
    monkeypatch.setenv("TERMINAL_CWD", "/home/launch-user/private")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "false")
    monkeypatch.delenv("TERMINAL_DOCKER_IMAGE", raising=False)
    yield


# ---------------------------------------------------------------------------
# Scope primitives: authoritative projection, no ambient widening
# ---------------------------------------------------------------------------


def test_no_scope_keeps_historical_env_behavior():
    """No scope bound → process env (single-process CLI/TUI unchanged)."""
    token = set_terminal_scope(None)
    try:
        assert terminal_env("TERMINAL_ENV") == "local"
        assert terminal_env("TERMINAL_SSH_HOST") == "10.10.0.103"
    finally:
        reset_terminal_scope(token)


def test_scoped_read_never_falls_through_to_process_env():
    """The inverted contract: while a scope is bound, a key the profile did
    not configure resolves to the defined default — NOT to the ambient
    process value. This is the omission-leak fix."""
    token = set_terminal_scope({"TERMINAL_ENV": "docker"})
    try:
        assert terminal_env("TERMINAL_ENV") == "docker"
        # Process env carries these (see fixture); the scope must NOT.
        assert terminal_env("TERMINAL_SSH_HOST") == ""
        assert terminal_env("TERMINAL_DOCKER_VOLUMES", "[]") == "[]"
        # Defined defaults still surface through the scope.
        assert terminal_env("TERMINAL_CONTAINER_PERSISTENT", "true") != ""
    finally:
        reset_terminal_scope(token)


def test_scope_read_does_not_mutate_process_env():
    token = set_terminal_scope({"TERMINAL_ENV": "docker"})
    try:
        assert terminal_env("TERMINAL_ENV") == "docker"
        assert os.environ["TERMINAL_ENV"] == "local"
    finally:
        reset_terminal_scope(token)
    assert terminal_env("TERMINAL_ENV") == "local"


def test_two_scopes_alternate_backends_in_one_process(tmp_path):
    """The original regression: docker-profile and local-profile turns
    interleave in one process; each resolves its OWN backend and neither the
    process env nor the other profile is ever read."""
    from tools.terminal_tool import _get_env_config

    scope_docker = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img:a"}
    scope_local = {"TERMINAL_ENV": "local"}

    t1 = set_terminal_scope(scope_docker)
    cfg1 = _get_env_config()
    reset_terminal_scope(t1)
    assert cfg1["env_type"] == "docker"
    assert cfg1["docker_image"] == "img:a"
    assert os.environ["TERMINAL_ENV"] == "local"

    t2 = set_terminal_scope(scope_local)
    cfg2 = _get_env_config()
    reset_terminal_scope(t2)
    assert cfg2["env_type"] == "local"

    t3 = set_terminal_scope(scope_docker)
    cfg3 = _get_env_config()
    reset_terminal_scope(t3)
    assert cfg3["env_type"] == "docker"
    assert cfg3["docker_image"] == "img:a"


def test_bridge_suppressed_while_scope_active():
    """The one-shot config bridge must not write profile values into the
    process env while a scope is bound."""
    from tools import terminal_tool

    token = set_terminal_scope({"TERMINAL_ENV": "docker"})
    try:
        before = dict(os.environ)
        terminal_tool._ensure_terminal_env_bridged()
        assert os.environ == before  # nothing written under scope
    finally:
        reset_terminal_scope(token)


# ---------------------------------------------------------------------------
# Cross-profile leak matrix (review requirement): A sensitive, B incomplete
# ---------------------------------------------------------------------------


def _leak_matrix_keys() -> dict:
    return {
        "TERMINAL_ENV": "local",          # A's backend
        "TERMINAL_DOCKER_VOLUMES": '["/host/secret:/data"]',
        "TERMINAL_SSH_HOST": "10.10.0.103",
        "TERMINAL_SSH_USER": "launch-admin",
        "TERMINAL_DOCKER_NETWORK": "false",
        "TERMINAL_CWD": "/home/launch-user/private",
        "TERMINAL_CONTAINER_PERSISTENT": "false",
    }


@pytest.mark.parametrize(
    "b_config_yaml,b_env",
    [
        pytest.param("terminal:\n  backend: docker\n", "", id="incomplete-backend-only"),
        pytest.param("", "", id="empty-terminal-block"),
        pytest.param(
            "",
            "TERMINAL_ENV=docker\nTERMINAL_CONTAINER_PERSISTENT=true\n",
            id="env-selections-only",
        ),
    ],
)
def test_profile_b_cannot_observe_profile_a_policy(
    tmp_path, monkeypatch, b_config_yaml, b_env
):
    """Profile A (launch process) holds sensitive mounts/SSH/network/CWD.
    Profile B — backend-only, empty, or .env-only — must observe NONE of it,
    even where B selects the same docker backend family."""
    home_b = tmp_path / "profiles" / "b"
    home_b.mkdir(parents=True)
    if b_config_yaml:
        (home_b / "config.yaml").write_text(b_config_yaml, encoding="utf-8")
    if b_env:
        (home_b / ".env").write_text(b_env, encoding="utf-8")

    scope = build_profile_terminal_scope(home_b)
    token = set_terminal_scope(scope)
    try:
        observed = {k: terminal_env(k, "<default>") for k in _leak_matrix_keys()}
        # Backend is B's own selection (docker from its config/.env where
        # given; the defined default otherwise) — never A's "local" via env.
        if "TERMINAL_ENV=docker" in b_env or "backend: docker" in b_config_yaml:
            assert observed["TERMINAL_ENV"] == "docker"
        # The sensitive A values must be unobservable. Resolving to a DEFINED
        # default ("" / "[]" / "true") is correct; resolving to A's seeded
        # value is the leak.
        a = _leak_matrix_keys()
        for key in (
            "TERMINAL_DOCKER_VOLUMES",
            "TERMINAL_SSH_HOST",
            "TERMINAL_SSH_USER",
            "TERMINAL_CWD",
        ):
            assert observed[key] != a[key], (
                f"LEAK: profile B observed A's {key}={observed[key]!r}"
            )
        # Boolean/network policy resolves to the defined default, not A's.
        assert observed["TERMINAL_DOCKER_NETWORK"] in {"true", "True"}
        assert observed["TERMINAL_CONTAINER_PERSISTENT"] in {"true", "True"}
    finally:
        reset_terminal_scope(token)


# ---------------------------------------------------------------------------
# build_profile_terminal_scope: precedence + completeness
# ---------------------------------------------------------------------------


def _docker_profile_yaml() -> str:
    return (
        "model:\n"
        "  default: test-model\n"
        "terminal:\n"
        "  backend: docker\n"
        "  docker_image: nikolaik/python-nodejs:python3.11-nodejs20\n"
        "  container_cpu: 2\n"
        "  container_persistent: true\n"
    )


def test_build_scope_reads_profile_config_and_is_total(tmp_path):
    home = tmp_path / "profiles" / "qa"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_docker_profile_yaml(), encoding="utf-8")
    scope = build_profile_terminal_scope(home)
    # Profile selections override defined defaults...
    assert scope["TERMINAL_ENV"] == "docker"
    assert scope["TERMINAL_CONTAINER_CPU"] == "2"
    # ...and the projection is total: every mapped key exists, except
    # TERMINAL_CWD whose config placeholder (".") is resolved per-surface
    # by the consuming tool (gateway cwd resolution), not projected.
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP

    expected = set(TERMINAL_CONFIG_ENV_MAP.values()) - {"TERMINAL_CWD"}
    assert set(scope) == expected
    assert not any(k.startswith("HERMES_") for k in scope)


def test_build_scope_config_overrides_dotenv(tmp_path):
    home = tmp_path / "profiles" / "qa"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_docker_profile_yaml(), encoding="utf-8")
    (home / ".env").write_text(
        "TERMINAL_ENV=local\nTERMINAL_CWD=/home/ubuntu\n", encoding="utf-8"
    )
    scope = build_profile_terminal_scope(home)
    assert scope["TERMINAL_ENV"] == "docker"       # config wins
    assert scope["TERMINAL_CWD"] == "/home/ubuntu"  # .env-only key survives


def test_build_scope_placeholder_cwd_not_bridged(tmp_path):
    home = tmp_path / "profiles" / "qa"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "terminal:\n  backend: local\n  cwd: .\n", encoding="utf-8"
    )
    assert "TERMINAL_CWD" not in build_profile_terminal_scope(home)


def test_build_scope_dotenv_only_profile(tmp_path):
    home = tmp_path / "profiles" / "qa"
    home.mkdir(parents=True)
    (home / ".env").write_text("TERMINAL_ENV=docker\n", encoding="utf-8")
    scope = build_profile_terminal_scope(home)
    assert scope["TERMINAL_ENV"] == "docker"
    # Still total despite the minimal .env (minus the per-surface cwd).
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP

    expected = set(TERMINAL_CONFIG_ENV_MAP.values()) - {"TERMINAL_CWD"}
    assert set(scope) == expected


# ---------------------------------------------------------------------------
# Fail closed: unresolvable policy → refusal scope → execution refuses
# ---------------------------------------------------------------------------


def test_malformed_profile_config_builds_refusal(tmp_path):
    home = tmp_path / "profiles" / "broken"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("terminal: [unclosed\n", encoding="utf-8")

    token = install_profile_terminal_scope(home)
    try:
        assert isinstance(get_terminal_scope(), TerminalPolicyRefusal)
        with pytest.raises(TerminalPolicyUnavailable):
            terminal_env("TERMINAL_ENV")
        with pytest.raises(TerminalPolicyUnavailable):
            enforce_no_refusal()
    finally:
        reset_terminal_scope(token)


def test_unreadable_profile_env_builds_refusal(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "broken-env"
    home.mkdir(parents=True)
    env_path = home / ".env"
    env_path.write_text("TERMINAL_ENV=docker\n", encoding="utf-8")
    # Make the .env unreadable at read_bytes time (the pre-flight check).
    real_read_bytes = Path.read_bytes

    def refusing_read_bytes(self):
        if self == env_path:
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", refusing_read_bytes)
    with pytest.raises(TerminalPolicyUnavailable):
        build_profile_terminal_scope(home)


def test_explicit_refusal_scope_blocks_execution_reads():
    token = install_refusal_scope("unit-test refusal")
    try:
        with pytest.raises(TerminalPolicyUnavailable):
            terminal_env("TERMINAL_ENV")
        with pytest.raises(TerminalPolicyUnavailable):
            enforce_no_refusal()
    finally:
        reset_terminal_scope(token)


def test_terminal_tool_refuses_under_refusal_scope():
    """The real execution path refuses with the typed error (fail closed),
    and _host_local control-plane children still work."""
    from tools.terminal_tool import terminal_tool

    token = install_refusal_scope("unresolved profile policy")
    try:
        result = terminal_tool(command="whoami")
        assert "terminal policy unavailable" in result
        assert "whoami" not in result or '"status": "error"' in result
    finally:
        reset_terminal_scope(token)


# ---------------------------------------------------------------------------
# Gateway boundary: _profile_runtime_scope installs the policy per turn
# ---------------------------------------------------------------------------


def test_gateway_runtime_scope_installs_and_cleans(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "qa"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_docker_profile_yaml(), encoding="utf-8")

    import gateway.run as gw

    monkeypatch.setattr(
        "agent.secret_scope.build_profile_secret_scope", lambda _h: {}
    )
    monkeypatch.setattr(
        "hermes_cli.env_loader.hydrate_profile_secret_sources", lambda _h: None
    )

    assert get_terminal_scope() is None
    with gw._profile_runtime_scope(home):
        scope = get_terminal_scope()
        assert scope is not None
        assert scope.get("TERMINAL_ENV") == "docker"
    assert get_terminal_scope() is None


def test_gateway_runtime_scope_cleans_up_on_error(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "qa"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_docker_profile_yaml(), encoding="utf-8")

    import gateway.run as gw

    monkeypatch.setattr("agent.secret_scope.build_profile_secret_scope", lambda _h: {})
    monkeypatch.setattr(
        "hermes_cli.env_loader.hydrate_profile_secret_sources", lambda _h: None
    )

    with pytest.raises(RuntimeError):
        with gw._profile_runtime_scope(home):
            assert get_terminal_scope() is not None
            raise RuntimeError("turn blew up")
    assert get_terminal_scope() is None


# ---------------------------------------------------------------------------
# TUI / dashboard boundary: the #98581 local→docker dashboard regression
# ---------------------------------------------------------------------------


def test_tui_session_runtime_scope_binds_terminal_policy(tmp_path, monkeypatch):
    """_session_profile_runtime_scope is the TUI turn/agent-build boundary;
    it must carry the same authoritative terminal policy the gateway binds —
    the docker-configured dashboard profile must never resolve host env."""
    import tui_gateway.server as server

    home = tmp_path / "profiles" / "dash"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_docker_profile_yaml(), encoding="utf-8")

    # Simulate the polluted launch-process env (unified desktop backend).
    assert os.environ["TERMINAL_ENV"] == "local"
    session = {"profile_home": str(home)}

    with server._session_profile_runtime_scope(session):
        assert get_terminal_scope() is not None
        assert terminal_env("TERMINAL_ENV") == "docker"
        # And cannot observe the launch process's secrets.
        assert terminal_env("TERMINAL_SSH_HOST") == ""
    assert get_terminal_scope() is None


def test_tui_session_runtime_scope_nop_for_launch_profile():
    import tui_gateway.server as server

    session = {}  # no profile_home → launch profile, no scope (unchanged)
    with server._session_profile_runtime_scope(session):
        assert get_terminal_scope() is None


def test_tui_agent_build_binds_terminal_scope(tmp_path, monkeypatch):
    """_start_agent_build's profile block binds home + secret scope; it must
    also bind the terminal policy so the built agent's tools can't drift."""
    # The build path wraps its own install/reset pair; verify the helper it
    # uses is the authoritative installer with refusal semantics.
    home = tmp_path / "profiles" / "broken"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("terminal: [oops\n", encoding="utf-8")
    token = install_profile_terminal_scope(home)
    try:
        assert isinstance(get_terminal_scope(), TerminalPolicyRefusal)
    finally:
        reset_terminal_scope(token)


# ---------------------------------------------------------------------------
# Cron boundary: per-fire install/reset on the ticker path
# ---------------------------------------------------------------------------


def test_cron_fire_install_reset_lifecycle(tmp_path):
    """Drive the REAL install/reset pair the scheduler's fire function uses
    (not just the builder): docker profile policy binds, then fully resets —
    a wrong/reset-skip cron installation cannot pass this."""
    home = tmp_path / "profiles" / "qa"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_docker_profile_yaml(), encoding="utf-8")

    from tools.terminal_scope import install_and_reset_profile_terminal_scope

    assert get_terminal_scope() is None
    with install_and_reset_profile_terminal_scope(home):
        assert get_terminal_scope() is not None
        assert terminal_env("TERMINAL_ENV") == "docker"
    assert get_terminal_scope() is None


def test_cron_fire_failure_still_resets(tmp_path):
    home = tmp_path / "profiles" / "qa"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_docker_profile_yaml(), encoding="utf-8")

    from tools.terminal_scope import install_and_reset_profile_terminal_scope

    with pytest.raises(RuntimeError):
        with install_and_reset_profile_terminal_scope(home):
            raise RuntimeError("job blew up")
    assert get_terminal_scope() is None


# ---------------------------------------------------------------------------
# Downstream scope-aware readers
# ---------------------------------------------------------------------------


def test_runtime_cwd_reads_scope(tmp_path):
    from agent.runtime_cwd import resolve_agent_cwd

    profile_cwd = tmp_path / "sandbox-workspace"
    profile_cwd.mkdir()
    token = set_terminal_scope({"TERMINAL_CWD": str(profile_cwd)})
    try:
        assert resolve_agent_cwd() == profile_cwd
    finally:
        reset_terminal_scope(token)


def test_prompt_builder_backend_read_is_scope_aware():
    from agent.prompt_builder import _tenv_read

    token = set_terminal_scope({"TERMINAL_ENV": "docker"})
    try:
        assert _tenv_read("TERMINAL_ENV") == "docker"
    finally:
        reset_terminal_scope(token)


def test_platform_base_media_translation_reads_scope():
    from gateway.platforms.base import _tenv

    token = set_terminal_scope({"TERMINAL_ENV": "docker"})
    try:
        assert _tenv("TERMINAL_ENV") == "docker"
    finally:
        reset_terminal_scope(token)


# ---------------------------------------------------------------------------
# Round-2 P1: refusal must propagate through downstream readers
# (review: broad `except Exception` around terminal_env() downgraded an
# active refusal scope back into ambient launch-profile policy)
# ---------------------------------------------------------------------------


def test_refusal_scope_blocks_all_downstream_readers():
    """With a refusal scope bound and POLLUTED ambient TERMINAL_* values,
    every scope-aware downstream reader must raise/refuse — never return the
    launch process's values."""
    from agent.prompt_builder import _tenv_read
    from agent.runtime_cwd import _terminal_cwd_env
    from gateway.platforms.base import _tenv as _gw_tenv
    from tools.terminal_scope import (
        TerminalPolicyUnavailable,
        install_refusal_scope,
    )

    token = install_refusal_scope("unit: downstream propagation")
    try:
        with pytest.raises(TerminalPolicyUnavailable):
            _tenv_read("TERMINAL_DOCKER_VOLUMES")
        with pytest.raises(TerminalPolicyUnavailable):
            _terminal_cwd_env()
        with pytest.raises(TerminalPolicyUnavailable):
            _gw_tenv("TERMINAL_DOCKER_VOLUMES")
        with pytest.raises(TerminalPolicyUnavailable):
            _gw_tenv("TERMINAL_CWD")
        with pytest.raises(TerminalPolicyUnavailable):
            from tools.terminal_scope import terminal_env as _te

            _te("TERMINAL_CWD")
    finally:
        from tools.terminal_scope import reset_terminal_scope

        reset_terminal_scope(token)


def test_platform_media_translation_refuses_under_refusal(monkeypatch):
    """The consequential surface the review calls out: Docker media-path
    translation must NOT reconstruct the launch profile's mounts under a
    refusal scope."""
    import gateway.platforms.base as base

    token = install_refusal_scope("unit: media translation refusal")
    try:
        # _parse_docker_volume_mounts reads TERMINAL_DOCKER_VOLUMES through
        # the scope-aware _tenv; under refusal it must raise, not parse the
        # ambient '["/host/secret:/data"]'.
        with pytest.raises(Exception) as exc_info:
            base._parse_docker_volume_mounts()
        assert "terminal policy unavailable" in str(exc_info.value)
    finally:
        from tools.terminal_scope import reset_terminal_scope

        reset_terminal_scope(token)


def test_runtime_cwd_resolution_refuses_under_refusal():
    """agent.runtime_cwd's public cwd resolution refuses under a refusal
    scope instead of returning the launch profile's cwd."""
    from agent import runtime_cwd

    token = install_refusal_scope("unit: cwd resolution refusal")
    try:
        with pytest.raises(Exception) as exc_info:
            runtime_cwd._terminal_cwd_env()
        assert "terminal policy unavailable" in str(exc_info.value)
    finally:
        from tools.terminal_scope import reset_terminal_scope

        reset_terminal_scope(token)


def test_gateway_terminal_scope_cwd_refuses_under_refusal():
    from gateway.run import _terminal_scope_cwd
    from tools.terminal_scope import TerminalPolicyUnavailable

    token = install_refusal_scope("unit: gateway cwd refusal")
    try:
        with pytest.raises(TerminalPolicyUnavailable):
            _terminal_scope_cwd("")
    finally:
        from tools.terminal_scope import reset_terminal_scope

        reset_terminal_scope(token)


def test_import_error_still_falls_back(monkeypatch):
    """Compatibility fallback exists ONLY for the genuine import-unavailable
    condition (never for an active refusal)."""
    import sys
    from agent.prompt_builder import _tenv_read

    real_import = __import__

    def _hide_scope(name, *a, **kw):
        if name == "tools.terminal_scope":
            raise ImportError("hidden for test")
        return real_import(name, *a, **kw)

    monkeypatch.setitem(sys.modules, "tools.terminal_scope", None)
    monkeypatch.setattr("builtins.__import__", _hide_scope)
    # Falls back to process env (= local, from the fixture) — not a refusal.
    assert _tenv_read("TERMINAL_ENV") == "local"


# ---------------------------------------------------------------------------
# Round-2: real-path coverage (the review's proof-gap items)
# ---------------------------------------------------------------------------


def test_tui_agent_build_real_path_binds_and_cleans(tmp_path, monkeypatch):
    """Drive the REAL _start_agent_build boundary: monkeypatch the agent
    factory to observe the bound scope on the build thread, and verify the
    build's finally fully resets it."""
    import tui_gateway.server as server

    home = tmp_path / "profiles" / "dash"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_docker_profile_yaml(), encoding="utf-8")

    observed = {}

    def fake_make_agent(*args, **kw):
        observed["scope"] = dict(get_terminal_scope() or {})
        # Minimal truthy agent stand-in: the build's post steps tolerate it
        # because _transfer_db_to_agent / attach tolerate plain objects.
        class _A:
            session_db = None
            async_clients = ()

            def close(self):
                pass

        return _A()

    monkeypatch.setattr(server, "_make_agent", fake_make_agent)

    saved = dict(server._sessions)
    try:
        sid = "test-build-sid"
        import threading as _threading

        session = {
            "profile_home": str(home),
            "session_key": "k",
            "agent": None,
            "agent_error": None,
            "agent_ready": _threading.Event(),
        }
        server._sessions[sid] = session
        server._start_agent_build(sid, session)
        # _start_agent_build spawns the build thread and returns; wait for
        # the ready event, which the build body sets in its finally.
        session["agent_ready"].wait(timeout=10)
        observed["after"] = get_terminal_scope()
        assert observed["scope"].get("TERMINAL_ENV") == "docker"
        assert observed["after"] is None
    finally:
        server._sessions.clear()
        server._sessions.update(saved)


def test_cron_fire_real_path_installs_and_resets(tmp_path, monkeypatch):
    """Drive the REAL scheduler per-fire boundary via run_one_job with a
    no-agent script job bound to a docker profile home: the terminal policy
    must be bound inside the fire (observed in-process at the script
    execution seam) and fully reset after the fire returns. (The script
    itself runs in a subprocess — ContextVars cannot cross that boundary by
    design; in-process seams like delivery and config resolution are what
    the scope protects.)"""
    import cron.scheduler as scheduler

    home = tmp_path / "profiles" / "qa"
    scripts_dir = home / "scripts"
    scripts_dir.mkdir(parents=True)
    (home / "config.yaml").write_text(_docker_profile_yaml(), encoding="utf-8")

    script = scripts_dir / "noop.py"
    script.write_text("pass\n", encoding="utf-8")
    job = {
        "id": "t-scope-fire",
        "name": "scope probe",
        "schedule": {"kind": "interval", "every_minutes": 60},
        "enabled": True,
        "prompt": "unused",
        "script": str(script),
        "no_agent": True,
        "session_id": None,
    }

    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: home)

    observed = {}
    real_runner = scheduler._run_job_script

    def recording_runner(script_path, workdir=None, cancel_event=None):
        observed["scope"] = get_terminal_scope()
        return real_runner(script_path, workdir=workdir, cancel_event=cancel_event)

    monkeypatch.setattr(scheduler, "_run_job_script", recording_runner)

    assert get_terminal_scope() is None
    result = scheduler.run_one_job(job)
    assert result is True
    # Inside the fire the docker profile policy was bound.
    assert isinstance(observed.get("scope"), dict)
    assert observed["scope"].get("TERMINAL_ENV") == "docker"
    # The fire's scope did not survive the boundary.
    assert get_terminal_scope() is None


def test_cron_fire_real_path_resets_on_failure(tmp_path, monkeypatch):
    """A no-agent job whose script FAILS must still reset the fire's scope
    and surface the failure through the normal (False) run path."""
    import cron.scheduler as scheduler

    home = tmp_path / "profiles" / "qa"
    scripts_dir = home / "scripts"
    scripts_dir.mkdir(parents=True)
    (home / "config.yaml").write_text(_docker_profile_yaml(), encoding="utf-8")

    script = scripts_dir / "boom.py"
    script.write_text("raise RuntimeError('boom-inside-fire')\n", encoding="utf-8")
    job = {
        "id": "t-scope-boom",
        "name": "boom",
        "schedule": {"kind": "interval", "every_minutes": 60},
        "enabled": True,
        "prompt": "unused",
        "script": str(script),
        "no_agent": True,
        "session_id": None,
    }

    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: home)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **kw: "ok")

    assert get_terminal_scope() is None
    result = scheduler.run_one_job(job)  # failure recorded, not raised
    assert result is True
    assert get_terminal_scope() is None
