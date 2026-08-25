from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import hermes_cli.cli_auth_broker as cli_auth_module
from hermes_cli.cli_auth_broker import CliAuthBroker, CliAuthError, parse_login_view


class FakeMux:
    def __init__(self):
        self.created = []
        self.inputs = []
        self.closed = []
        self.sessions = []
        self.ansi = ""
        self.next_pid = 100

    def create(self, session, *, cwd, command=None):
        self.created.append((session, cwd, command))
        self.sessions = [{"name": session, "pane_pid": self.next_pid}]
        self.next_pid += 1
        return {"ok": True, "session": session}

    def list_sessions(self):
        return {"available": True, "engine": "rmux", "sessions": self.sessions}

    def capture(self, session, *, lines=500):
        return {"session": session, "ansi": self.ansi, "lines": lines}

    def send_input(self, session, *, text=None, key=None):
        self.inputs.append((session, text, key))
        return {"ok": True}

    def close(self, session):
        self.closed.append(session)
        self.sessions = []
        return {"ok": True}


def test_parse_login_view_returns_only_authorization_metadata() -> None:
    text = "\x1b[34mOpen https://example.test/oauth?state=abc\x1b[0m\nPaste authorization code here:"
    view = parse_login_view(text)
    assert view == {
        "auth_url": "https://example.test/oauth?state=abc",
        "expects_code": True,
        "status": "pending",
    }
    assert "Paste" not in str(view)


@pytest.mark.parametrize("terminator", ("\x07", "\x1b\\"), ids=("bel", "st"))
def test_parse_login_view_strips_osc8_hyperlinks(terminator: str) -> None:
    auth_url = "https://claude.com/cai/oauth/authorize?code=test#state"
    output = (
        f"\x1b]8;;{auth_url}{terminator}"
        f"{auth_url}"
        f"\x1b]8;;{terminator}\n"
        "Paste code here if prompted >"
    )

    assert parse_login_view(output) == {
        "auth_url": auth_url,
        "expects_code": True,
        "status": "pending",
    }


def test_parse_login_view_recognizes_installed_claude_code_prompt() -> None:
    assert parse_login_view("Paste code here if prompted >") == {
        "expects_code": True,
        "status": "pending",
    }


def test_start_uses_isolated_provider_slot_and_fixed_command(tmp_path) -> None:
    mux = FakeMux()
    broker = CliAuthBroker(home=tmp_path, mux=mux, which=lambda name: f"/bin/{name}")
    result = broker.start("claude-code", "work")
    session, cwd, command = mux.created[0]
    slot = tmp_path / "accounts" / "claude-code" / "work"
    assert result == {
        "account_id": "work",
        "provider": "claude-code",
        "session_id": session,
        "status": "pending",
    }
    assert cwd == str(slot)
    unset_keys = [*cli_auth_module._SPECS["claude-code"].unset_env, "CLAUDE_CONFIG_DIR"]
    expected_prefix = ["env"]
    for key in unset_keys:
        expected_prefix.extend(("-u", key))
    assert command == [
        *expected_prefix,
        f"CLAUDE_CONFIG_DIR={slot}",
        "/bin/claude",
        "auth",
        "login",
        "--claudeai",
    ]
    assert oct(slot.stat().st_mode & 0o777) == "0o700"


def test_default_claude_profile_unsets_config_dir_and_status_is_redacted(tmp_path) -> None:
    mux = FakeMux()
    status_calls = []

    def run(argv, **kwargs):
        status_calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"max",'
                '"email":"private@example.test","orgId":"org-secret","token":"never-return"}'
            ),
            stderr="also-private",
        )

    broker = CliAuthBroker(home=tmp_path, mux=mux, which=lambda name: f"/bin/{name}", run=run)
    started = broker.start("claude-code", "default")
    command = mux.created[0][2]
    assert command[-4:] == ["/bin/claude", "auth", "login", "--claudeai"]
    assert "CLAUDE_CONFIG_DIR=" not in " ".join(command)

    result = broker.status("claude-code", "default")
    assert result == {
        "label": "default",
        "loggedIn": True,
        "authMethod": "claude.ai",
        "subscriptionType": "max",
    }
    assert set(result) == {"label", "loggedIn", "authMethod", "subscriptionType"}
    assert "private@example.test" not in str(result)
    assert "org-secret" not in str(result)
    assert "never-return" not in str(result)
    assert "CLAUDE_CONFIG_DIR" not in status_calls[0][1]
    assert started["account_id"] == "default"


