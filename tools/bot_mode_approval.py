"""Cross-process approval bridge for Bot Mode query-file deliveries.

Mailbox modes are a POSIX privacy boundary. On Windows, ``chmod`` only
controls the read-only bit, so records follow Hermes' usual same-user threat
model: other processes running as that user can read or write them.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from hermes_constants import get_hermes_home

_BRIDGE_ENV = "HERMES_BOT_MODE_QUERY_FILE"
_ALLOWED_CHOICES = {"once", "session", "always", "deny"}


def bridge_enabled() -> bool:
    return os.getenv(_BRIDGE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def install_bot_mode_approval_callback(cli: Any, *, timeout: float) -> bool:
    """Install the durable callback only in marked Bot Mode query processes."""
    if not bridge_enabled() or not str(getattr(cli, "session_id", "") or ""):
        return False

    from tools.terminal_tool import set_approval_callback

    def callback(
        command: str,
        description: str,
        *,
        allow_permanent: bool = True,
        allow_session: bool = True,
        smart_denied: bool = False,
    ) -> str:
        choices = cli._approval_choices(
            command,
            allow_permanent=allow_permanent,
            allow_session=allow_session,
            smart_denied=smart_denied,
        )
        return request_bot_mode_approval(
            session_key=str(cli.session_id),
            command=command,
            description=description,
            choices=choices,
            timeout=timeout,
        )

    set_approval_callback(callback)
    return True


def _mailbox_dir(home: Optional[Path] = None, *, create: bool = False) -> Path:
    root = Path(home) if home is not None else get_hermes_home()
    directory = root / "runtime" / "bot-mode-approvals"
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
    return directory


def _write_record(path: Path, record: dict) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".approval-", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _read_record(path: Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


@contextmanager
def _resolution_lock(path: Path) -> Iterator[bool]:
    """Atomically elect one resolver for ``path`` across local processes.

    A crashed resolver leaves the lock behind and the request times out denied,
    which is safer than allowing a second choice to overwrite the first.
    """
    lock_path = path.with_name(f"{path.name}.lock")

    try:
        lock_path.mkdir(mode=0o700)
    except FileExistsError:
        yield False
        return

    try:
        yield True
    finally:
        try:
            lock_path.rmdir()
        except OSError:
            pass


def _timestamp(value: object) -> float:
    if not isinstance(value, (int, float, str)):
        return 0.0
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _valid_request_id(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 32:
        return False
    return all(char in "0123456789abcdef" for char in value)


def request_bot_mode_approval(
    *,
    session_key: str,
    command: str,
    description: str,
    choices: list[str],
    timeout: float,
) -> str:
    """Publish one request and block until the profile gateway answers or times out."""
    offered = [choice for choice in choices if choice in _ALLOWED_CHOICES]
    if not session_key or not offered:
        return "deny"

    request_id = uuid.uuid4().hex
    now = time.time()
    timeout = max(0.0, float(timeout))
    record = {
        "request_id": request_id,
        "session_key": session_key,
        "command": command,
        "description": description,
        "choices": offered,
        "created_at": now,
        "expires_at": now + timeout,
        "status": "pending",
    }
    path = _mailbox_dir(create=True) / f"{request_id}.json"
    _write_record(path, record)

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            current = _read_record(path)
            if current is None:
                return "deny"
            if current.get("status") == "resolved":
                choice = current.get("choice")
                return choice if choice in offered else "deny"
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return "timeout"
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def get_pending_bot_mode_approval(
    session_key: str, *, home: Optional[Path] = None
) -> Optional[dict]:
    """Return the oldest non-expired pending request for a durable session."""
    directory = _mailbox_dir(home)
    if not directory.is_dir() or not session_key:
        return None

    now = time.time()
    pending: list[dict] = []
    for path in directory.glob("*.json"):
        record = _read_record(path)
        if record is None:
            continue
        expires_at = _timestamp(record.get("expires_at"))
        if expires_at <= now:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if record.get("status") != "pending" or record.get("session_key") != session_key:
            continue
        request_id = record.get("request_id")
        choices = record.get("choices")
        if not _valid_request_id(request_id) or not isinstance(choices, list):
            continue
        if not choices or any(choice not in _ALLOWED_CHOICES for choice in choices):
            continue
        pending.append(record)

    pending.sort(
        key=lambda item: (
            _timestamp(item.get("created_at")),
            str(item.get("request_id") or ""),
        )
    )
    return pending[0] if pending else None


def resolve_bot_mode_approval(
    session_key: str,
    choice: str,
    *,
    request_id: str,
    home: Optional[Path] = None,
) -> bool:
    """Resolve one request, validating identity and the originally offered choice."""
    if (
        not session_key
        or not _valid_request_id(request_id)
        or choice not in _ALLOWED_CHOICES
    ):
        return False
    path = _mailbox_dir(home) / f"{request_id}.json"

    with _resolution_lock(path) as won:
        if not won:
            return False

        record = _read_record(path)
        if record is None or record.get("status") != "pending":
            return False
        if record.get("session_key") != session_key:
            return False
        choices = record.get("choices")
        if not isinstance(choices, list) or choice not in choices:
            return False
        if _timestamp(record.get("expires_at")) <= time.time():
            return False

        updated = dict(record)
        updated["status"] = "resolved"
        updated["choice"] = choice
        updated["resolved_at"] = time.time()
        _write_record(path, updated)
        return True
