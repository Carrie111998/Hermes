"""Tests for copilot_remote.launcher — command building, output parsing, and launch.

Every test in this module runs with COPILOT_HOME redirected to a per-test
tmp_path (see the autouse `_isolate_copilot_home` fixture below), so no test
can read or write the real ~/.copilot/settings.json on the host running the
suite.
"""

import json
import os
import signal
import threading
from pathlib import Path

import pytest

from copilot_remote.launcher import (
    _attempt_initial_prompt_delivery,
    _copilot_settings_path,
    _ensure_folder_trusted,
    _ensure_initial_prompt_delivered,
    _parse_remote_task_id,
    _remote_task_has_user_message,
    _resolve_copilot_bin,
    build_copilot_command,
    launch_copilot,
    parse_copilot_output,
)
from copilot_remote.models import RepoEntry


@pytest.fixture(autouse=True)
def _isolate_copilot_home(monkeypatch, tmp_path):
    """Redirect COPILOT_HOME so _ensure_folder_trusted never touches the
    real ~/.copilot/settings.json while running this test module."""
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / ".copilot-test-home"))


# ---------------------------------------------------------------------------
# Helpers for fake subprocesses with real pipes.
# ---------------------------------------------------------------------------

def _make_fake_proc(stdout_text: str, returncode: int = 0):
    """Create a FakeProc with a real pipe for stdout.

    The pipe is pre-loaded with *stdout_text* and the write end is closed,
    so ``readline()`` / ``read()`` work with selectors.
    """
    r_fd, w_fd = os.pipe()
    os.write(w_fd, stdout_text.encode())
    os.close(w_fd)

    class FakeProc:
        def __init__(self):
            self.stdout = os.fdopen(r_fd, "r")
            self.returncode = returncode

        def wait(self):
            return self.returncode

    return FakeProc()


# ---------------------------------------------------------------------------
# parse_copilot_output
# ---------------------------------------------------------------------------

class TestParseCopilotOutput:
    def test_parses_session_id_jsonl(self):
        output = '{"sessionId": "ses_abc123"}\n{"type":"done"}\n'
        result = parse_copilot_output(output)
        assert result["session_id"] == "ses_abc123"

    def test_parses_session_id_snake_case_json(self):
        output = '{"session_id": "ses_xyz"}\n'
        result = parse_copilot_output(output)
        assert result["session_id"] == "ses_xyz"

    def test_fallback_regex(self):
        output = "Starting...\nsession_id: abc123-def456\nReady."
        result = parse_copilot_output(output)
        assert result["session_id"] == "abc123-def456"

    def test_no_match_returns_none(self):
        result = parse_copilot_output("just some random output")
        assert result["session_id"] is None

    def test_case_insensitive_regex(self):
        output = "Session_ID: UPPER_CASE_123"
        result = parse_copilot_output(output)
        assert result["session_id"] == "UPPER_CASE_123"

    def test_jsonl_takes_precedence(self):
        """JSONL match should be returned even if regex would also match."""
        output = '{"sessionId": "from_json"}\nsession_id: from_regex'
        result = parse_copilot_output(output)
        assert result["session_id"] == "from_json"

    def test_empty_string(self):
        result = parse_copilot_output("")
        assert result["session_id"] is None


# ---------------------------------------------------------------------------
# build_copilot_command
# ---------------------------------------------------------------------------

class TestBuildCopilotCommand:
    def test_basic_command(self):
        cmd = build_copilot_command("fix the tests")
        assert cmd[0] == "copilot"
        # Interactive mode required for --remote/--connect to work.
        assert "-i" in cmd
        assert "fix the tests" in cmd
        assert "--allow-all" in cmd
        assert "--remote" in cmd
        assert "--no-auto-update" in cmd
        assert "--no-ask-user" in cmd

    def test_no_silent_or_json_output(self):
        # --silent and --output-format json conflict with the interactive
        # TUI required by --remote/--connect.
        cmd = build_copilot_command("test")
        assert "--silent" not in cmd
        assert "--output-format" not in cmd

    def test_custom_model(self):
        cmd = build_copilot_command("test", model="claude-sonnet-4.6")
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet-4.6"

    def test_custom_binary(self):
        cmd = build_copilot_command("test", copilot_bin="/usr/bin/copilot")
        assert cmd[0] == "/usr/bin/copilot"

    def test_session_id_adds_resume(self):
        cmd = build_copilot_command("test", session_id="abc-123")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "abc-123"

    def test_no_session_id_no_resume(self):
        cmd = build_copilot_command("test")
        assert "--resume" not in cmd