def test_list_statuses_returns_default_and_safe_direct_slots_only(tmp_path) -> None:
    provider_root = tmp_path / "accounts" / "claude-code"
    provider_root.mkdir(parents=True)
    (provider_root / "simono").mkdir()
    (provider_root / "moonbase").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (provider_root / "linked").symlink_to(outside, target_is_directory=True)

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"loggedIn":false,"authMethod":"none","subscriptionType":null}',
            stderr="",
        )

    broker = CliAuthBroker(home=tmp_path, mux=FakeMux(), which=lambda name: f"/bin/{name}", run=run)
    statuses = broker.list_statuses("claude-code")
    assert [status["label"] for status in statuses] == ["default", "moonbase", "simono"]
    assert all(set(status) == {"label", "loggedIn", "authMethod", "subscriptionType"} for status in statuses)


def test_logged_out_json_is_accepted_when_claude_status_exits_nonzero(tmp_path) -> None:
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout='{"loggedIn":false,"authMethod":"none","subscriptionType":null}',
            stderr="Not logged in",
        )

    broker = CliAuthBroker(home=tmp_path, mux=FakeMux(), which=lambda name: f"/bin/{name}", run=run)
    assert broker.status("claude-code", "simono") == {
        "label": "simono",
        "loggedIn": False,
        "authMethod": "none",
        "subscriptionType": None,
    }


def test_logged_in_json_fails_closed_when_claude_status_exits_nonzero(tmp_path) -> None:
    mux = FakeMux()

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            2,
            stdout='{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"max"}',
            stderr="status crashed",
        )

    broker = CliAuthBroker(home=tmp_path, mux=mux, which=lambda name: f"/bin/{name}", run=run)
    assert broker.status("claude-code", "simono") == {
        "label": "simono",
        "loggedIn": None,
        "authMethod": None,
        "subscriptionType": None,
    }

    started = broker.start("claude-code", "simono")
    mux.sessions = []
    assert broker.poll("claude-code", "simono", started["session_id"]) == {
        "status": "error",
        "label": "simono",
        "loggedIn": None,
        "authMethod": None,
        "subscriptionType": None,
    }


@pytest.mark.parametrize("returncode", [0, 2])
def test_partial_claude_status_never_surfaces_auth_or_plan(
    tmp_path, returncode
) -> None:
    mux = FakeMux()

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            returncode,
            stdout='{"authMethod":"claude.ai","subscriptionType":"max"}',
            stderr="partial status",
        )

    broker = CliAuthBroker(
        home=tmp_path,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        run=run,
    )
    unknown = {
        "label": "simono",
        "loggedIn": None,
        "authMethod": None,
        "subscriptionType": None,
    }
    assert broker.status("claude-code", "simono") == unknown

    started = broker.start("claude-code", "simono")
    mux.sessions = []
    assert broker.poll("claude-code", "simono", started["session_id"]) == {
        "status": "error",
        **unknown,
    }


def test_start_resumes_an_existing_persistent_login_session(tmp_path) -> None:
    mux = FakeMux()
    broker = CliAuthBroker(home=tmp_path, mux=mux, which=lambda name: f"/bin/{name}")
    first = broker.start("claude-code", "work")
    mux.created.clear()
    second = CliAuthBroker(home=tmp_path, mux=mux, which=lambda name: f"/bin/{name}").start("claude-code", "work")
    assert second["session_id"] == first["session_id"]
    assert mux.created == []


def test_concurrent_starts_create_one_owned_session_per_slot(tmp_path) -> None:
    class SlowMux(FakeMux):
        def create(self, session, *, cwd, command=None):
            time.sleep(0.05)
            return super().create(session, cwd=cwd, command=command)

    mux = SlowMux()
    nonces = iter(["a1b2c3d4e5f6", "001122334455"])

    def start_login():
        return CliAuthBroker(
            home=tmp_path,
            mux=mux,
            which=lambda name: f"/bin/{name}",
            nonce=lambda: next(nonces),
        ).start("claude-code", "work")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: start_login(), range(2)))

    assert len(mux.created) == 1
    assert results[0]["session_id"] == results[1]["session_id"]
    slot = tmp_path / "accounts" / "claude-code" / "work"
    marker_session, marker_pid = CliAuthBroker._read_marker(slot)
    assert marker_session == results[0]["session_id"]
    assert marker_pid == mux.sessions[0]["pane_pid"]


