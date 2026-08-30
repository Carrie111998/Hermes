"""Tests for Bug #12905 fixes in agent/anthropic_adapter.py — macOS Keychain support."""

import json
import re
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from agent.anthropic_adapter import (
    _read_claude_code_credentials_from_keychain,
    read_claude_code_credentials,
    _refresh_oauth_token,
)
from agent.anthropic_credentials import (
    _security_i_escape,
    _write_claude_code_credentials,
)


# This module exercises the reader itself with explicit platform and subprocess
# mocks, so it opts out of the suite-wide guard without touching a real Keychain.
pytestmark = pytest.mark.allow_macos_keychain


@pytest.mark.macos_only
class TestReadClaudeCodeCredentialsFromKeychain:
    """Bug 4: macOS Keychain support for Claude Code >=2.1.114.

    ``macos_only``: the reader is gated on ``platform.system() == "Darwin"``
    and shells out to the ``security`` CLI. Faking Darwin on Linux selected
    the branch but proved nothing about the host it exists for; on the real
    macOS runner only ``subprocess.run`` is mocked (via the
    ``allow_macos_keychain`` opt-out of the suite-wide guard), so no real
    Keychain is ever touched.
    """



    def test_returns_none_when_security_command_not_found(self):
        """OSError from missing security binary must be handled gracefully."""
        with patch("agent.anthropic_adapter.subprocess.run",
                   side_effect=OSError("security not found")):
            assert _read_claude_code_credentials_from_keychain() is None

    def test_returns_none_on_nonzero_exit_code(self):
        """security returns non-zero when the Keychain entry doesn't exist."""
        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            assert _read_claude_code_credentials_from_keychain() is None







@pytest.mark.macos_only
class TestReadClaudeCodeCredentialsPriority:
    """Bug 4: Keychain must be checked before the JSON file."""

    def test_keychain_takes_priority_over_json_file(self, tmp_path, monkeypatch):
        """When both Keychain and JSON file have credentials, Keychain wins."""
        # Set up JSON file with "older" token
        json_cred_file = tmp_path / ".claude" / ".credentials.json"
        json_cred_file.parent.mkdir(parents=True)
        json_cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "json-token",
                "refreshToken": "json-refresh",
                "expiresAt": 9999999999999,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        # Mock Keychain to return a "newer" token
        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "claudeAiOauth": {
                        "accessToken": "keychain-token",
                        "refreshToken": "keychain-refresh",
                        "expiresAt": 9999999999999,
                    }
                }),
                stderr="",
            )
            creds = read_claude_code_credentials()

        # Keychain token should be returned, not JSON file token
        assert creds is not None
        assert creds["accessToken"] == "keychain-token"
        assert creds["source"] == "macos_keychain"

    def test_falls_back_to_json_when_keychain_returns_none(self, tmp_path, monkeypatch):
        """When Keychain has no entry, JSON file is used as fallback."""
        json_cred_file = tmp_path / ".claude" / ".credentials.json"
        json_cred_file.parent.mkdir(parents=True)
        json_cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "json-fallback-token",
                "refreshToken": "json-refresh",
                "expiresAt": 9999999999999,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            # Simulate Keychain entry not found
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "json-fallback-token"
        assert creds["source"] == "claude_code_credentials_file"

    def test_returns_none_when_neither_keychain_nor_json_has_creds(self, tmp_path, monkeypatch):
        """No credentials anywhere — must return None cleanly."""
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            creds = read_claude_code_credentials()

        assert creds is None


