"""Provider parity tests: plugin terminal backends get the same policy
treatment as built-in backends at every classification site.

Pluggable terminal backends participate in core policy decisions through
declarative provider attributes (see the classification contract in
``agent/terminal_env_provider.py``), but four sites still consulted
hardcoded built-in names only, so a plugin backend silently lost behavior
every in-tree backend has:

  * ``_get_env_config()``'s default-cwd ladder ignored the provider's
    ``default_cwd`` and forced ``/root``.
  * The host-path cwd sanitizers discarded the backend's own guest home
    (e.g. ``/home/agent``) because it matches the host-path heuristics —
    including the agent's recorded ``cd`` state between commands.
  * RL/benchmark per-task image overrides (``f"{backend}_image"``) neither
    triggered rollout isolation nor reached ``create_environment``.
  * ``_should_skip_container_guards`` never consulted the provider's
    ``skip_container_guards`` flag, and the prompt builder's live backend
    probe could spin up an expensive/billable sandbox at prompt-build time
    (``probe_at_prompt_build``).

All fake providers here subclass the real ABC and register through the real
registry, so the fail-soft ``provider_flag`` chain is exercised end to end.
"""

import pytest

import tools.terminal_tool as tt
from agent import terminal_env_registry as reg
from agent.terminal_env_provider import TerminalEnvironmentProvider


class _FakeEnv:
    def __init__(self, cwd="/home/guest"):
        self.cwd = cwd

    def execute(self, command, timeout=None, **kwargs):
        return {"output": "", "exit_code": 0}

    def cleanup(self):
        pass


class _GuestHomeProvider(TerminalEnvironmentProvider):
    """Container-style plugin backend whose guest home hides under /home/."""

    name = "guestbox"
    display_name = "GuestBox"
    is_remote = True
    is_container = True
    guest_home_root = "/home/guest"
    default_cwd = "/home/guest"

    def is_available(self):
        return True

    def create_environment(self, *, cwd, timeout, task_id="default",
                           image=None, container_config=None, **kwargs):
        return _FakeEnv(cwd)


@pytest.fixture(autouse=True)
def _clean_registry():
    reg._reset_for_tests()
    yield
    reg._reset_for_tests()


def _plugin_env_config(monkeypatch, provider):
    """Run the real _get_env_config() with *provider* as the active backend."""
    reg.register_provider(provider)
    monkeypatch.setenv("TERMINAL_ENV", provider.name)
    # The config.yaml → env bridge is a no-op after first attempt; skip it so
    # the test reads only the env vars set here.
    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
    return tt._get_env_config()


class TestPluginDefaultCwd:
    def test_provider_default_cwd_honored(self, monkeypatch):
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        config = _plugin_env_config(monkeypatch, _GuestHomeProvider())
        assert config["cwd"] == "/home/guest"

    def test_missing_default_cwd_falls_back_to_root(self, monkeypatch):
        class PlainBox(_GuestHomeProvider):
            name = "plainbox"
            guest_home_root = None
            default_cwd = None

        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        config = _plugin_env_config(monkeypatch, PlainBox())
        assert config["cwd"] == "/root"

    def test_raising_default_cwd_fails_soft_to_root(self, monkeypatch):
        class RaiseBox(_GuestHomeProvider):
            name = "raisebox"
            guest_home_root = None

            @property
            def default_cwd(self):
                raise RuntimeError("boom")

        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        config = _plugin_env_config(monkeypatch, RaiseBox())
        assert config["cwd"] == "/root"