class TestResolveCopilotBin:
    def test_prefers_which_result(self, monkeypatch):
        monkeypatch.setattr("copilot_remote.launcher.shutil.which", lambda name: "/custom/bin/copilot")
        assert _resolve_copilot_bin("copilot") == "/custom/bin/copilot"

    def test_returns_explicit_path_when_missing(self, monkeypatch):
        monkeypatch.setattr("copilot_remote.launcher.shutil.which", lambda name: None)
        assert _resolve_copilot_bin("/opt/copilot/bin/copilot") == "/opt/copilot/bin/copilot"

    def test_falls_back_to_known_install_location(self, monkeypatch, tmp_path):
        candidate = tmp_path / "copilot"
        candidate.write_text("#!/bin/sh\n", encoding="utf-8")

        monkeypatch.setattr("copilot_remote.launcher.shutil.which", lambda name: None)
        monkeypatch.setattr("copilot_remote.launcher._DEFAULT_COPILOT_PATHS", [str(candidate)])

        assert _resolve_copilot_bin("copilot") == str(candidate)


class TestParseRemoteTaskId:
    def test_extracts_task_id_for_requested_session(self):
        log_text = """
2026-04-20T15:36:12.106Z [INFO] Creating new session with provided ID: requested-session
2026-04-20T15:36:13.192Z [INFO] Remote session active (steerable): https://github.com/org/repo/tasks/2c9fa2a9-5ee6-4504-9fd0-afa54132b304
        """
        assert _parse_remote_task_id(log_text, "requested-session") == "2c9fa2a9-5ee6-4504-9fd0-afa54132b304"

    def test_ignores_unrelated_sessions(self):
        log_text = "Remote session active (steerable): https://github.com/org/repo/tasks/2c9fa2a9-5ee6-4504-9fd0-afa54132b304"
        assert _parse_remote_task_id(log_text, "requested-session") is None

    def test_extracts_task_id_without_session_filter(self):
        log_text = "Remote session active (steerable): https://github.com/org/repo/tasks/2c9fa2a9-5ee6-4504-9fd0-afa54132b304"
        assert _parse_remote_task_id(log_text) == "2c9fa2a9-5ee6-4504-9fd0-afa54132b304"


class TestInitialPromptDelivery:
    def test_detects_existing_user_message(self):
        assert _remote_task_has_user_message([
            {"type": "session.start"},
            {"type": "user.message"},
        ]) is True

    def test_skips_steering_when_prompt_is_already_present(self, monkeypatch):
        monkeypatch.setattr(
            "copilot_remote.launcher._resolve_github_auth_token",
            lambda: "token",
        )
        monkeypatch.setattr(
            "copilot_remote.launcher._list_remote_task_events",
            lambda task_id, token: [{"type": "user.message"}],
        )

        steered = []
        monkeypatch.setattr(
            "copilot_remote.launcher._steer_remote_task",
            lambda task_id, prompt, token: steered.append((task_id, prompt, token)),
        )

        result = _ensure_initial_prompt_delivered("task-123", "fix it", check_timeout=0.0)

        assert result == "already-submitted"
        assert steered == []

    def test_steers_when_no_user_message_is_observed(self, monkeypatch):
        monkeypatch.setattr(
            "copilot_remote.launcher._resolve_github_auth_token",
            lambda: "token",
        )
        monkeypatch.setattr(
            "copilot_remote.launcher._list_remote_task_events",
            lambda task_id, token: [],
        )

        steered = []
        monkeypatch.setattr(
            "copilot_remote.launcher._steer_remote_task",
            lambda task_id, prompt, token: steered.append((task_id, prompt, token)),
        )

        result = _ensure_initial_prompt_delivered("task-123", "fix it", check_timeout=0.0)

        assert result == "steered"
        assert steered == [("task-123", "fix it", "token")]

    def test_attempt_warns_when_task_id_is_missing(self):
        result = _attempt_initial_prompt_delivery(None, "fix it")

        assert result["status"] == "unverified"
        assert "remote task ID" in result["warning"]

    def test_attempt_warns_when_steering_fails(self, monkeypatch):
        monkeypatch.setattr(
            "copilot_remote.launcher._ensure_initial_prompt_delivered",
            lambda task_id, prompt: (_ for _ in ()).throw(
                RuntimeError("Unable to resolve a GitHub auth token")
            ),
        )

        result = _attempt_initial_prompt_delivery("task-123", "fix it")

        assert result["status"] == "unverified"
        assert "GitHub auth token" in result["warning"]


