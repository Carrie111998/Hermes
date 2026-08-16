"""``hermes remote`` pairing and connection management.

The parser builder follows the other extracted subcommands: ``main`` injects
the handler, while this module owns the small HTTP/config workflow so it can be
tested without importing the CLI god-file.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable


DEFAULT_REMOTE_PORT = 8642
DEFAULT_PAIR_BASE_URL = f"http://127.0.0.1:{DEFAULT_REMOTE_PORT}"
REMOTE_REQUEST_TIMEOUT = 10.0


class RemoteHTTPError(RuntimeError):
    """An HTTP error returned by a remote Hermes API server."""

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail or f"HTTP {status}")


class RemoteConnectionError(RuntimeError):
    """The remote Hermes API server could not be reached."""


class RemoteTimeoutError(RemoteConnectionError):
    """The remote Hermes API request timed out."""


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def build_remote_parser(subparsers, *, cmd_remote: Callable) -> None:
    """Attach the ``remote`` subcommand to ``subparsers``."""
    remote_parser = subparsers.add_parser(
        "remote",
        help="Pair with and attach to a remote Hermes host",
        description="Manage remote Hermes session connections",
    )
    remote_sub = remote_parser.add_subparsers(dest="remote_action")

    pair_parser = remote_sub.add_parser(
        "pair", help="Generate a pairing code on this host"
    )
    pair_parser.add_argument(
        "--base-url",
        default=DEFAULT_PAIR_BASE_URL,
        help=f"Local API server URL (default {DEFAULT_PAIR_BASE_URL})",
    )
    pair_parser.add_argument(
        "--port",
        type=_port,
        help=f"Override the local API server port (default {DEFAULT_REMOTE_PORT})",
    )

    attach_parser = remote_sub.add_parser(
        "attach", help="Pair with a remote Hermes host"
    )
    attach_parser.add_argument("host", metavar="host[:port]", help="Remote host")
    attach_parser.add_argument(
        "--code", help="Six-character pairing code supplied by the host"
    )
    attach_parser.add_argument(
        "--name", help="Name used to save this connection (default: host name)"
    )

    sessions_parser = remote_sub.add_parser(
        "sessions", help="List sessions on a saved remote connection"
    )
    sessions_parser.add_argument(
        "connection_name",
        nargs="?",
        metavar="name",
        help="Saved connection name (default: most recently saved)",
    )
    sessions_parser.add_argument(
        "--name",
        dest="selected_name",
        help="Saved connection name (alternative to the positional name)",
    )

    remote_parser.set_defaults(func=cmd_remote)


def _error_detail(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")
    return str(error or payload.get("detail") or payload.get("message") or "")


def _request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = REMOTE_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """Make one credential-safe JSON request to a Hermes API server."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        from hermes_cli.urllib_security import open_credentialed_url

        with open_credentialed_url(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = _error_detail(exc.read())
        except OSError:
            detail = ""
        raise RemoteHTTPError(exc.code, detail) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise RemoteTimeoutError(str(exc) or "request timed out") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise RemoteTimeoutError(str(exc.reason) or "request timed out") from exc
        raise RemoteConnectionError(str(exc.reason)) from exc
    except (ConnectionError, OSError) as exc:
        raise RemoteConnectionError(str(exc)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteHTTPError(0, "Server returned invalid JSON") from exc

    if not isinstance(result, dict):
        raise RemoteHTTPError(0, "Server returned an unexpected response")
    return result


def _resolve_api_server_key() -> str:
    """Resolve the key through the API-server adapter's canonical config path."""
    env_key = os.environ.get("API_SERVER_KEY", "").strip()
    if env_key:
        return env_key

    try:
        from gateway.config import Platform, load_gateway_config

        platform = load_gateway_config().platforms.get(Platform.API_SERVER)
        if platform is not None:
            return str((platform.extra or {}).get("key") or "").strip()
    except Exception:
        pass
    return ""


def _normalized_base_url(raw: str, *, port: int | None = None) -> str:
    value = (raw or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base URL must start with http:// or https://")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain credentials, a query, or a fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("base URL must not contain a path")
    try:
        selected_port = port if port is not None else parsed.port
    except ValueError as exc:
        raise ValueError("base URL contains an invalid port") from exc
    host = parsed.hostname
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host + (f":{selected_port}" if selected_port is not None else "")
    return urllib.parse.urlunsplit((parsed.scheme, netloc, "", "", ""))


def _parse_remote_host(raw: str) -> tuple[str, int, str]:
    target = (raw or "").strip()
    if not target or "://" in target:
        raise ValueError("host must be in host[:port] form")
    try:
        parsed = urllib.parse.urlsplit(f"//{target}")
        host = parsed.hostname or ""
        port = parsed.port or DEFAULT_REMOTE_PORT
    except ValueError as exc:
        raise ValueError("host contains an invalid port") from exc
    if not host or parsed.username or parsed.password or parsed.path:
        raise ValueError("host must be in host[:port] form")
    rendered_host = f"[{host}]" if ":" in host else host
    return host, port, f"http://{rendered_host}:{port}"


def _connection_name(host: str, override: str | None) -> str:
    if override is not None:
        name = override.strip()
        if not name:
            raise ValueError("connection name cannot be empty")
        return name
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", host).strip("-")
    return name or "remote-host"


def _save_connection(
    *, name: str, host: str, port: int, token: str, expires_at: str
) -> None:
    from hermes_cli.config import load_config, save_config

    config = load_config()
    remote = config.setdefault("remote", {})
    if not isinstance(remote, dict):
        remote = {}
        config["remote"] = remote
    connections = remote.setdefault("connections", {})
    if not isinstance(connections, dict):
        connections = {}
        remote["connections"] = connections
    connections[name] = {
        "host": host,
        "port": port,
        "token": token,
        "expires_at": expires_at,
    }
    save_config(config)


def _saved_connections() -> dict[str, dict[str, Any]]:
    """Return well-shaped saved remote connections from config.yaml."""
    from hermes_cli.config import load_config

    remote = load_config().get("remote")
    if not isinstance(remote, dict):
        return {}
    connections = remote.get("connections")
    if not isinstance(connections, dict):
        return {}
    return {
        str(name): connection
        for name, connection in connections.items()
        if isinstance(connection, dict)
    }


def _format_updated_at(value: Any) -> str:
    """Render the server's RFC3339 timestamp compactly for a text table."""
    raw = str(value or "").strip()
    if not raw:
        return "-"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed.strftime("%Y-%m-%d %H:%M")


def _remote_target(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    return rendered_host if port == DEFAULT_REMOTE_PORT else f"{rendered_host}:{port}"


def _print_sessions_table(result: dict[str, Any], *, fallback_host: str) -> None:
    hostname = str(result.get("hostname") or fallback_host)
    profile = str(result.get("profile") or "default")
    print(f"Remote host: {hostname} (profile: {profile})")

    sessions = result.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        print("No open sessions.")
        return

    print()
    print(f"  {'Session ID':<12}  {'Title':<40}  {'Status':<8}  Updated")
    print(f"  {'----------':<12}  {'-----':<40}  {'------':<8}  -------")
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("id") or "-")[:12]
        title = " ".join(str(session.get("title") or "(untitled)").split())[:40]
        status = str(session.get("status") or "-")
        updated = _format_updated_at(session.get("updated_at"))
        print(f"  {session_id:<12}  {title:<40}  {status:<8}  {updated}")


def _print_request_error(exc: Exception, *, base_url: str, pairing: bool) -> int:
    if isinstance(exc, RemoteHTTPError):
        if exc.status == 401 and pairing:
            print(
                "Pairing code invalid or expired, ask the host for a new one",
                file=sys.stderr,
            )
        else:
            detail = f": {exc.detail}" if exc.detail else ""
            print(f"Remote host returned HTTP {exc.status}{detail}", file=sys.stderr)
    elif isinstance(exc, RemoteTimeoutError):
        print(f"Timed out connecting to host at {base_url}", file=sys.stderr)
    else:
        print(f"Cannot reach host at {base_url}", file=sys.stderr)
    return 1


def _pair(args) -> int:
    try:
        base_url = _normalized_base_url(args.base_url, port=args.port)
    except ValueError as exc:
        print(f"Invalid local API server URL: {exc}", file=sys.stderr)
        return 1

    api_key = _resolve_api_server_key()
    if not api_key:
        print(
            "API_SERVER_KEY is not configured. Configure gateway.api_server.key "
            "in config.yaml or API_SERVER_KEY in the Hermes environment.",
            file=sys.stderr,
        )
        return 1

    try:
        result = _request_json(
            "POST", f"{base_url}/api/remote/pair/code", token=api_key
        )
    except RemoteHTTPError as exc:
        if exc.status == 401:
            print("Local API server rejected API_SERVER_KEY.", file=sys.stderr)
        else:
            detail = f": {exc.detail}" if exc.detail else ""
            print(f"Local API server returned HTTP {exc.status}{detail}", file=sys.stderr)
        return 1
    except RemoteTimeoutError:
        print(f"Timed out connecting to the local API server at {base_url}", file=sys.stderr)
        return 1
    except RemoteConnectionError:
        print(f"Cannot reach the local API server at {base_url}", file=sys.stderr)
        return 1

    code = str(result.get("code") or "").strip()
    expires_at = str(result.get("expires_at") or "").strip()
    if not code or not expires_at:
        print("Local API server returned an unexpected pairing response.", file=sys.stderr)
        return 1
    ttl = result.get("ttl_minutes", 10)
    print(f"Pairing code: {code}")
    print(f"Expires: {expires_at} ({ttl} minutes)")
    print("Share this one-time code with the remote client.")
    return 0


def _attach(args) -> int:
    try:
        host, port, base_url = _parse_remote_host(args.host)
        name = _connection_name(host, args.name)
    except ValueError as exc:
        print(f"Invalid remote host: {exc}", file=sys.stderr)
        return 1

    raw_code = args.code
    if raw_code is None:
        print("Ask the host to run `hermes remote pair` and give you the code:")
        try:
            raw_code = input("Pairing code: ")
        except (EOFError, KeyboardInterrupt):
            print("\nPairing cancelled.", file=sys.stderr)
            return 1
    code = str(raw_code or "").strip().upper()
    if not code:
        print("A pairing code is required.", file=sys.stderr)
        return 1

    try:
        paired = _request_json(
            "POST", f"{base_url}/api/remote/pair", payload={"code": code}
        )
    except (RemoteHTTPError, RemoteConnectionError) as exc:
        return _print_request_error(exc, base_url=base_url, pairing=True)

    token = str(paired.get("token") or "").strip()
    expires_at = str(paired.get("expires_at") or "").strip()
    if not token or not expires_at:
        print("Remote host returned an unexpected pairing response.", file=sys.stderr)
        return 1

    # Pairing codes are single-use. Persist the issued token before probing so
    # a transient failure during the connection test does not discard it.
    _save_connection(
        name=name,
        host=host,
        port=port,
        token=token,
        expires_at=expires_at,
    )

    try:
        status = _request_json(
            "GET", f"{base_url}/api/remote/sessions", token=token
        )
    except (RemoteHTTPError, RemoteConnectionError) as exc:
        return _print_request_error(exc, base_url=base_url, pairing=False)

    hostname = str(status.get("hostname") or host)
    profile = str(status.get("profile") or "default")
    sessions = status.get("sessions")
    session_count = len(sessions) if isinstance(sessions, list) else 0
    noun = "session" if session_count == 1 else "sessions"
    print(
        f"Connected to {hostname} (profile: {profile}, "
        f"{session_count} {noun}). Saved as {name!r}."
    )
    return 0


def _sessions(args) -> int:
    connections = _saved_connections()
    if not connections:
        print(
            "No saved remote connection. Run `hermes remote attach <host>` first.",
            file=sys.stderr,
        )
        return 1

    selected_name = getattr(args, "selected_name", None) or getattr(
        args, "connection_name", None
    )
    if selected_name:
        connection = connections.get(selected_name)
        if connection is None:
            print(
                f"Saved remote connection {selected_name!r} was not found.",
                file=sys.stderr,
            )
            return 1
    else:
        selected_name, connection = next(reversed(connections.items()))

    host = str(connection.get("host") or "").strip()
    token = str(connection.get("token") or "").strip()
    try:
        port = _port(str(connection.get("port", DEFAULT_REMOTE_PORT)))
    except ValueError:
        port = 0
    if not host or not token or not port:
        print(
            f"Saved remote connection {selected_name!r} is invalid. "
            "Run `hermes remote attach <host>` again.",
            file=sys.stderr,
        )
        return 1

    target = _remote_target(host, port)
    rendered_host = f"[{host}]" if ":" in host else host
    base_url = f"http://{rendered_host}:{port}"
    try:
        result = _request_json(
            "GET", f"{base_url}/api/remote/sessions", token=token
        )
    except RemoteHTTPError as exc:
        if exc.status == 401:
            print(
                "Attach token expired or invalid — run "
                f"`hermes remote attach {target} --code ...` again",
                file=sys.stderr,
            )
            return 1
        return _print_request_error(exc, base_url=base_url, pairing=False)
    except RemoteConnectionError as exc:
        return _print_request_error(exc, base_url=base_url, pairing=False)

    _print_sessions_table(result, fallback_host=host)
    return 0


def remote_command(args) -> int:
    """Dispatch a ``hermes remote`` action."""
    action = getattr(args, "remote_action", None)
    if action == "pair":
        return _pair(args)
    if action == "attach":
        return _attach(args)
    if action == "sessions":
        return _sessions(args)
    print("Choose a remote action: pair, attach, or sessions.", file=sys.stderr)
    return 2


__all__ = [
    "RemoteConnectionError",
    "RemoteHTTPError",
    "RemoteTimeoutError",
    "build_remote_parser",
    "remote_command",
]
