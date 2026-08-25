"""Persistent, gateway-local CLI OAuth sessions backed by rmux/tmux.

Only fixed provider commands run. The renderer receives an authorization URL,
a prompt direction, and status, never terminal output or stored credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None  # type: ignore[assignment]

from hermes_constants import get_default_hermes_root, get_hermes_home

from .mux_broker import MuxBroker

_OSC_RE = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c)",
    re.DOTALL,
)
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https://[^\s<>\"']+")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_STATUS_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_STATUS_ACCOUNTS = 32
_START_FALLBACK_LOCK = threading.Lock()
_MARKER_NAME = ".hermes-auth-session"
_LOCK_NAME = ".hermes-auth-session.lock"


class CliAuthError(ValueError):
    pass


@dataclass(frozen=True)
class _ProviderSpec:
    binary: str
    session_tag: str
    env_key: str
    login_argv: tuple[str, ...]
    status_argv: tuple[str, ...]
    unset_env: tuple[str, ...]


_SPECS = {
    "claude-code": _ProviderSpec(
        binary="claude",
        session_tag="cc",
        env_key="CLAUDE_CONFIG_DIR",
        login_argv=("auth", "login", "--claudeai"),
        status_argv=("auth", "status", "--json"),
        unset_env=(
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_BEDROCK_BASE_URL",
            "ANTHROPIC_VERTEX_BASE_URL",
            "ANTHROPIC_VERTEX_PROJECT_ID",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "AZURE_TENANT_ID",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH",
            "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        ),
    ),
    "openai-cli": _ProviderSpec(
        binary="codex",
        session_tag="oc",
        env_key="CODEX_HOME",
        login_argv=("login",),
        status_argv=("login", "status"),
        unset_env=("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
    ),
}


def parse_login_view(output: str) -> dict[str, Any]:
    clean = _OSC_RE.sub("", str(output or ""))
    clean = _ANSI_RE.sub("", clean)
    match = _URL_RE.search(clean)
    url = match.group(0).rstrip(".,);]") if match else None
    lower = clean.lower()
    expects_code = any(
        marker in lower
        for marker in (
            "paste authorization code",
            "paste the code",
            "paste code here",
            "enter authorization code",
            "enter the code",
            "code from your browser",
        )
    )
    result: dict[str, Any] = {"auth_url": url, "expects_code": expects_code, "status": "pending"}
    if url is None:
        result.pop("auth_url")
    return result


class CliAuthBroker:
    def __init__(
        self,
        *,
        home: Path | str | None = None,
        shared_home: Path | str | None = None,
        mux: MuxBroker | None = None,
        which: Callable[[str], str | None] = shutil.which,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        nonce: Callable[[], str] = lambda: secrets.token_hex(6),
        platform_name: str | None = None,
    ) -> None:
        explicit_home = home is not None
        self.home = Path(home) if explicit_home else Path(get_hermes_home())
        if shared_home is not None:
            self.shared_home = Path(shared_home)
        elif explicit_home:
            self.shared_home = self._inferred_shared_home(self.home)
        else:
            self.shared_home = Path(get_default_hermes_root())
        self.mux = mux or MuxBroker()
        self._which = which
        self._run = run
        self._nonce = nonce
        self._platform_name = platform_name or os.name

    @staticmethod
    def _spec(provider: str) -> _ProviderSpec:
        spec = _SPECS.get(str(provider or "").strip().lower())
        if spec is None:
            raise CliAuthError("unsupported CLI auth provider")
        return spec

    @staticmethod
    def _account_id(account_id: str) -> str:
        value = str(account_id or "").strip().lower()
        if not _ID_RE.fullmatch(value):
            raise CliAuthError("invalid account id")
        return value

    @staticmethod
    def _inferred_shared_home(home: Path) -> Path:
        if home.parent.name == "profiles":
            return home.parent.parent
        return home

    def _slot_home(self, provider: str, account_id: str) -> Path:
        if self._uses_default_profile(provider, account_id):
            return self.shared_home
        return self.home

    def _slot(self, provider: str, account_id: str) -> Path:
        account = self._account_id(account_id)
        accounts_root = self._slot_home(provider, account) / "accounts"
        if accounts_root.is_symlink():
            raise CliAuthError("accounts directory must not be a symlink")
        accounts_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if accounts_root.is_symlink() or not accounts_root.is_dir():
            raise CliAuthError("invalid accounts directory")
        provider_root = accounts_root / provider
        if provider_root.is_symlink():
            raise CliAuthError("account provider directory must not be a symlink")
        provider_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if provider_root.is_symlink() or not provider_root.is_dir():
            raise CliAuthError("invalid account provider directory")
        slot = provider_root / account
        if slot.is_symlink():
            raise CliAuthError("account slot must not be a symlink")
        slot.mkdir(mode=0o700, exist_ok=True)
        if slot.is_symlink() or not slot.is_dir():
            raise CliAuthError("invalid account slot")
        if os.name == "posix":
            accounts_root.chmod(0o700)
            provider_root.chmod(0o700)
            slot.chmod(0o700)
        return slot

    @staticmethod
    def _uses_default_profile(provider: str, account_id: str) -> bool:
        return provider == "claude-code" and account_id == "default"

    @classmethod
    def _status_value(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text if _STATUS_VALUE_RE.fullmatch(text) else None

    def _command_env(
        self,
        spec: _ProviderSpec,
        provider: str,
        account_id: str,
        slot: Path,
    ) -> dict[str, str]:
        env = dict(os.environ)
        for key in (*spec.unset_env, spec.env_key):
            env.pop(key, None)
        if not self._uses_default_profile(provider, account_id):
            env[spec.env_key] = str(slot)
        return env

    def _redacted_status(
        self,
        provider: str,
        account_id: str,
        result: subprocess.CompletedProcess[str] | None,
    ) -> dict[str, Any]:
        status = {
            "label": account_id,
            "loggedIn": None,
            "authMethod": None,
            "subscriptionType": None,
        }
        if provider != "claude-code" or result is None:
            return status
        try:
            payload = json.loads(result.stdout or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return status
        if not isinstance(payload, dict):
            return status
        logged_in = payload.get("loggedIn")
        if not isinstance(logged_in, bool):
            return status
        if logged_in and result.returncode != 0:
            return status
        status["loggedIn"] = logged_in
        status["authMethod"] = self._status_value(payload.get("authMethod"))
        if logged_in:
            status["subscriptionType"] = self._status_value(
                payload.get("subscriptionType")
            )
        return status

    def status(self, provider: str, account_id: str) -> dict[str, Any]:
        provider_id = str(provider or "").strip().lower()
        spec = self._spec(provider_id)
        account = self._account_id(account_id)
        slot = self._slot(provider_id, account)
        binary = self._which(spec.binary)
        if not binary:
            return self._redacted_status(provider_id, account, None)
        try:
            result = self._run(
                [binary, *spec.status_argv],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                env=self._command_env(spec, provider_id, account, slot),
            )
        except Exception:
            result = None
        return self._redacted_status(provider_id, account, result)

    def list_statuses(self, provider: str) -> list[dict[str, Any]]:
        provider_id = str(provider or "").strip().lower()
        self._spec(provider_id)
        provider_root = self.home / "accounts" / provider_id
        if provider_root.is_symlink():
            raise CliAuthError("account provider directory must not be a symlink")
        labels = ["default"] if provider_id == "claude-code" else []
        if provider_root.is_dir():
            labels.extend(
                path.name
                for path in sorted(provider_root.iterdir(), key=lambda path: path.name)
                if path.name != "default"
                and path.is_dir()
                and not path.is_symlink()
                and _ID_RE.fullmatch(path.name)
            )
        return [self.status(provider_id, label) for label in labels[:_MAX_STATUS_ACCOUNTS]]

    def _session_prefixes(self, provider: str, account_id: str) -> tuple[str, ...]:
        account = self._account_id(account_id)
        spec = self._spec(provider)
        current = f"ha-{spec.session_tag}-{self._home_tag(provider, account)}-{account}-"
        compact_legacy = f"hermes-auth-{spec.session_tag}-{account}-"
        provider_legacy = f"hermes-auth-{provider}-{account}-"
        return tuple(dict.fromkeys((current, compact_legacy, provider_legacy)))

    def _home_tag(
        self,
        provider: str | None = None,
        account_id: str | None = None,
    ) -> str:
        namespace_home = self.home
        if provider is not None and account_id is not None:
            namespace_home = self._slot_home(provider, account_id)
        try:
            home = str(namespace_home.expanduser().resolve(strict=False))
        except Exception:
            home = os.path.abspath(os.path.expanduser(str(namespace_home)))
        return hashlib.sha256(os.fsencode(home)).hexdigest()[:12]

    def _session_prefix(self, provider: str, account_id: str) -> str:
        return self._session_prefixes(provider, account_id)[0]

    def _session_belongs_to_slot(self, provider: str, account_id: str, session_id: str) -> bool:
        value = str(session_id or "")
        return any(
            value.startswith(prefix) and re.fullmatch(r"[0-9a-f]{12}", value[len(prefix) :])
            for prefix in self._session_prefixes(provider, account_id)
        )

    def _session_belongs_to_current_namespace(
        self, provider: str, account_id: str, session_id: str
    ) -> bool:
        value = str(session_id or "")
        prefix = self._session_prefix(provider, account_id)
        return bool(
            value.startswith(prefix)
            and re.fullmatch(r"[0-9a-f]{12}", value[len(prefix) :])
        )

    def _new_session_id(self, provider: str, account_id: str) -> str:
        nonce = str(self._nonce() or "").lower()
        if not re.fullmatch(r"[0-9a-f]{12}", nonce):
            raise CliAuthError("invalid authorization session nonce")
        return f"{self._session_prefix(provider, account_id)}{nonce}"

    def _validate_session(self, provider: str, account_id: str, session_id: str) -> str:
        value = str(session_id or "")
        if not self._session_belongs_to_slot(provider, account_id, value):
            raise CliAuthError("authorization session does not match provider and account")
        return value

    @staticmethod
    def _read_marker(slot: Path) -> tuple[str, int]:
        marker = slot / _MARKER_NAME
        if marker.is_symlink():
            raise CliAuthError("authorization session is not owned by Hermes")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(marker, flags)
        except OSError as exc:
            raise CliAuthError("authorization session is not owned by Hermes") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
                raise CliAuthError("authorization session ownership could not be verified")
            payload = json.loads(os.read(fd, 4097).decode("utf-8"))
            session_id = str(payload.get("session_id") or "")
            pane_pid = int(payload.get("pane_pid") or 0)
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CliAuthError("authorization session ownership could not be verified") from exc
        finally:
            os.close(fd)
        if not session_id or pane_pid <= 0:
            raise CliAuthError("authorization session ownership could not be verified")
        return session_id, pane_pid

    @staticmethod
    def _write_marker(slot: Path, session_id: str, pane_pid: int) -> None:
        marker = slot / _MARKER_NAME
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(marker, flags, 0o600)
        try:
            payload = (
                json.dumps(
                    {"session_id": session_id, "pane_pid": pane_pid},
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])
            os.fsync(fd)
            if os.name == "posix":
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    def _assert_owned_session(
        self, provider: str, account_id: str, session_id: str
    ) -> tuple[str, Path, dict[str, Any] | None]:
        expected = self._validate_session(provider, account_id, session_id)
        slot = self._slot(provider, account_id)
        marker_session, marker_pid = self._read_marker(slot)
        if marker_session != expected:
            raise CliAuthError("authorization session is not owned by Hermes")
        inventory = self.mux.list_sessions()
        if inventory.get("available") is not True:
            raise CliAuthError("authorization session inventory is unavailable")
        live = next((row for row in inventory.get("sessions", []) if row.get("name") == expected), None)
        if live is not None and int(live.get("pane_pid") or 0) != marker_pid:
            raise CliAuthError("authorization session is not owned by Hermes")
        return expected, slot, live

    @staticmethod
    def _clear_marker(slot: Path) -> None:
        try:
            (slot / _MARKER_NAME).unlink(missing_ok=True)
        except OSError:
            pass

    @classmethod
    def _clear_marker_if_owned(cls, slot: Path, session_id: str) -> None:
        try:
            marker_session, _marker_pid = cls._read_marker(slot)
        except CliAuthError:
            return
        if marker_session == session_id:
            cls._clear_marker(slot)

    @staticmethod
    @contextmanager
    def _slot_start_lock(slot: Path) -> Iterator[None]:
        """Serialize login creation for one account across gateway processes."""
        lock_path = slot / _LOCK_NAME
        if lock_path.is_symlink():
            raise CliAuthError("authorization session lock must not be a symlink")
        if fcntl is None and msvcrt is None:  # pragma: no cover - unsupported-platform fallback
            with _START_FALLBACK_LOCK:
                yield
            return

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise CliAuthError("authorization session lock could not be opened") from exc
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)

    def start(self, provider: str, account_id: str) -> dict[str, Any]:
        provider_id = str(provider or "").strip().lower()
        spec = self._spec(provider_id)
        account = self._account_id(account_id)
        if self._platform_name != "posix":
            raise CliAuthError(
                "CLI login sessions are supported only on POSIX gateways"
            )
        slot = self._slot(provider_id, account)
        with self._slot_start_lock(slot):
            return self._start_locked(provider_id, spec, account, slot)

    def _start_locked(
        self,
        provider_id: str,
        spec: _ProviderSpec,
        account: str,
        slot: Path,
    ) -> dict[str, Any]:
        marker = slot / _MARKER_NAME
        inventory = self.mux.list_sessions()
        live_by_name = {str(row.get("name") or ""): row for row in inventory.get("sessions", [])}
        live_for_slot = [
            row
            for name, row in live_by_name.items()
            if (
                self._session_belongs_to_current_namespace(
                    provider_id, account, name
                )
                or (
                    self.home.parent.name != "profiles"
                    and self._session_belongs_to_slot(
                        provider_id, account, name
                    )
                )
            )
        ]
        if marker.is_symlink():
            raise CliAuthError("authorization session marker must not be a symlink")
        if marker.is_file():
            try:
                existing, marker_pid = self._read_marker(slot)
                existing = self._validate_session(provider_id, account, existing)
            except CliAuthError:
                if live_for_slot:
                    raise CliAuthError(
                        "existing authorization session is not owned by Hermes"
                    )
                self._clear_marker(slot)
            else:
                live = live_by_name.get(existing)
                relevant_live_names = {
                    str(row.get("name") or "") for row in live_for_slot
                }
                if live is not None:
                    relevant_live_names.add(existing)
                if live is None and not relevant_live_names:
                    # The owned pane finished while the renderer was away.
                    # With no live writer in this namespace and the slot lock
                    # held, retiring its marker is safe and restores login.
                    self._clear_marker_if_owned(slot, existing)
                elif (
                    live is not None
                    and int(live.get("pane_pid") or 0) == marker_pid
                    and len(relevant_live_names) == 1
                ):
                    return {
                        "account_id": account,
                        "provider": provider_id,
                        "session_id": existing,
                        "status": "pending",
                    }
                else:
                    raise CliAuthError(
                        "existing authorization session is not owned by Hermes"
                    )
        elif live_for_slot:
            raise CliAuthError(
                "existing authorization session is not owned by Hermes"
            )

        session_id = self._new_session_id(provider_id, account)
        if session_id in live_by_name:
            raise CliAuthError("existing authorization session is not owned by Hermes")
        binary = self._which(spec.binary)
        if not binary:
            raise CliAuthError(f"{spec.binary} CLI is not installed on this gateway")
        unset_args = [part for key in (*spec.unset_env, spec.env_key) for part in ("-u", key)]
        profile_arg = [] if self._uses_default_profile(provider_id, account) else [f"{spec.env_key}={slot}"]
        command = ["env", *unset_args, *profile_arg, binary, *spec.login_argv]
        self.mux.create(session_id, cwd=str(slot), command=command)
        try:
            created_inventory = self.mux.list_sessions()
            created = next(
                (row for row in created_inventory.get("sessions", []) if row.get("name") == session_id),
                None,
            )
            pane_pid = int(created.get("pane_pid") or 0) if created else 0
            if pane_pid <= 0:
                raise OSError("mux did not report a pane pid")
            self._write_marker(slot, session_id, pane_pid)
        except Exception as exc:
            try:
                self.mux.close(session_id)
            except Exception:
                pass
            raise CliAuthError("authorization session ownership marker could not be written") from exc
        return {
            "account_id": account,
            "provider": provider_id,
            "session_id": session_id,
            "status": "pending",
        }

    def poll(self, provider: str, account_id: str, session_id: str) -> dict[str, Any]:
        provider_id = str(provider or "").strip().lower()
        spec = self._spec(provider_id)
        account = self._account_id(account_id)
        slot = self._slot(provider_id, account)
        with self._slot_start_lock(slot):
            expected, slot, live = self._assert_owned_session(
                provider_id, account, session_id
            )
            if live is not None:
                captured = self.mux.capture(expected, lines=300)
                return parse_login_view(str(captured.get("ansi") or ""))

            binary = self._which(spec.binary)
            if not binary:
                self._clear_marker_if_owned(slot, expected)
                return {"status": "error"}
            try:
                result = self._run(
                    [binary, *spec.status_argv],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                    env=self._command_env(spec, provider_id, account, slot),
                )
            except Exception:
                self._clear_marker_if_owned(slot, expected)
                return {"status": "error"}
            if provider_id == "claude-code":
                redacted = self._redacted_status(provider_id, account, result)
                self._clear_marker_if_owned(slot, expected)
                return {
                    "status": (
                        "approved" if redacted["loggedIn"] is True else "error"
                    ),
                    **redacted,
                }

            output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
            denied_markers = (
                '"loggedin": false',
                '"loggedin":false',
                "not logged",
                "logged out",
                "not authenticated",
                "please run",
                "please log in",
                "login required",
                "no credentials",
            )
            approved_markers = (
                '"loggedin": true',
                '"loggedin":true',
                "logged in",
            )
            approved = (
                result.returncode == 0
                and not any(marker in output for marker in denied_markers)
                and any(marker in output for marker in approved_markers)
            )
            self._clear_marker_if_owned(slot, expected)
            return {"status": "approved" if approved else "error"}

    def submit(
        self,
        provider: str,
        account_id: str,
        session_id: str,
        code: str,
    ) -> dict[str, Any]:
        provider_id = str(provider or "").strip().lower()
        self._spec(provider_id)
        expected, _slot, live = self._assert_owned_session(provider_id, account_id, session_id)
        if live is None:
            raise CliAuthError("authorization session is no longer active")
        value = str(code or "").strip()
        if not value or len(value) > 4096 or "\n" in value or "\r" in value or "\x00" in value:
            raise CliAuthError("invalid authorization code")
        self.mux.send_input(expected, text=value)
        self.mux.send_input(expected, key="Enter")
        return {"status": "pending"}

    def cancel(self, provider: str, account_id: str, session_id: str) -> dict[str, Any]:
        provider_id = str(provider or "").strip().lower()
        self._spec(provider_id)
        account = self._account_id(account_id)
        slot = self._slot(provider_id, account)
        with self._slot_start_lock(slot):
            expected, slot, live = self._assert_owned_session(
                provider_id, account, session_id
            )
            if live is not None:
                self.mux.close(expected)
            self._clear_marker_if_owned(slot, expected)
        return {"status": "cancelled"}