class TestBackendGuestSubpath:
    """The backend's guest home (and its subtree) is a real sandbox path, so
    it must be exempt from the host-path guard even though it shares the
    /home/ prefix that other backends reject."""

    @pytest.fixture(autouse=True)
    def _register(self, _clean_registry):
        reg.register_provider(_GuestHomeProvider())

    def test_guest_home_root_is_subpath(self):
        assert tt._is_backend_guest_subpath("guestbox", "/home/guest") is True

    def test_guest_home_child_is_subpath(self):
        assert tt._is_backend_guest_subpath("guestbox", "/home/guest/project") is True

    def test_unrelated_home_is_not_subpath(self):
        # A different user's home is still a host path even on guestbox.
        assert tt._is_backend_guest_subpath("guestbox", "/home/someoneelse") is False

    def test_sibling_prefix_is_not_subpath(self):
        # /home/guestother shares the string prefix but not the subtree.
        assert tt._is_backend_guest_subpath("guestbox", "/home/guestother") is False

    def test_docker_has_no_guest_home_exemption(self):
        # Built-in backends declare no guest home; /home/... stays a host path.
        assert tt._is_backend_guest_subpath("docker", "/home/guest/project") is False

    def test_guest_subpath_survives_full_guard(self):
        # The combined check the call sites use: unusable-by-prefix but exempt.
        assert tt._is_unusable_container_cwd("/home/guest/project") is True
        assert tt._is_backend_guest_subpath("guestbox", "/home/guest/project") is True

    def test_raising_guest_home_property_fails_soft(self):
        class RaiseBox(_GuestHomeProvider):
            name = "raisebox"

            @property
            def guest_home_root(self):
                raise RuntimeError("boom")

        reg.register_provider(RaiseBox())
        assert tt._is_backend_guest_subpath("raisebox", "/home/guest") is False

    def test_root_slash_never_exempts(self):
        # A bare "/" root would exempt EVERY absolute path from the
        # host-path guard — reject it instead of honoring it.
        class SlashBox(_GuestHomeProvider):
            name = "slashbox"
            guest_home_root = "/"

        reg.register_provider(SlashBox())
        assert tt._is_backend_guest_subpath("slashbox", "/Users/me/secrets") is False
        assert tt._is_backend_guest_subpath("slashbox", "/home/guest") is False
        assert tt._is_backend_guest_subpath("slashbox", "/") is False

    def test_empty_root_never_exempts(self):
        class EmptyBox(_GuestHomeProvider):
            name = "emptybox"
            guest_home_root = ""

        reg.register_provider(EmptyBox())
        assert tt._is_backend_guest_subpath("emptybox", "/home/guest") is False

    def test_relative_root_never_exempts(self):
        class RelBox(_GuestHomeProvider):
            name = "relbox"
            guest_home_root = "home/guest"

        reg.register_provider(RelBox())
        assert tt._is_backend_guest_subpath("relbox", "home/guest/project") is False
        assert tt._is_backend_guest_subpath("relbox", "/home/guest") is False

    def test_terminal_cwd_in_guest_home_survives_sanitize(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "guestbox")
        monkeypatch.setenv("TERMINAL_CWD", "/home/guest/project")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
        config = tt._get_env_config()
        assert config["cwd"] == "/home/guest/project"

    def test_terminal_cwd_outside_guest_home_still_sanitized(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "guestbox")
        monkeypatch.setenv("TERMINAL_CWD", "/home/someoneelse/project")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
        config = tt._get_env_config()
        assert config["cwd"] == "/home/guest"

    def test_recorded_guest_cd_state_survives(self):
        # The per-command sibling site: the session's own `cd` record inside
        # the guest home must not be discarded as a host path.
        tt.record_session_cwd("sess-guest-cd", "/home/guest/project")
        try:
            resolved = tt._resolve_command_cwd(
                workdir=None, default_cwd="/home/guest",
                session_key="sess-guest-cd", env_type="guestbox",
            )
        finally:
            tt.clear_session_cwd("sess-guest-cd")
        assert resolved == "/home/guest/project"

    def test_recorded_host_cwd_still_discarded(self):
        tt.record_session_cwd("sess-guest-cd", "/Users/me/workspace")
        try:
            resolved = tt._resolve_command_cwd(
                workdir=None, default_cwd="/home/guest",
                session_key="sess-guest-cd", env_type="guestbox",
            )
        finally:
            tt.clear_session_cwd("sess-guest-cd")
        assert resolved == "/home/guest"


