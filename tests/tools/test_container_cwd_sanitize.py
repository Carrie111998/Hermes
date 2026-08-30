"""Regression tests for host-path cwd sanitization on container backends.

Two code paths in ``tools/terminal_tool.py`` must reject a host (or relative)
working directory before it reaches ``docker run -w``:

  1. ``_get_env_config()`` sanitizes the ``TERMINAL_CWD``-derived ``config["cwd"]``.
  2. ``terminal_tool()`` resolves a *per-task cwd override* that WINS over
     ``config["cwd"]`` (registered by the gateway/TUI for workspace tracking,
     and by RL/benchmark envs). That override was applied RAW — never sanitized
     — so a host cwd (e.g. a Windows desktop session's ``C:\\Users\\<user>``)
     leaked straight to ``docker run -w C:\\Users\\<user>``, which fails to start
     the container (exit 125). The sanitizer at path #1 lists ``C:\\``/``C:/`` as
     host prefixes but only ever ran against ``config["cwd"]``, so the override
     bypassed the one guard that would have caught it.

  3. ``register_task_env_overrides()`` writes a freshly registered ``cwd``
     onto the ALREADY-LIVE environment. That write was raw too, and it is the
     one that breaks file tools rather than container startup:
     ``ShellFileOperations`` reads ``env.cwd`` for every operation and the
     command wrapper emits ``builtin cd -- <cwd> || exit 126``, so a host cwd
     made ``read_file``/``patch``/``search_files`` fail with "File not found"
     and "rg is not installed" for files that plainly exist — intermittently,
     because the next terminal command rewrote the cwd back to a usable one.

All three paths now share ``_is_unusable_container_cwd()``; these tests pin
their behaviour so none can regress.
"""

import tools.terminal_tool as tt


class TestIsUnusableContainerCwd:
    def test_windows_backslash_host_path_rejected(self):
        # The exact shape from the bug report: a Windows host cwd reaching a
        # Linux container's -w flag.
        assert tt._is_unusable_container_cwd(r"C:\Users\someuser") is True


    def test_posix_home_host_path_rejected(self):
        assert tt._is_unusable_container_cwd("/home/ben/projects") is True


    def test_container_backends_set(self):
        assert tt._CONTAINER_BACKENDS == frozenset(
            {"docker", "singularity", "modal", "daytona", "vercel_sandbox"}
        )