def test_windows_slot_lock_uses_cross_process_file_lock(tmp_path, monkeypatch) -> None:
    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.calls = []

        def locking(self, fd, mode, size):
            self.calls.append((mode, size))

    fake_msvcrt = FakeMsvcrt()
    monkeypatch.setattr(cli_auth_module, "fcntl", None)
    monkeypatch.setattr(cli_auth_module, "msvcrt", fake_msvcrt)
    slot = tmp_path / "accounts" / "claude-code" / "work"
    slot.mkdir(parents=True)

    with CliAuthBroker._slot_start_lock(slot):
        assert (slot / ".hermes-auth-session.lock").read_bytes() == b"\0"

    assert fake_msvcrt.calls == [
        (fake_msvcrt.LK_LOCK, 1),
        (fake_msvcrt.LK_UNLCK, 1),
    ]


def test_start_rejects_an_unowned_mux_name_collision(tmp_path) -> None:
    mux = FakeMux()
    mux.sessions = [{"name": "hermes-auth-claude-code-work-a1b2c3d4e5f6", "pane_pid": 999}]
    broker = CliAuthBroker(
        home=tmp_path,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: "a1b2c3d4e5f6",
    )
    with pytest.raises(CliAuthError, match="not owned"):
        broker.start("claude-code", "work")


def test_start_rejects_an_orphaned_session_for_the_same_account_slot(tmp_path) -> None:
    mux = FakeMux()
    mux.sessions = [
        {
            "name": "hermes-auth-claude-code-work-aaaaaaaaaaaa",
            "pane_pid": 999,
        }
    ]
    broker = CliAuthBroker(
        home=tmp_path,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: "bbbbbbbbbbbb",
    )

    with pytest.raises(CliAuthError, match="not owned"):
        broker.start("claude-code", "work")
    assert mux.created == []


def test_similarly_prefixed_account_slot_does_not_block_start(tmp_path) -> None:
    mux = FakeMux()
    mux.sessions = [{"name": "hermes-auth-cc-work-a-aaaaaaaaaaaa", "pane_pid": 999}]
    broker = CliAuthBroker(
        home=tmp_path,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: "bbbbbbbbbbbb",
    )

    started = broker.start("claude-code", "work")
    assert started["session_id"] == (
        f"{broker._session_prefix('claude-code', 'work')}bbbbbbbbbbbb"
    )


def test_max_length_account_id_produces_a_valid_mux_session_name(tmp_path) -> None:
    broker = CliAuthBroker(
        home=tmp_path,
        mux=FakeMux(),
        which=lambda name: f"/bin/{name}",
        nonce=lambda: "a1b2c3d4e5f6",
    )

    started = broker.start("claude-code", "a" * 32)
    assert len(started["session_id"]) <= 64
    assert started["session_id"] == (
        f"{broker._session_prefix('claude-code', 'a' * 32)}a1b2c3d4e5f6"
    )


def test_stale_marker_with_different_pid_fails_closed(tmp_path) -> None:
    mux = FakeMux()
    broker = CliAuthBroker(
        home=tmp_path,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: "a1b2c3d4e5f6",
    )
    first = broker.start("claude-code", "work")
    mux.sessions = [{"name": first["session_id"], "pane_pid": 999}]
    with pytest.raises(CliAuthError, match="not owned"):
        broker.start("claude-code", "work")
    assert len(mux.created) == 1


def test_owned_finished_session_marker_allows_a_fresh_login(tmp_path) -> None:
    mux = FakeMux()
    nonces = iter(("aaaaaaaaaaaa", "bbbbbbbbbbbb"))
    broker = CliAuthBroker(
        home=tmp_path,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: next(nonces),
    )
    first = broker.start("claude-code", "work")
    mux.sessions = []

    second = broker.start("claude-code", "work")

    assert second["session_id"] != first["session_id"]
    assert second["session_id"].endswith("bbbbbbbbbbbb")
    marker_session, _marker_pid = broker._read_marker(
        tmp_path / "accounts" / "claude-code" / "work"
    )
    assert marker_session == second["session_id"]


def test_submit_code_is_literal_and_separate_from_enter(tmp_path) -> None:
    mux = FakeMux()
    broker = CliAuthBroker(home=tmp_path, mux=mux, which=lambda name: f"/bin/{name}")
    started = broker.start("claude-code", "work")
    broker.submit("claude-code", "work", started["session_id"], "abc-$(touch /tmp/nope)")
    assert mux.inputs == [
        (started["session_id"], "abc-$(touch /tmp/nope)", None),
        (started["session_id"], None, "Enter"),
    ]
    with pytest.raises(CliAuthError, match="invalid authorization code"):
        broker.submit("claude-code", "work", started["session_id"], "bad\ncode")