class TestResolveTaskImage:
    """_resolve_task_image is the single owner of the backend→image ladder
    (terminal_tool, ensure_task_env, execute_code, file tools); pin the
    helper itself so the ladder can't drift at any call site."""

    def test_builtin_override_wins(self):
        image = tt._resolve_task_image(
            "docker", {"docker_image": "task:img"}, {"docker_image": "cfg:img"})
        assert image == "task:img"

    def test_builtin_falls_back_to_config(self):
        image = tt._resolve_task_image("docker", {}, {"docker_image": "cfg:img"})
        assert image == "cfg:img"

    def test_plugin_override_resolved_when_registered(self):
        reg.register_provider(_GuestHomeProvider())
        image = tt._resolve_task_image("guestbox", {"guestbox_image": "guest:img"}, {})
        assert image == "guest:img"

    def test_plugin_without_override_is_empty(self):
        reg.register_provider(_GuestHomeProvider())
        assert tt._resolve_task_image("guestbox", {}, {}) == ""

    def test_unknown_backend_is_empty(self):
        # Not registered: an arbitrary *_image override key resolves nothing.
        image = tt._resolve_task_image("nosuchbox", {"nosuchbox_image": "x:img"}, {})
        assert image == ""


def _plugin_backend_config(config_cwd="/home/guest"):
    """Minimal _get_env_config()-shaped dict for the guestbox backend."""
    return {
        "env_type": "guestbox",
        "cwd": config_cwd,
        "host_cwd": None,
        "timeout": 180,
        "lifetime_seconds": 300,
        "container_cpu": 1,
        "container_memory": 5120,
        "container_disk": 51200,
        "container_persistent": True,
        "docker_volumes": [],
        "docker_env": {},
        "docker_extra_args": [],
        "docker_mount_cwd_to_workspace": False,
        "docker_run_as_host_user": False,
        "docker_forward_env": [],
        "modal_mode": "auto",
    }