@pytest.mark.macos_only
class TestReadClaudeCodeCredentialsDesync:
    """Reconciliation when Keychain and JSON file disagree.

    Observed in the wild on Claude Code 2.1.x: a refresh updates one source
    (commonly the JSON file) but leaves the other holding an expired token.
    The reader must not blindly return whichever source it consulted first;
    it must prefer the non-expired credential.
    """

    # Far-future ms-epoch — comfortably valid under is_claude_code_token_valid.
    _FRESH = 9_999_999_999_999
    # Past ms-epoch — comfortably expired (with the 60s buffer).
    _EXPIRED = 1

    def _setup(self, tmp_path, monkeypatch, *, file_expires_at, file_token="json-token"):
        json_cred_file = tmp_path / ".claude" / ".credentials.json"
        json_cred_file.parent.mkdir(parents=True)
        json_cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": file_token,
                "refreshToken": "json-refresh",
                "expiresAt": file_expires_at,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

    def _keychain_payload(self, *, access_token, expires_at, refresh_token="kc-refresh"):
        return MagicMock(
            returncode=0,
            stdout=json.dumps({
                "claudeAiOauth": {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "expiresAt": expires_at,
                }
            }),
            stderr="",
        )

    def test_keychain_expired_file_fresh_returns_file(self, tmp_path, monkeypatch):
        """Regression: when the Keychain holds an expired token but the JSON
        file has a valid one, callers must receive the valid file token rather
        than None. (Pre-fix behavior returned the expired Keychain token, and
        downstream validity checks then yielded None — surfacing the misleading
        ``No Anthropic credentials found`` error.)
        """
        self._setup(tmp_path, monkeypatch, file_expires_at=self._FRESH, file_token="fresh-file-token")
        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = self._keychain_payload(
                access_token="stale-keychain-token", expires_at=self._EXPIRED,
            )
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "fresh-file-token"
        assert creds["source"] == "claude_code_credentials_file"



    def test_both_expired_prefers_later_expiry(self, tmp_path, monkeypatch):
        """When both are expired, return the one with the later ``expiresAt``;
        its ``refresh_token`` is the most recently issued and most likely to
        succeed at the OAuth refresh endpoint.
        """
        self._setup(tmp_path, monkeypatch, file_expires_at=self._EXPIRED + 5, file_token="newer-expired-file")
        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = self._keychain_payload(
                access_token="older-expired-keychain", expires_at=self._EXPIRED,
            )
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "newer-expired-file"


