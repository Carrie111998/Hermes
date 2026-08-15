"""Profile-isolation tests for shared background MCP discovery startup."""

from types import SimpleNamespace

from agent.secret_scope import reset_secret_scope, set_secret_scope
import hermes_cli.mcp_startup as startup
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


class _Scope:
    def __init__(self, home):
        self.home = str(home)
        self.token = None

    def __enter__(self):
        self.token = set_hermes_home_override(self.home)

    def __exit__(self, *_exc):
        reset_hermes_home_override(self.token)


def test_background_discovery_started_state_is_per_profile(tmp_path, monkeypatch):
    """A connected profile A must not suppress profile B's discovery."""
    home_a = tmp_path / "profiles" / "a"
    home_b = tmp_path / "profiles" / "b"
    home_a.mkdir(parents=True)
    home_b.mkdir(parents=True)
    seen = []

    monkeypatch.setattr(startup, "_mcp_discovery_started", False)
    monkeypatch.setattr(startup, "_mcp_discovery_thread", None)
    monkeypatch.setattr(startup, "_has_configured_mcp_servers", lambda: True)
    monkeypatch.setattr(
        startup,
        "_discover_mcp_tools_without_interactive_oauth",
        lambda: seen.append(startup._current_discovery_scope()),
    )
    monkeypatch.setattr(
        "tools.mcp_tool.get_mcp_status",
        lambda: [{"connected": True}],
    )

    logger = SimpleNamespace(warning=lambda *_a, **_k: None, debug=lambda *_a, **_k: None)
    for home in (home_a, home_b):
        with _Scope(home):
            startup.start_background_mcp_discovery(logger=logger, thread_name="test-mcp")
            startup.join_mcp_discovery(timeout=2)

    assert len(seen) == 2
    assert seen[0] != seen[1]


def test_discovery_lock_path_is_profile_local(tmp_path, monkeypatch):
    """Profiles never share the cached advisory discovery-lock pathname."""
    import tools.mcp_tool as mcp

    home_a = tmp_path / "profiles" / "a"
    home_b = tmp_path / "profiles" / "b"
    home_a.mkdir(parents=True)
    home_b.mkdir(parents=True)
    paths = []

    mcp._reset_mcp_runtimes_for_tests()
    try:
        for home in (home_a, home_b):
            with _Scope(home):
                cookie = mcp._try_acquire_mcp_discovery_lock()
                assert cookie not in (None, mcp._LOCK_UNAVAILABLE)
                paths.append(mcp._current_runtime().discovery_lock_path)
                cookie.release()
    finally:
        mcp._reset_mcp_runtimes_for_tests()

    assert paths == [
        str(home_a / ".mcp-discovery.lock"),
        str(home_b / ".mcp-discovery.lock"),
    ]


def test_background_discovery_copies_profile_secret_scope(tmp_path, monkeypatch):
    """The discovery thread resolves `${TOKEN}` from its owning profile."""
    from agent.secret_scope import get_secret

    home = tmp_path / "profiles" / "a"
    home.mkdir(parents=True)
    seen = []
    monkeypatch.setattr(startup, "_has_configured_mcp_servers", lambda: True)
    monkeypatch.setattr(
        startup,
        "_discover_mcp_tools_without_interactive_oauth",
        lambda: seen.append(get_secret("LINEAR_API_KEY")),
    )
    monkeypatch.setattr("tools.mcp_tool.get_mcp_status", lambda: [])
    logger = SimpleNamespace(
        warning=lambda *_a, **_k: None,
        debug=lambda *_a, **_k: None,
    )

    with _Scope(home):
        token = set_secret_scope({"LINEAR_API_KEY": "profile-a-token"})
        try:
            startup.start_background_mcp_discovery(
                logger=logger,
                thread_name="test-secret",
            )
            startup.join_mcp_discovery(timeout=2)
        finally:
            reset_secret_scope(token)

    assert seen == ["profile-a-token"]