def test_poll_returns_url_while_mux_lives_then_verifies_cli_status(tmp_path) -> None:
    mux = FakeMux()
    statuses = []

    def run(argv, **kwargs):
        statuses.append((argv, kwargs.get("env")))
        return subprocess.CompletedProcess(argv, 0, stdout="Logged in", stderr="")

    broker = CliAuthBroker(home=tmp_path, mux=mux, which=lambda name: f"/bin/{name}", run=run)
    started = broker.start("openai-cli", "personal")
    mux.ansi = "Open this URL: https://auth.openai.com/example\nPaste the code from your browser"
    assert broker.poll("openai-cli", "personal", started["session_id"]) == {
        "auth_url": "https://auth.openai.com/example",
        "expects_code": True,
        "status": "pending",
    }

    mux.sessions = []
    assert broker.poll("openai-cli", "personal", started["session_id"])["status"] == "approved"
    assert statuses[0][0] == ["/bin/codex", "login", "status"]
    assert statuses[0][1]["CODEX_HOME"].endswith("accounts/openai-cli/personal")


def test_poll_fails_closed_when_status_command_is_ambiguous(tmp_path) -> None:
    mux = FakeMux()
    broker = CliAuthBroker(
        home=tmp_path,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="Not authenticated", stderr=""),
    )
    started = broker.start("claude-code", "work")
    mux.sessions = []
    assert broker.poll("claude-code", "work", started["session_id"])["status"] == "error"


def test_submit_rejects_an_unowned_deterministic_session(tmp_path) -> None:
    mux = FakeMux()
    session_id = "hermes-auth-claude-code-work-a1b2c3d4e5f6"
    mux.sessions = [{"name": session_id, "pane_pid": 999}]
    broker = CliAuthBroker(home=tmp_path, mux=mux, which=lambda name: f"/bin/{name}")
    with pytest.raises(CliAuthError, match="not owned"):
        broker.submit("claude-code", "work", session_id, "code")
    assert mux.inputs == []


def test_account_slot_rejects_symlinks(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    slot = tmp_path / "accounts" / "claude-code" / "work"
    slot.parent.mkdir(parents=True)
    slot.symlink_to(outside, target_is_directory=True)
    broker = CliAuthBroker(home=tmp_path, mux=FakeMux(), which=lambda name: f"/bin/{name}")
    with pytest.raises(CliAuthError, match="symlink"):
        broker.start("claude-code", "work")


def test_status_scrubs_inherited_cloud_backend_modes(tmp_path, monkeypatch) -> None:
    inherited = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "ANTHROPIC_BASE_URL": "https://proxy.example.test",
        "AWS_BEARER_TOKEN_BEDROCK": "secret",
        "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/google.json",
        "AZURE_CLIENT_SECRET": "secret",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    observed_env = {}

    def run(argv, **kwargs):
        observed_env.update(kwargs["env"])
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout='{"loggedIn":false,"authMethod":"none"}',
            stderr="",
        )

    broker = CliAuthBroker(
        home=tmp_path,
        mux=FakeMux(),
        which=lambda name: f"/bin/{name}",
        run=run,
    )

    assert broker.status("claude-code", "isolated")["loggedIn"] is False
    assert all(key not in observed_env for key in inherited)


def test_start_closes_created_session_when_inventory_refresh_fails(tmp_path) -> None:
    class InventoryFailureMux(FakeMux):
        list_calls = 0

        def list_sessions(self):
            self.list_calls += 1
            if self.list_calls >= 2:
                raise RuntimeError("inventory failed")
            return super().list_sessions()

    mux = InventoryFailureMux()
    broker = CliAuthBroker(
        home=tmp_path,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: "aaaaaaaaaaaa",
    )

    with pytest.raises(CliAuthError, match="marker could not be written"):
        broker.start("claude-code", "work")

    assert mux.closed == [
        f"{broker._session_prefix('claude-code', 'work')}aaaaaaaaaaaa"
    ]
    assert mux.sessions == []


def test_same_account_in_distinct_profiles_uses_distinct_mux_names(tmp_path) -> None:
    class SharedMux(FakeMux):
        def create(self, session, *, cwd, command=None):
            self.created.append((session, cwd, command))
            self.sessions.append({"name": session, "pane_pid": self.next_pid})
            self.next_pid += 1
            return {"ok": True, "session": session}

    mux = SharedMux()
    profile_a = tmp_path / "profiles" / "a"
    profile_b = tmp_path / "profiles" / "b"
    broker_a = CliAuthBroker(
        home=profile_a,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: "aaaaaaaaaaaa",
    )
    broker_b = CliAuthBroker(
        home=profile_b,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: "bbbbbbbbbbbb",
    )

    started_a = broker_a.start("claude-code", "work")
    started_b = broker_b.start("claude-code", "work")

    assert started_a["session_id"] != started_b["session_id"]
    assert started_a["session_id"].startswith(
        f"ha-cc-{broker_a._home_tag()}-work-"
    )
    assert started_b["session_id"].startswith(
        f"ha-cc-{broker_b._home_tag()}-work-"
    )
    assert len(mux.sessions) == 2