class TestOverrideCwdSanitizedAtCallSite:
    """E2E pin: a per-task cwd OVERRIDE that is a host path must NOT reach the
    container builder. This is the actual reported bug — the gateway/TUI
    registers the host launch dir as a cwd override, which previously won over
    the (sanitized) config["cwd"] and flowed raw into `docker run -w`.
    """

    def _run_and_capture_cwd(self, monkeypatch, override_cwd, config_cwd="/root"):
        """Drive terminal_tool() on the docker backend with a host-path cwd
        override registered, and return the cwd that reached _create_environment
        (i.e. the cwd that would be passed to `docker run -w`).
        """
        captured = {}

        config = {
            "env_type": "docker",
            "docker_image": "pytorch/pytorch:latest",
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

        class _DummyEnv:
            cwd = config_cwd

            def execute(self, *a, **k):
                return {"output": "", "exit_code": 0}

        def fake_create_environment(env_type, image, cwd, timeout, **kwargs):
            captured["cwd"] = cwd
            return _DummyEnv()

        monkeypatch.setattr(tt, "_get_env_config", lambda: config)
        monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
        monkeypatch.setattr(tt, "_check_all_guards", lambda *a, **k: {"approved": True})
        monkeypatch.setattr(tt, "_create_environment", fake_create_environment)
        # Force a fresh environment build so _create_environment is invoked.
        monkeypatch.setattr(tt, "_active_environments", {})
        monkeypatch.setattr(tt, "_last_activity", {})

        task_id = "sess-host-cwd"
        tt.register_task_env_overrides(task_id, {"cwd": override_cwd})
        try:
            tt.terminal_tool(command="pwd", task_id=task_id)
        finally:
            tt.clear_task_env_overrides(task_id)
            tt._active_environments.pop(task_id, None)
            tt._active_environments.pop("default", None)
        return captured.get("cwd")

    def test_windows_host_override_does_not_reach_container(self, monkeypatch):
        # The bug: C:\Users\<user> registered as override → docker run -w C:\Users\<user> → exit 125.
        cwd = self._run_and_capture_cwd(monkeypatch, r"C:\Users\someuser")
        assert cwd == "/root", (
            f"Host-path cwd override leaked to the container builder: {cwd!r}. "
            "It must be sanitized back to config['cwd']."
        )


    def test_valid_container_override_is_preserved(self, monkeypatch):
        # RL/benchmark envs set an in-container path; it must pass through.
        cwd = self._run_and_capture_cwd(monkeypatch, "/workspace/task42")
        assert cwd == "/workspace/task42"


class TestFileOpsCwdSanitizedAtCallSite:
    """E2E pin: file tools (_get_file_ops) must sanitize a host/relative cwd
    override before it reaches _create_environment on a container backend —
    the same guard the terminal tool got in #50636.  Without it, a Desktop/TUI
    host cwd (e.g. ``/Users/me/workspace``) leaks straight into
    ``docker run -w`` and ``search_files`` returns an empty workspace (#54447).
    """

    def _run_and_capture_cwd(self, monkeypatch, override_cwd, env_type="docker",
                             config_cwd="/workspace"):
        """Drive ``_get_file_ops()`` on a container backend with a host-path cwd
        override registered, and return the cwd that reached
        ``_create_environment`` (i.e. the cwd passed to ``docker run -w``).
        """
        import tools.terminal_tool as tt
        import tools.file_tools as ft

        captured = {}

        config = {
            "env_type": env_type,
            "docker_image": "pytorch/pytorch:latest",
            "singularity_image": "docker://pytorch/pytorch:latest",
            "modal_image": "pytorch/pytorch:latest",
            "daytona_image": "pytorch/pytorch:latest",
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
            "ssh_host": "",
            "ssh_user": "",
            "ssh_port": 22,
            "ssh_key": "",
            "ssh_persistent": False,
            "local_persistent": False,
        }

        class _DummyEnv:
            cwd = config_cwd

            def execute(self, *a, **k):
                return {"output": "", "exit_code": 0}

        def fake_create_environment(env_type, image, cwd, timeout, **kwargs):
            captured["cwd"] = cwd
            return _DummyEnv()

        monkeypatch.setattr(tt, "_get_env_config", lambda: config)
        monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
        monkeypatch.setattr(tt, "_create_environment", fake_create_environment)
        # Force a fresh environment build.
        monkeypatch.setattr(tt, "_active_environments", {})
        monkeypatch.setattr(tt, "_last_activity", {})
        monkeypatch.setattr(ft, "_file_ops_cache", {})
        monkeypatch.setattr(tt, "_session_cwd", {})

        task_id = "sess-fileops-host-cwd"
        tt.register_task_env_overrides(task_id, {"cwd": override_cwd})
        try:
            ft._get_file_ops(task_id)
        finally:
            tt.clear_task_env_overrides(task_id)
        return captured.get("cwd")

    def test_macos_host_override_does_not_reach_container(self, monkeypatch):
        # Desktop/TUI registers /Users/<me>/workspace as the session cwd.
        cwd = self._run_and_capture_cwd(monkeypatch, "/Users/me/workspace")
        assert cwd == "/workspace", (
            f"Host-path cwd override leaked to the container builder: {cwd!r}. "
            "It must be sanitized back to config['cwd']."
        )


    def test_valid_container_override_is_preserved(self, monkeypatch):
        # RL/benchmark envs set an in-container path; it must pass through.
        cwd = self._run_and_capture_cwd(monkeypatch, "/workspace/task42")
        assert cwd == "/workspace/task42"

    def test_host_override_sanitized_on_singularity(self, monkeypatch):
        cwd = self._run_and_capture_cwd(
            monkeypatch, "/Users/me/workspace", env_type="singularity")
        assert cwd == "/workspace"

    def test_host_override_sanitized_on_modal(self, monkeypatch):
        cwd = self._run_and_capture_cwd(
            monkeypatch, "/Users/me/workspace", env_type="modal")
        assert cwd == "/workspace"


class TestLiveEnvCwdSanitizedOnRegistration:
    """E2E pin for path #3: registering a host cwd must not poison the cwd of
    an environment that is already running on a container backend.
    """

    class _FakeDockerEnvironment:
        """Stands in for a live container env (recognized by class name)."""

        def __init__(self, cwd="/root"):
            self.cwd = cwd

    class _FakeLocalEnvironment:
        def __init__(self, cwd="/root"):
            self.cwd = cwd

    def _register(self, monkeypatch, env, cwd, task_id="sess-live-cwd"):
        monkeypatch.setattr(tt, "_active_environments", {task_id: env})
        monkeypatch.setattr(tt, "_session_cwd", {})
        tt.register_task_env_overrides(task_id, {"cwd": cwd})
        try:
            return tt.get_session_cwd(task_id)
        finally:
            tt.clear_task_env_overrides(task_id)

    def test_host_cwd_does_not_reach_live_container_env(self, monkeypatch):
        env = self._FakeDockerEnvironment(cwd="/root")
        recorded = self._register(monkeypatch, env, "/Users/me")
        assert env.cwd == "/root", (
            f"Host cwd leaked onto the live container env: {env.cwd!r}. Every "
            "file operation would then run `cd /Users/me` inside the sandbox "
            "and fail with a phantom 'File not found'."
        )
        # The session RECORD still keeps the host path — its readers guard it,
        # and host-side surfaces need to know where the user actually is.
        assert recorded == "/Users/me"

    def test_windows_host_cwd_does_not_reach_live_container_env(self, monkeypatch):
        env = self._FakeDockerEnvironment(cwd="/workspace")
        self._register(monkeypatch, env, r"C:\Users\someuser")
        assert env.cwd == "/workspace"

    def test_container_cwd_still_applies_immediately(self, monkeypatch):
        # An in-sandbox path is the case the assignment exists for (an ACP
        # client switching project root mid-session); it must still take
        # effect on the live env.
        env = self._FakeDockerEnvironment(cwd="/root")
        self._register(monkeypatch, env, "/workspace/task42")
        assert env.cwd == "/workspace/task42"

    def test_local_backend_keeps_host_cwd(self, monkeypatch):
        # On a local backend a host path IS the working directory.
        env = self._FakeLocalEnvironment(cwd="/tmp")
        self._register(monkeypatch, env, "/Users/me")
        assert env.cwd == "/Users/me"


class TestEnvInstanceBackendName:
    def test_docker_class(self):
        class DockerEnvironment:
            pass

        assert tt._env_instance_backend_name(DockerEnvironment()) == "docker"

    def test_stamp_wins_over_class_name(self):
        class SomePluginEnv:
            _hermes_backend_name = "e2b"

        assert tt._env_instance_backend_name(SomePluginEnv()) == "e2b"

    def test_unrecognized_returns_empty(self):
        class Mystery:
            pass

        assert tt._env_instance_backend_name(Mystery()) == ""
