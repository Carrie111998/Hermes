"""Submit prompts through the running Hermes Desktop renderer.

The command spools a bounded request into Electron's user-data directory and
opens a ``hermes://chat-z/<id>`` deep link.  The primary Desktop renderer uses
its existing gateway connection and returns a receipt as soon as submission is
accepted; the agent response is never awaited.

This is intentionally a same-OS-user channel, not a remote authentication
boundary.  UUIDs correlate files and links rather than authorize callers; any
process already running as the Desktop user is inside the trust boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


CHAT_Z_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_PROMPT_CHARS = 1_000_000
MAX_REQUEST_BYTES = 1_100_000


def desktop_user_data_dir() -> Path:
    override = os.environ.get("HERMES_DESKTOP_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # Electron sets the packaged app name to "Hermes" before resolving
    # app.getPath("userData"), whose platform defaults match these locations.
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        return (
            Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        ) / "Hermes"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Hermes"
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(config_home) if config_home else Path.home() / ".config") / "Hermes"


def _spool_paths(user_data: Path, request_id: str) -> tuple[Path, Path]:
    root = user_data / "chat-z"
    return (
        root / "requests" / f"{request_id}.json",
        root / "receipts" / f"{request_id}.json",
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError(
            f"serialized request exceeds the {MAX_REQUEST_BYTES:,}-byte limit"
        )

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_prompt(args) -> str:
    if args.query is not None:
        text = args.query
    elif args.query_file:
        try:
            text = Path(args.query_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read --query-file: {exc}") from exc
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        raise ValueError(
            "provide -q/--query, --query-file, or pipe prompt text on stdin"
        )

    text = text.strip()
    if not text:
        raise ValueError("prompt is empty")
    if len(text) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt exceeds the {MAX_PROMPT_CHARS:,}-character limit")
    return text


def _launch_deep_link(uri: str) -> None:
    if sys.platform == "win32":
        os.startfile(uri)  # type: ignore[attr-defined]
        return
    command = ["open", uri] if sys.platform == "darwin" else ["xdg-open", uri]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_for_receipt(path: Path, timeout_seconds: float) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            time.sleep(0.05)
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
    raise TimeoutError(
        f"Hermes Desktop did not acknowledge the request within {timeout_seconds:g}s. "
        "The running Desktop may not include the chat-z bridge or may not have finished loading; "
        "this timeout occurs before target-session lookup and is not caused by the session source."
    )


def send_to_desktop(
    args, *, launch=_launch_deep_link, user_data: Path | None = None
) -> dict:
    text = _read_prompt(args)
    title = (getattr(args, "conversation", None) or "").strip()
    session_id = (getattr(args, "session_id", None) or "").strip()
    new_session = bool(getattr(args, "new_session", False))
    new_title = (getattr(args, "title", None) or "").strip()
    if sum((bool(title), bool(session_id), new_session)) != 1:
        raise ValueError(
            "choose exactly one target: -c/--conversation, --session-id, or --new"
        )

    cwd_input = (getattr(args, "cwd", None) or "").strip()
    cwd = ""
    if new_session:
        if not cwd_input:
            raise ValueError("--new requires --cwd with an existing project directory")
        try:
            cwd_path = Path(cwd_input).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"cannot resolve --cwd: {exc}") from exc
        if not cwd_path.is_dir():
            raise ValueError("--cwd must identify an existing directory")
        cwd = str(cwd_path)
    elif cwd_input:
        raise ValueError("--cwd can only be used with --new")

    if new_title and not new_session:
        raise ValueError("--title can only be used with --new")
    if len(new_title) > 500:
        raise ValueError("--title must be at most 500 characters")

    timeout_seconds = float(getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS))
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise ValueError("--timeout must be greater than 0 and at most 300 seconds")

    try:
        from hermes_cli.profiles import get_active_profile_name

        profile = get_active_profile_name()
    except Exception:
        profile = "default"

    request_id = str(uuid.uuid4())
    request_path, receipt_path = _spool_paths(
        user_data or desktop_user_data_dir(), request_id
    )
    now_ms = int(time.time() * 1000)
    request = {
        "version": CHAT_Z_VERSION,
        "requestId": request_id,
        "profile": profile,
        "text": text,
        "createdAt": now_ms,
        "expiresAt": now_ms + int(timeout_seconds * 1000),
        **(
            {
                "newSession": True,
                "cwd": cwd,
                **({"newTitle": new_title} if new_title else {}),
            }
            if new_session
            else ({"title": title} if title else {"sessionId": session_id})
        ),
    }

    _atomic_write_json(request_path, request)
    try:
        launch(f"hermes://chat-z/{request_id}")
        receipt = _wait_for_receipt(receipt_path, timeout_seconds)
        if receipt.get("requestId") != request_id:
            raise RuntimeError("Desktop returned a receipt for a different request")
        return receipt
    finally:
        for path in (request_path, receipt_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def cmd_chat_z(args) -> int:
    try:
        receipt = send_to_desktop(args)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"chat-z: {exc}", file=sys.stderr)
        return 1

    if receipt.get("status") != "accepted":
        message = (
            receipt.get("message")
            or receipt.get("code")
            or "Desktop rejected the request"
        )
        print(f"chat-z: {message}", file=sys.stderr)
        return 1

    if not getattr(args, "quiet", False):
        target = receipt.get("title") or receipt.get("storedSessionId") or "session"
        if receipt.get("created"):
            workspace = f" in {receipt['cwd']}" if receipt.get("cwd") else ""
            stored_session_id = receipt.get("storedSessionId")
            created_target = (
                f"{target} (ID: {stored_session_id})"
                if receipt.get("title") and stored_session_id
                else target
            )
            print(f"Created by Hermes Desktop: {created_target}{workspace}")
        else:
            print(f"Accepted by Hermes Desktop: {target}")
    return 0


def build_chat_z_parser(subparsers):
    parser = subparsers.add_parser(
        "chat-z",
        help="Send a prompt through the running Hermes Desktop session",
        description=(
            "Submit through Hermes Desktop, targeting an existing conversation "
            "or creating a new one in a project directory. Return as soon as "
            "Desktop accepts the prompt; the agent response is not awaited."
        ),
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("-c", "--conversation", help="Exact Desktop conversation title")
    target.add_argument("--session-id", help="Stored Desktop session ID")
    target.add_argument(
        "--new",
        dest="new_session",
        action="store_true",
        help="Create a new Desktop conversation",
    )
    parser.add_argument("--cwd", help="Existing project directory for --new")
    parser.add_argument("--title", help="Fixed session title for --new")
    prompt = parser.add_mutually_exclusive_group()
    prompt.add_argument("-q", "--query", help="Prompt text")
    prompt.add_argument("--query-file", help="UTF-8 file containing the prompt")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Seconds to wait for Desktop acceptance (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "-Q", "--quiet", action="store_true", help="Print nothing when accepted"
    )
    parser.set_defaults(func=cmd_chat_z)
    return parser