class TestPluginImageOverride:
    """Per-task image overrides for plugin backends (``f"{backend}_image"``)
    must trigger rollout isolation and reach create_environment — the same
    contract RL/benchmark envs rely on for docker/modal/... backends."""

    def test_plugin_image_override_triggers_isolation(self, monkeypatch):
        reg.register_provider(_GuestHomeProvider())
        monkeypatch.setenv("TERMINAL_ENV", "guestbox")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
        task_id = "rollout-42"
        tt.register_task_env_overrides(task_id, {"guestbox_image": "guest:img"})
        try:
            assert tt._has_isolation_overrides(task_id) is True
            assert tt._resolve_container_task_id(task_id) == task_id
        finally:
            tt.clear_task_env_overrides(task_id)

    def test_unregistered_backend_image_key_is_not_isolation(self, monkeypatch):
        # guestbox is NOT registered here: an arbitrary *_image key must not
        # trigger per-task sandbox isolation.
        monkeypatch.setenv("TERMINAL_ENV", "guestbox")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)
        task_id = "rollout-43"
        tt.register_task_env_overrides(task_id, {"guestbox_image": "guest:img"})
        try:
            assert tt._has_isolation_overrides(task_id) is False
        finally:
            tt.clear_task_env_overrides(task_id)

    def _drive_terminal_tool(self, monkeypatch, overrides, config_cwd="/home/guest"):
        """Run terminal_tool() on the guestbox backend with *overrides*
        registered, and return what reached _create_environment."""
        reg.register_provider(_GuestHomeProvider())
        monkeypatch.setenv("TERMINAL_ENV", "guestbox")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)

        captured = {}

        def fake_create_environment(env_type, image, cwd, timeout, **kwargs):
            captured["image"] = image
            captured["cwd"] = cwd
            captured["task_id"] = kwargs.get("task_id")
            return _FakeEnv(cwd)

        monkeypatch.setattr(tt, "_get_env_config", lambda: _plugin_backend_config(config_cwd))
        monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
        monkeypatch.setattr(tt, "_check_all_guards", lambda *a, **k: {"approved": True})
        monkeypatch.setattr(tt, "_create_environment", fake_create_environment)
        # Force a fresh environment build so _create_environment is invoked.
        monkeypatch.setattr(tt, "_active_environments", {})
        monkeypatch.setattr(tt, "_last_activity", {})
        monkeypatch.setattr(tt, "_session_cwd", {})

        task_id = "rollout-image"
        tt.register_task_env_overrides(task_id, overrides)
        try:
            tt.terminal_tool(command="pwd", task_id=task_id)
        finally:
            tt.clear_task_env_overrides(task_id)
            tt._active_environments.pop(task_id, None)
            tt._active_environments.pop("default", None)
        return captured

    def test_plugin_image_override_reaches_terminal_create(self, monkeypatch):
        captured = self._drive_terminal_tool(
            monkeypatch, {"guestbox_image": "guest:img"})
        assert captured.get("image") == "guest:img"
        # The override isolates the rollout: its own sandbox, not "default".
        assert captured.get("task_id") == "rollout-image"

    def test_guest_home_cwd_override_survives_terminal_guard(self, monkeypatch):
        captured = self._drive_terminal_tool(
            monkeypatch,
            {"guestbox_image": "guest:img", "cwd": "/home/guest/project"},
        )
        assert captured.get("cwd") == "/home/guest/project"

    def test_host_cwd_override_still_sanitized(self, monkeypatch):
        captured = self._drive_terminal_tool(
            monkeypatch,
            {"guestbox_image": "guest:img", "cwd": "/Users/me/workspace"},
        )
        assert captured.get("cwd") == "/home/guest"

    def test_plugin_image_override_reaches_ensure_task_env(self, monkeypatch):
        reg.register_provider(_GuestHomeProvider())
        monkeypatch.setenv("TERMINAL_ENV", "guestbox")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)

        captured = {}

        def fake_create_environment(env_type, image, cwd, timeout, **kwargs):
            captured["image"] = image
            return _FakeEnv(cwd)

        monkeypatch.setattr(tt, "_get_env_config", lambda: _plugin_backend_config())
        monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
        monkeypatch.setattr(tt, "_create_environment", fake_create_environment)
        monkeypatch.setattr(tt, "_active_environments", {})
        monkeypatch.setattr(tt, "_last_activity", {})

        task_id = "rollout-lazy"
        tt.register_task_env_overrides(task_id, {"guestbox_image": "guest:img"})
        try:
            env = tt.ensure_task_env(task_id)
        finally:
            tt.clear_task_env_overrides(task_id)
            tt._active_environments.pop(task_id, None)
        assert env is not None
        assert captured.get("image") == "guest:img"

    def test_plugin_image_override_reaches_code_execution_env(self, monkeypatch):
        import tools.code_execution_tool as cet

        reg.register_provider(_GuestHomeProvider())
        monkeypatch.setenv("TERMINAL_ENV", "guestbox")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)

        captured = {}

        def fake_create_environment(env_type, image, cwd, timeout, **kwargs):
            captured["image"] = image
            return _FakeEnv(cwd)

        monkeypatch.setattr(tt, "_get_env_config", lambda: _plugin_backend_config())
        monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
        monkeypatch.setattr(tt, "_create_environment", fake_create_environment)
        monkeypatch.setattr(tt, "_active_environments", {})
        monkeypatch.setattr(tt, "_last_activity", {})

        task_id = "rollout-exec"
        tt.register_task_env_overrides(task_id, {"guestbox_image": "guest:img"})
        try:
            env, env_type = cet._get_or_create_env(task_id)
        finally:
            tt.clear_task_env_overrides(task_id)
            tt._active_environments.pop(task_id, None)
        assert env_type == "guestbox"
        assert captured.get("image") == "guest:img"

    def test_plugin_image_override_reaches_file_ops_env(self, monkeypatch):
        import tools.file_tools as ft

        reg.register_provider(_GuestHomeProvider())
        monkeypatch.setenv("TERMINAL_ENV", "guestbox")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)

        captured = {}

        def fake_create_environment(env_type, image, cwd, timeout, **kwargs):
            captured["image"] = image
            return _FakeEnv(cwd)

        monkeypatch.setattr(tt, "_get_env_config", lambda: _plugin_backend_config())
        monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
        monkeypatch.setattr(tt, "_create_environment", fake_create_environment)
        monkeypatch.setattr(tt, "_active_environments", {})
        monkeypatch.setattr(tt, "_last_activity", {})
        monkeypatch.setattr(tt, "_session_cwd", {})
        monkeypatch.setattr(ft, "_file_ops_cache", {})

        task_id = "rollout-files"
        tt.register_task_env_overrides(task_id, {"guestbox_image": "guest:img"})
        try:
            ft._get_file_ops(task_id)
        finally:
            tt.clear_task_env_overrides(task_id)
            tt._active_environments.pop(task_id, None)
        assert captured.get("image") == "guest:img"