def test_default_slot_is_shared_across_profiles(tmp_path) -> None:
    class SharedMux(FakeMux):
        def create(self, session, *, cwd, command=None):
            self.created.append((session, cwd, command))
            self.sessions.append({"name": session, "pane_pid": self.next_pid})
            self.next_pid += 1
            return {"ok": True, "session": session}

    mux = SharedMux()
    broker_a = CliAuthBroker(
        home=tmp_path / "profiles" / "a",
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: "aaaaaaaaaaaa",
    )
    broker_b = CliAuthBroker(
        home=tmp_path / "profiles" / "b",
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: "bbbbbbbbbbbb",
    )

    started_a = broker_a.start("claude-code", "default")
    started_b = broker_b.start("claude-code", "default")

    assert started_a["session_id"] == started_b["session_id"]
    assert len(mux.created) == 1
    _session, cwd, command = mux.created[0]
    assert cwd == str(tmp_path / "accounts" / "claude-code" / "default")
    assert not any(part.startswith("CLAUDE_CONFIG_DIR=") for part in command)


def test_accounts_parent_symlink_is_rejected(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "accounts").symlink_to(outside, target_is_directory=True)
    broker = CliAuthBroker(
        home=home,
        mux=FakeMux(),
        which=lambda name: f"/bin/{name}",
    )

    with pytest.raises(CliAuthError, match="accounts directory.*symlink"):
        broker.start("claude-code", "work")
    assert list(outside.iterdir()) == []


def test_login_start_fails_safely_on_non_posix_gateway(tmp_path) -> None:
    mux = FakeMux()
    broker = CliAuthBroker(
        home=tmp_path,
        mux=mux,
        which=lambda name: f"C:/bin/{name}.exe",
        platform_name="nt",
    )

    with pytest.raises(CliAuthError, match="only on POSIX"):
        broker.start("claude-code", "work")
    assert mux.created == []


def test_cancel_cannot_clear_marker_created_by_a_new_start(tmp_path) -> None:
    mux = FakeMux()
    nonces = iter(("aaaaaaaaaaaa", "bbbbbbbbbbbb"))
    broker = CliAuthBroker(
        home=tmp_path,
        mux=mux,
        which=lambda name: f"/bin/{name}",
        nonce=lambda: next(nonces),
    )
    old = broker.start("claude-code", "work")
    mux.sessions = []
    asserted = threading.Event()
    release = threading.Event()
    original_assert = broker._assert_owned_session

    def blocking_assert(*args, **kwargs):
        result = original_assert(*args, **kwargs)
        asserted.set()
        assert release.wait(timeout=2)
        return result

    broker._assert_owned_session = blocking_assert
    with ThreadPoolExecutor(max_workers=2) as executor:
        cancelling = executor.submit(
            broker.cancel,
            "claude-code",
            "work",
            old["session_id"],
        )
        assert asserted.wait(timeout=2)
        starting = executor.submit(broker.start, "claude-code", "work")
        assert not starting.done()
        release.set()
        assert cancelling.result(timeout=2)["status"] == "cancelled"
        fresh = starting.result(timeout=2)

    slot = tmp_path / "accounts" / "claude-code" / "work"
    marker_session, _marker_pid = broker._read_marker(slot)
    assert marker_session == fresh["session_id"]
    assert fresh["session_id"] in {row["name"] for row in mux.sessions}


def test_cancel_and_identifiers_are_fail_closed(tmp_path) -> None:
    mux = FakeMux()
    broker = CliAuthBroker(home=tmp_path, mux=mux, which=lambda name: f"/bin/{name}")
    started = broker.start("claude-code", "work")
    assert broker.cancel("claude-code", "work", started["session_id"])["status"] == "cancelled"
    assert mux.closed == [started["session_id"]]
    assert not (tmp_path / "accounts" / "claude-code" / "work" / ".hermes-auth-session").exists()
    with pytest.raises(CliAuthError):
        broker.start("claude-code", "../escape")
    with pytest.raises(CliAuthError):
        broker.poll("claude-code", "work", "other-session")
    with pytest.raises(CliAuthError):
        broker.start("unknown", "work")