class TestRefreshOAuthTokenAdoptsFreshCredential:
    """``_refresh_oauth_token`` should adopt a credential Claude Code has
    already refreshed rather than POSTing a (possibly already-rotated)
    single-use refresh token and racing Claude Code into ``invalid_grant``.
    """

    _FRESH = 9_999_999_999_999

    def test_adopts_already_refreshed_token_without_posting(self, tmp_path, monkeypatch):
        """When a live source already holds a valid token, return it and skip
        the network refresh entirely.
        """
        monkeypatch.setattr(
            "agent.anthropic_credentials.claude_code_credentials_path",
            lambda: tmp_path / ".claude" / ".credentials.json",
        )
        fresh = {
            "accessToken": "already-refreshed-token",
            "refreshToken": "live-refresh",
            "expiresAt": self._FRESH,
        }
        monkeypatch.setattr(
            "agent.anthropic_credentials.read_claude_code_credentials",
            lambda: fresh,
        )

        def _should_not_be_called(*args, **kwargs):  # pragma: no cover - guard
            raise AssertionError("refresh_anthropic_oauth_pure must not be called")

        monkeypatch.setattr(
            "agent.anthropic_credentials.refresh_anthropic_oauth_pure",
            _should_not_be_called,
        )

        # Stale creds passed in by the caller — should be ignored in favor
        # of the live, already-refreshed token.
        result = _refresh_oauth_token({"refreshToken": "stale", "expiresAt": 1})
        assert result == "already-refreshed-token"

    def test_falls_back_to_network_refresh_when_no_fresh_credential(self, tmp_path, monkeypatch):
        """When no live source has a valid token, fall back to refreshing
        ourselves using the freshest available refresh token.
        """
        monkeypatch.setattr(
            "agent.anthropic_credentials.claude_code_credentials_path",
            lambda: tmp_path / ".claude" / ".credentials.json",
        )
        # Live read returns an expired credential carrying a refresh token.
        monkeypatch.setattr(
            "agent.anthropic_credentials.read_claude_code_credentials",
            lambda: {"accessToken": "expired", "refreshToken": "live-refresh", "expiresAt": 1},
        )
        captured = {}

        def _fake_refresh(refresh_token, **kwargs):
            captured["refresh_token"] = refresh_token
            return {
                "access_token": "newly-minted",
                "refresh_token": "rotated",
                "expires_at_ms": self._FRESH,
            }

        monkeypatch.setattr(
            "agent.anthropic_credentials.refresh_anthropic_oauth_pure", _fake_refresh
        )
        monkeypatch.setattr(
            "agent.anthropic_credentials._write_claude_code_credentials",
            lambda *a, **k: None,
        )

        result = _refresh_oauth_token({"refreshToken": "caller-refresh", "expiresAt": 1})
        assert result == "newly-minted"
        # Prefers the live source's refresh token over the caller's stale copy.
        assert captured["refresh_token"] == "live-refresh"

    def test_concurrent_refreshes_use_one_shared_credentials_lock(self, tmp_path, monkeypatch):
        """Direct resolver refreshes must not spend one Claude token twice."""
        shared_credentials_path = tmp_path / ".claude" / ".credentials.json"
        monkeypatch.setattr(
            "agent.anthropic_credentials.claude_code_credentials_path",
            lambda: shared_credentials_path,
        )

        state = {
            "accessToken": "stale-access",
            "refreshToken": "stale-refresh",
            "expiresAt": 1,
        }
        state_lock = threading.Lock()
        calls = []

        def read_credentials():
            with state_lock:
                return dict(state)

        def write_credentials(access_token, refresh_token, expires_at_ms, **_kwargs):
            with state_lock:
                state.update(
                    accessToken=access_token,
                    refreshToken=refresh_token,
                    expiresAt=expires_at_ms,
                )

        def refresh(refresh_token, **_kwargs):
            calls.append(refresh_token)
            # Without the production shared lock, both callers read the stale
            # pair before either fake network request commits its rotation.
            time.sleep(0.05)
            with state_lock:
                if state["refreshToken"] != refresh_token:
                    raise ValueError("invalid_grant: refresh token already used")
                return {
                    "access_token": "fresh-access",
                    "refresh_token": "fresh-refresh",
                    "expires_at_ms": self._FRESH,
                }

        monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", read_credentials)
        monkeypatch.setattr("agent.anthropic_credentials._write_claude_code_credentials", write_credentials)
        monkeypatch.setattr("agent.anthropic_credentials.refresh_anthropic_oauth_pure", refresh)

        results = {}
        errors = {}
        start = threading.Barrier(2)

        def run(name):
            try:
                start.wait(timeout=5)
                results[name] = _refresh_oauth_token(
                    {
                        "accessToken": "stale-access",
                        "refreshToken": "stale-refresh",
                        "expiresAt": 1,
                    }
                )
            except BaseException as exc:  # pragma: no cover - failure diagnostics
                errors[name] = exc

        threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not [thread for thread in threads if thread.is_alive()]
        assert not errors, errors
        assert results == {"a": "fresh-access", "b": "fresh-access"}
        assert calls == ["stale-refresh"], calls


class TestSecurityIEscape:
    """``_security_i_escape`` must match what ``security -i``'s parser does
    inside double quotes — backslash and double-quote only. A single quote is
    an ordinary character there (verified against the real CLI: a shell-style
    ``'..'`` span silently truncates the value at the first embedded quote,
    which would corrupt the stored payload).
    """

    def test_escapes_backslash_and_double_quote_only(self):
        assert _security_i_escape('a"b\\c') == 'a\\"b\\\\c'
        assert _security_i_escape("plain") == "plain"
        # A single quote must pass through untouched...
        assert _security_i_escape("it's") == "it's"
        # ...and an already-produced escape must not be re-escaped wholesale:
        # the parser collapses ``\\`` and ``\"`` and leaves everything else.
        assert re.sub(r'\\(.)', r'\1', _security_i_escape('x\'y"z\\w')) == 'x\'y"z\\w'