class TestPluginApprovalGuardSkip:
    """_should_skip_container_guards consults the provider's declarative
    skip_container_guards flag for plugin backends (fail-soft: approval
    stays ON for unknown names and raising properties)."""

    def test_provider_skip_flag_honored(self):
        from tools.approval import _should_skip_container_guards

        # ABC default: skip_container_guards mirrors is_container (True here).
        reg.register_provider(_GuestHomeProvider())
        assert _should_skip_container_guards("guestbox") is True

    def test_host_mounting_provider_keeps_guards(self):
        from tools.approval import _should_skip_container_guards

        class HostBox(_GuestHomeProvider):
            name = "hostbox"

            @property
            def skip_container_guards(self):
                return False

        reg.register_provider(HostBox())
        assert _should_skip_container_guards("hostbox") is False

    def test_unknown_backend_keeps_guards(self):
        from tools.approval import _should_skip_container_guards

        assert _should_skip_container_guards("nosuchbox") is False

    def test_raising_property_fails_soft_to_guards_on(self):
        from tools.approval import _should_skip_container_guards

        class RaiseBox(_GuestHomeProvider):
            name = "raisebox"

            @property
            def skip_container_guards(self):
                raise RuntimeError("boom")

        reg.register_provider(RaiseBox())
        assert _should_skip_container_guards("raisebox") is False

    def test_builtin_behavior_unchanged(self):
        from tools.approval import _should_skip_container_guards

        assert _should_skip_container_guards("modal") is True
        assert _should_skip_container_guards("docker", has_host_access=True) is False
        assert _should_skip_container_guards("local") is False


class TestPromptProbeOptOut:
    """probe_at_prompt_build=False must prevent the prompt builder from
    creating a (potentially billable) environment; the static
    env_description fallback describes the backend instead."""

    class _NoProbeProvider(_GuestHomeProvider):
        name = "billbox"
        probe_at_prompt_build = False

        @property
        def env_description(self):
            return "a BillBox cloud sandbox (Linux)"

    def test_probe_disabled_skips_environment_creation(self, monkeypatch):
        import agent.prompt_builder as _pb

        reg.register_provider(self._NoProbeProvider())

        def _explode(*a, **k):
            raise AssertionError("prompt build must not create an environment")

        monkeypatch.setattr(tt, "_create_environment", _explode)
        _pb._clear_backend_probe_cache()
        try:
            assert _pb._probe_remote_backend("billbox") is None
        finally:
            _pb._clear_backend_probe_cache()

    def test_probe_disabled_falls_back_to_env_description(self, monkeypatch):
        import agent.prompt_builder as _pb

        reg.register_provider(self._NoProbeProvider())
        monkeypatch.setenv("TERMINAL_ENV", "billbox")
        monkeypatch.setattr(_pb, "is_wsl", lambda: False)

        def _explode(*a, **k):
            raise AssertionError("prompt build must not create an environment")

        monkeypatch.setattr(tt, "_create_environment", _explode)
        _pb._clear_backend_probe_cache()
        try:
            result = _pb.build_environment_hints()
        finally:
            _pb._clear_backend_probe_cache()
        assert "Terminal backend: billbox" in result
        assert "a BillBox cloud sandbox (Linux)" in result

    def test_probe_enabled_by_default_for_plugins(self, monkeypatch):
        import agent.prompt_builder as _pb

        reg.register_provider(_GuestHomeProvider())
        monkeypatch.setenv("TERMINAL_ENV", "guestbox")
        monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", True)

        created = {}

        class _ProbeEnv:
            def execute(self, cmd, timeout=None):
                return {
                    "returncode": 0,
                    "output": (
                        "os=Linux\nkernel=6.8.0\nhome=/home/guest\n"
                        "cwd=/home/guest\nuser=guest\n"
                    ),
                }

        def fake_create_environment(*, env_type, **kwargs):
            created["env_type"] = env_type
            return _ProbeEnv()

        monkeypatch.setattr(tt, "_create_environment", fake_create_environment)
        _pb._clear_backend_probe_cache()
        try:
            line = _pb._probe_remote_backend("guestbox")
        finally:
            _pb._clear_backend_probe_cache()
        assert created.get("env_type") == "guestbox"
        assert line is not None
        assert "Linux 6.8.0" in line