# ---------------------------------------------------------------------------
# launch_copilot
# ---------------------------------------------------------------------------

_TEST_SID = "test-sid-00000000-0000-0000-0000-000000000000"


class TestLaunchCopilot:
    def test_dry_run(self):
        repo = RepoEntry(slug="test-repo", path="/test")
        result = launch_copilot(repo, "test prompt", session_id=_TEST_SID, dry_run=True)

        assert result["exit_code"] == 0
        assert result["session_id"] == _TEST_SID
        assert "copilot" in result["cmd"][0]

    def test_dry_run_on_complete(self):
        """on_complete is called synchronously for dry_run."""
        repo = RepoEntry(slug="test-repo", path="/test")
        cb_args = {}

        def on_cb(sid, code):
            cb_args["sid"] = sid
            cb_args["code"] = code

        result = launch_copilot(repo, "test", session_id=_TEST_SID, dry_run=True, on_complete=on_cb)
        assert cb_args["sid"] == _TEST_SID
        assert cb_args["code"] == 0

    def test_spawn_hook_success(self):
        """_spawn hook should be used instead of real Popen."""
        repo = RepoEntry(slug="test-repo", path="/test")

        completed = threading.Event()
        cb_args = {}

        def on_cb(sid, code):
            cb_args["sid"] = sid
            cb_args["code"] = code
            completed.set()

        spawned = []

        def fake_spawn(cmd, cwd):
            spawned.append((cmd, cwd))
            return _make_fake_proc('some output\n', returncode=0)

        result = launch_copilot(
            repo, "test prompt", session_id=_TEST_SID, _spawn=fake_spawn, on_complete=on_cb
        )
        assert result["session_id"] == _TEST_SID
        assert len(spawned) == 1
        assert spawned[0][1] == "/test"
        # Real launches no longer force --resume because Copilot skips
        # startup prompt execution on resume paths.
        assert "--resume" not in result["cmd"]

        # Wait for background thread to finish.
        completed.wait(timeout=5)
        assert cb_args["sid"] == _TEST_SID
        assert cb_args["code"] == 0

    def test_exit_nonzero(self):
        """Non-zero exit code should be captured via on_complete."""
        repo = RepoEntry(slug="test-repo", path="/test")

        completed = threading.Event()
        cb_args = {}

        def on_cb(sid, code):
            cb_args["sid"] = sid
            cb_args["code"] = code
            completed.set()

        result = launch_copilot(
            repo, "test",
            session_id=_TEST_SID,
            _spawn=lambda c, d: _make_fake_proc("error output\n", returncode=1),
            on_complete=on_cb,
        )
        # Session ID is always known upfront
        assert result["session_id"] == _TEST_SID

        completed.wait(timeout=5)
        assert cb_args["code"] == 1

    def test_spawn_failure_raises(self):
        """If _spawn raises, the exception should propagate."""
        repo = RepoEntry(slug="test-repo", path="/test")

        def bad_spawn(cmd, cwd):
            raise OSError("copilot not found")

        with pytest.raises(OSError, match="copilot not found"):
            launch_copilot(repo, "test", session_id=_TEST_SID, _spawn=bad_spawn)

    def test_real_launch_uses_script_exit_code(self, monkeypatch, tmp_path):
        repo = RepoEntry(slug="test-repo", path="/test")
        captured = {}

        class DummyProc:
            pass

        def fake_popen(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return DummyProc()

        monkeypatch.setattr("copilot_remote.launcher._log_dir", lambda: tmp_path)
        monkeypatch.setattr("copilot_remote.launcher._snapshot_process_logs", lambda: {})
        monkeypatch.setattr("copilot_remote.launcher.subprocess.Popen", fake_popen)
        monkeypatch.setattr("copilot_remote.launcher.shutil.which", lambda name: "/resolved/copilot")
        monkeypatch.setattr("copilot_remote.launcher._wait_for_remote_task_id", lambda **kwargs: "task-123")
        monkeypatch.setattr(
            "copilot_remote.launcher._ensure_initial_prompt_delivered",
            lambda task_id, prompt: "steered",
        )

        result = launch_copilot(repo, "test", session_id=_TEST_SID)

        assert result["cmd"][0] == "/resolved/copilot"
        assert "--resume" not in result["cmd"]
        assert result["connect_id"] == "task-123"
        assert captured["args"][0] == "bash"
        assert captured["args"][1] == "-c"
        assert "script -eqfc" in captured["args"][2]
        assert captured["kwargs"]["cwd"] == "/test"

    def test_real_launch_returns_warning_when_prompt_delivery_fails(self, monkeypatch, tmp_path):
        repo = RepoEntry(slug="test-repo", path="/test")

        class DummyProc:
            pid = 4242

        monkeypatch.setattr("copilot_remote.launcher._log_dir", lambda: tmp_path)
        monkeypatch.setattr("copilot_remote.launcher._snapshot_process_logs", lambda: {})
        monkeypatch.setattr(
            "copilot_remote.launcher.subprocess.Popen",
            lambda *args, **kwargs: DummyProc(),
        )
        monkeypatch.setattr("copilot_remote.launcher.shutil.which", lambda name: "/resolved/copilot")
        monkeypatch.setattr("copilot_remote.launcher._wait_for_remote_task_id", lambda **kwargs: "task-123")
        monkeypatch.setattr(
            "copilot_remote.launcher._ensure_initial_prompt_delivered",
            lambda task_id, prompt: (_ for _ in ()).throw(RuntimeError("steer failed")),
        )

        result = launch_copilot(repo, "test", session_id=_TEST_SID)

        assert result["connect_id"] == "task-123"
        assert result["prompt_delivery_status"] == "unverified"
        assert "steer failed" in result["prompt_delivery_warning"]


# ---------------------------------------------------------------------------
# _ensure_folder_trusted
# ---------------------------------------------------------------------------

class TestEnsureFolderTrusted:
    def test_creates_settings_file_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        settings_path = _copilot_settings_path()
        assert not settings_path.exists()

        _ensure_folder_trusted("/some/repo/path")

        data = json.loads(settings_path.read_text())
        assert data["trustedFolders"] == [str(Path("/some/repo/path").resolve())]

    def test_appends_to_existing_trusted_folders_without_dropping_other_keys(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        settings_path = _copilot_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            '// user comment\n{"theme": "dark", "trustedFolders": ["/existing/root"]}\n'
        )

        _ensure_folder_trusted("/brand/new/repo")

        written = settings_path.read_text()
        json_only = "\n".join(
            line for line in written.splitlines() if not line.strip().startswith("//")
        )
        data = json.loads(json_only)
        assert data["theme"] == "dark"
        assert "/existing/root" in data["trustedFolders"]
        assert str(Path("/brand/new/repo").resolve()) in data["trustedFolders"]
        assert "// user comment" in written

    def test_noop_when_path_is_under_an_already_trusted_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        settings_path = _copilot_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        root = str(tmp_path / "workspace")
        settings_path.write_text(json.dumps({"trustedFolders": [root]}))
        before = settings_path.read_text()

        _ensure_folder_trusted(str(Path(root) / "repos" / "some-repo"))

        assert settings_path.read_text() == before

    def test_blank_trusted_entry_does_not_match_every_path(self, tmp_path, monkeypatch):
        """A stray empty string in trustedFolders must not act as a universal prefix match."""
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        settings_path = _copilot_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"trustedFolders": [""]}))

        _ensure_folder_trusted("/brand/new/repo")

        data = json.loads(settings_path.read_text())
        assert str(Path("/brand/new/repo").resolve()) in data["trustedFolders"]

    def test_repeat_calls_do_not_duplicate_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        settings_path = _copilot_settings_path()

        _ensure_folder_trusted("/repo/one")
        _ensure_folder_trusted("/repo/one")

        data = json.loads(settings_path.read_text())
        assert data["trustedFolders"].count(str(Path("/repo/one").resolve())) == 1

    def test_dry_run_launch_does_not_touch_settings_file(self, tmp_path, monkeypatch):
        """dry_run must stay a pure preview: no trust pre-seed side effect."""
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        settings_path = _copilot_settings_path()
        repo = RepoEntry(slug="test-repo", path="/test")

        launch_copilot(repo, "test prompt", session_id=_TEST_SID, dry_run=True)

        assert not settings_path.exists()

    def test_real_launch_pre_seeds_trust_before_spawning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        settings_path = _copilot_settings_path()
        repo = RepoEntry(slug="test-repo", path=str(tmp_path / "some-repo"))

        launch_copilot(
            repo, "test prompt", session_id=_TEST_SID,
            _spawn=lambda cmd, cwd: _make_fake_proc("output\n", returncode=0),
        )

        data = json.loads(settings_path.read_text())
        assert str((tmp_path / "some-repo").resolve()) in data["trustedFolders"]