@pytest.mark.macos_only
class TestWriteClaudeCodeCredentialsMirrorsKeychain:
    """#98334: on Darwin a committed refresh write must also rotate the
    Keychain copy Claude Code reads, not just ~/.claude/.credentials.json.

    Anthropic refresh tokens are single-use and rotating: leaving the new
    pair only in the file strands a spent refresh token in the Keychain, so
    Claude Code's next refresh fails with ``invalid_grant`` and empties the
    entry ("Login: Expired"). All ``security`` traffic is mocked — no real
    Keychain is touched, and every token string below is a dummy fixture.
    """

    _FRESH = 9_999_999_999_999

    # Dummy fixture strings (not credentials of any kind).
    _AT = "dummy-access-token"
    _RT = "dummy-refresh-token"
    _OLD_AT = "dummy-stale-access"
    _OLD_RT = "dummy-spent-refresh"

    def _setup_home(self, tmp_path, monkeypatch, *, file_body=None):
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir(parents=True, exist_ok=True)
        if file_body is not None:
            (cred_dir / ".credentials.json").write_text(json.dumps(file_body))
        monkeypatch.setattr("agent.anthropic_credentials.Path.home", lambda: tmp_path)

    def _security_calls(self, stored_payload, *, add_result=None):
        """Mock ``subprocess.run`` for the three ``security`` invocations the
        mirror makes, recording every (argv, stdin) pair for assertions.
        """
        calls = []

        def _run(cmd, **kwargs):
            cmd = list(cmd)
            calls.append((cmd, kwargs.get("input")))
            if cmd[:2] == ["security", "find-generic-password"] and "-w" not in cmd:
                return MagicMock(
                    returncode=0,
                    stdout=(
                        'keychain: "/Users/x/Library/Keychains/login.keychain-db"\n'
                        '    "acct"<blob>="liuhao"\n'
                        '    "svce"<blob>="Claude Code-credentials"\n'
                    ),
                    stderr="",
                )
            if cmd[:2] == ["security", "find-generic-password"] and "-w" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout=json.dumps({"claudeAiOauth": stored_payload}),
                    stderr="",
                )
            if cmd == ["security", "-i"]:
                if add_result is not None:
                    return add_result
                return MagicMock(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected security call: {cmd}")

        return _run, calls

    @staticmethod
    def _stdin_payload(command):
        """Extract and un-escape the ``-w`` argument the way ``security -i``'s
        parser does, so the merge can be asserted on real JSON.
        """
        quoted = command.split(' -w "', 1)[1].rsplit('"', 1)[0]
        return json.loads(re.sub(r'\\(.)', r'\1', quoted))

    def test_rotated_pair_mirrored_and_merged_into_existing_entry(
        self, tmp_path, monkeypatch
    ):
        """After a refresh both stores must hold the same refreshToken, and
        the Keychain metadata Claude Code gates on must survive the merge.
        """
        stored = {
            "accessToken": self._OLD_AT,
            "refreshToken": self._OLD_RT,
            "expiresAt": 1,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
            "rateLimitTier": "tier_1",
        }
        self._setup_home(tmp_path, monkeypatch, file_body={"claudeAiOauth": {
            "accessToken": self._OLD_AT,
            "refreshToken": self._OLD_RT,
            "expiresAt": 1,
            "scopes": ["user:inference"],
        }})
        _run, calls = self._security_calls(stored)

        with patch("agent.anthropic_credentials.subprocess.run", side_effect=_run):
            _write_claude_code_credentials(self._AT, self._RT, self._FRESH)

        add_calls = [c for c in calls if c[0] == ["security", "-i"]]
        assert len(add_calls) == 1, calls
        argv, command = add_calls[0]

        # The payload is a secret: it must travel via stdin, never argv.
        assert self._RT not in " ".join(argv)
        assert command.startswith(
            'add-generic-password -U -a "liuhao" '
            '-s "Claude Code-credentials" -w "'
        )

        oauth = self._stdin_payload(command)["claudeAiOauth"]
        assert oauth["accessToken"] == self._AT
        assert oauth["refreshToken"] == self._RT
        assert oauth["expiresAt"] == self._FRESH
        # Metadata survives the rotation...
        assert oauth["subscriptionType"] == "max"
        assert oauth["rateLimitTier"] == "tier_1"
        # ...while the file-side scopes (the write's own authority) win.
        assert oauth["scopes"] == ["user:inference"]

        # The file store committed the same rotation.
        on_disk = json.loads(
            (tmp_path / ".claude" / ".credentials.json").read_text()
        )["claudeAiOauth"]
        assert on_disk["refreshToken"] == self._RT
        assert on_disk["scopes"] == ["user:inference"]

    def test_merges_into_emptied_entry_after_invalid_grant(self, tmp_path, monkeypatch):
        """Claude Code's own ``invalid_grant`` handling empties the token
        triple but leaves the metadata; the mirror must still repopulate it.
        """
        stored = {
            "accessToken": "",
            "refreshToken": "",
            "expiresAt": "",
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        }
        self._setup_home(tmp_path, monkeypatch)
        _run, calls = self._security_calls(stored)

        with patch("agent.anthropic_credentials.subprocess.run", side_effect=_run):
            _write_claude_code_credentials(self._AT, self._RT, self._FRESH)

        add_calls = [c for c in calls if c[0] == ["security", "-i"]]
        assert len(add_calls) == 1, calls
        oauth = self._stdin_payload(add_calls[0][1])["claudeAiOauth"]
        assert oauth["accessToken"] == self._AT
        assert oauth["refreshToken"] == self._RT
        assert oauth["expiresAt"] == self._FRESH
        assert oauth["subscriptionType"] == "max"

    def test_no_existing_entry_means_no_keychain_write(self, tmp_path, monkeypatch):
        """A user whose Claude Code install does not use the Keychain must
        not gain an entry from a Hermes refresh.
        """
        self._setup_home(tmp_path, monkeypatch)
        calls = []

        def _run(cmd, **kwargs):
            cmd = list(cmd)
            calls.append(cmd)
            assert cmd[:2] == ["security", "find-generic-password"]
            return MagicMock(returncode=44, stdout="", stderr="could not be found")

        with patch("agent.anthropic_credentials.subprocess.run", side_effect=_run):
            _write_claude_code_credentials(self._AT, self._RT, self._FRESH)

        # Metadata probe only — no payload read, no add.
        assert calls == [["security", "find-generic-password",
                          "-s", "Claude Code-credentials"]]
        on_disk = json.loads(
            (tmp_path / ".claude" / ".credentials.json").read_text()
        )["claudeAiOauth"]
        assert on_disk["refreshToken"] == self._RT

    def test_unreadable_account_attribute_skips_mirror(self, tmp_path, monkeypatch):
        """An entry without a parsable ``acct`` cannot be matched by
        ``add-generic-password -U``; updating blind would risk creating a
        duplicate entry, so the mirror must stand down.
        """
        self._setup_home(tmp_path, monkeypatch)
        calls = []

        def _run(cmd, **kwargs):
            cmd = list(cmd)
            calls.append(cmd)
            return MagicMock(
                returncode=0,
                stdout='    "acct"<blob>=<NULL>\n    "svce"<blob>="Claude Code-credentials"\n',
                stderr="",
            )

        with patch("agent.anthropic_credentials.subprocess.run", side_effect=_run):
            _write_claude_code_credentials(self._AT, self._RT, self._FRESH)

        assert len(calls) == 1, calls
        assert (tmp_path / ".claude" / ".credentials.json").exists()

    def test_keychain_failure_does_not_fail_the_committed_write(
        self, tmp_path, monkeypatch
    ):
        """The JSON file write is the commit step of the refresh transaction;
        a locked/unavailable Keychain must not un-commit it. Regression guard
        for the fail-closed contract: every caller of
        ``_write_claude_code_credentials`` treats an exception as a failed
        rotation and would mark a perfectly good rotated pair as spent.
        """
        stored = {
            "accessToken": self._OLD_AT,
            "refreshToken": self._OLD_RT,
            "expiresAt": 1,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        }
        self._setup_home(tmp_path, monkeypatch, file_body={"claudeAiOauth": {
            "accessToken": self._OLD_AT,
            "refreshToken": self._OLD_RT,
            "expiresAt": 1,
        }})
        _run, _calls = self._security_calls(
            stored,
            add_result=MagicMock(
                returncode=1, stdout="",
                stderr="security: SecKeychainItemUpdate: UNIX Operation not permitted",
            ),
        )

        with patch("agent.anthropic_credentials.subprocess.run", side_effect=_run):
            _write_claude_code_credentials(self._AT, self._RT, self._FRESH)

        on_disk = json.loads(
            (tmp_path / ".claude" / ".credentials.json").read_text()
        )["claudeAiOauth"]
        assert on_disk["refreshToken"] == self._RT

    def test_security_timeout_does_not_fail_the_committed_write(
        self, tmp_path, monkeypatch
    ):
        import subprocess as _subprocess

        stored = {
            "accessToken": self._OLD_AT,
            "refreshToken": self._OLD_RT,
            "expiresAt": 1,
            "scopes": ["user:inference"],
        }
        self._setup_home(tmp_path, monkeypatch, file_body={"claudeAiOauth": {
            "accessToken": self._OLD_AT,
            "refreshToken": self._OLD_RT,
            "expiresAt": 1,
        }})
        state = {"phase": 0}

        def _run(cmd, **kwargs):
            cmd = list(cmd)
            if cmd == ["security", "-i"]:
                raise _subprocess.TimeoutExpired(cmd, 5)
            state["phase"] += 1
            if state["phase"] == 1:
                return MagicMock(
                    returncode=0,
                    stdout='    "acct"<blob>="liuhao"\n',
                    stderr="",
                )
            return MagicMock(
                returncode=0,
                stdout=json.dumps({"claudeAiOauth": stored}),
                stderr="",
            )

        with patch("agent.anthropic_credentials.subprocess.run", side_effect=_run):
            _write_claude_code_credentials(self._AT, self._RT, self._FRESH)

        on_disk = json.loads(
            (tmp_path / ".claude" / ".credentials.json").read_text()
        )["claudeAiOauth"]
        assert on_disk["refreshToken"] == self._RT


@pytest.mark.linux_only
class TestWriteClaudeCodeCredentialsIgnoresKeychainOnLinux:
    """#98334 guard: off Darwin the mirror must not shell out to ``security``
    at all — there is no Keychain to keep in step with. Runs on the Linux
    lane for real rather than faking the platform (see conftest OS-gating
    rationale).
    """

    def test_non_darwin_makes_no_security_calls(self, tmp_path, monkeypatch):
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir(parents=True)
        monkeypatch.setattr("agent.anthropic_credentials.Path.home", lambda: tmp_path)

        with patch("agent.anthropic_credentials.subprocess.run") as mock_run:
            _write_claude_code_credentials("dummy-access-token", "dummy-refresh-token", 12345)

        assert mock_run.call_count == 0
        on_disk = json.loads(
            (tmp_path / ".claude" / ".credentials.json").read_text()
        )["claudeAiOauth"]
        assert on_disk["refreshToken"] == "dummy-refresh-token"

