"""Pre-model /senv (secure env) command — store secrets without LLM exposure.

The value never appears in the returned text. Callers must not log the raw
argument string; use :func:`redact_senv_args` for hooks and telemetry.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home
from utils import atomic_replace

logger = logging.getLogger(__name__)

SENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SENV_SKILL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

USAGE = (
    "Usage:\n"
    "  /senv [main] KEY=VALUE\n"
    "  /senv skill <name> KEY=VALUE\n"
    "  /senv delete [main] KEY\n"
    "  /senv delete skill <name> KEY\n"
    "  /senv list [main]\n"
    "  /senv list skill <name>"
)

DELETE_USER_MESSAGE_HINT = (
    "Secret saved. Please delete your original message containing the private value."
)

_REDACTED = "[redacted]"


@dataclass(frozen=True)
class SenvResult:
    """Value-free command result."""

    text: str
    ok: bool = True
    action: str = ""
    key: str = ""
    scope: str = ""
    # Never populate this on a public result. Tests may assert it is empty.
    leaked_secret: str = ""


def redact_senv_args(args: str) -> str:
    """Replace assignment values so hooks/logs never see the secret."""
    if not args:
        return ""
    if "\n" in args or "\r" in args:
        return _REDACTED
    if "=" not in args:
        return args.strip()
    prefix, _sep, _rest = args.partition("=")
    return f"{prefix}={_REDACTED}"


def redact_senv_hook_args(command: str, args_raw: str) -> str:
    """Redact /senv hook payloads; leave other commands unchanged."""
    if str(command or "").strip().lower() not in {"senv", "secure-env", "secure_env"}:
        return args_raw
    return redact_senv_args(args_raw)


def _unquote_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        if value[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value


def _parse_assignment(token: str) -> Optional[tuple[str, str]]:
    if "=" not in token:
        return None
    key, _sep, raw_value = token.partition("=")
    key = key.strip()
    if not SENV_KEY_RE.match(key):
        return None
    return key, _unquote_value(raw_value)


def parse_senv_args(args: str) -> dict:
    """Parse /senv arguments. Returns an action dict or ``{"error": ...}``."""
    if args is None:
        return {"error": USAGE}
    if "\n" in args or "\r" in args:
        return {"error": "Multi-line values are not allowed."}

    parts = args.strip().split()
    if not parts:
        return {"error": USAGE}

    def _scope_and_rest(tokens: list[str]) -> tuple[str, str, list[str]] | dict:
        if not tokens:
            return {"error": USAGE}
        if tokens[0].lower() == "skill":
            if len(tokens) < 2:
                return {"error": "Usage: /senv skill <name> ..."}
            name = tokens[1]
            if not SENV_SKILL_RE.match(name) or ".." in name or "/" in name or "\\" in name:
                return {"error": "Invalid skill name."}
            return "skill", name, tokens[2:]
        if tokens[0].lower() == "main":
            return "main", "main", tokens[1:]
        return "main", "main", tokens

    head = parts[0].lower()
    if head == "list":
        scoped = _scope_and_rest(parts[1:])
        if isinstance(scoped, dict):
            if parts[1:] == []:
                return {"action": "list", "scope": "main", "skill": ""}
            return scoped
        scope, skill, rest = scoped
        if rest:
            return {"error": USAGE}
        return {"action": "list", "scope": scope, "skill": skill if scope == "skill" else ""}

    if head == "delete":
        scoped = _scope_and_rest(parts[1:])
        if isinstance(scoped, dict):
            return scoped
        scope, skill, rest = scoped
        if len(rest) != 1:
            return {"error": "Usage: /senv delete [main|skill <name>] KEY"}
        key = rest[0].strip()
        if not SENV_KEY_RE.match(key):
            return {"error": "Invalid key name. Use A-Z, digits, and underscore (e.g. BOOKING_PASSWORD)."}
        return {
            "action": "delete",
            "scope": scope,
            "skill": skill if scope == "skill" else "",
            "key": key,
        }

    scoped = _scope_and_rest(parts)
    if isinstance(scoped, dict):
        return scoped
    scope, skill, rest = scoped
    if not rest:
        return {"error": USAGE}
    assignment = " ".join(rest)
    parsed = _parse_assignment(assignment)
    if parsed is None:
        key_part = assignment.split("=", 1)[0].strip() if "=" in assignment else assignment
        if "=" not in assignment:
            return {"error": USAGE}
        if not SENV_KEY_RE.match(key_part):
            return {"error": "Invalid key name. Use A-Z, digits, and underscore (e.g. BOOKING_PASSWORD)."}
        return {"error": USAGE}
    key, value = parsed
    if "\n" in value or "\r" in value:
        return {"error": "Multi-line values are not allowed."}
    return {
        "action": "set",
        "scope": scope,
        "skill": skill if scope == "skill" else "",
        "key": key,
        "value": value,
    }


def _skill_env_path(skill: str) -> Path | dict:
    skills_root = (get_hermes_home() / "skills").resolve()
    skill_dir = (skills_root / skill).resolve()
    try:
        skill_dir.relative_to(skills_root)
    except ValueError:
        return {"error": "Invalid skill name."}
    if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
        return {"error": f"Skill {skill} was not found in the active profile."}
    return skill_dir / ".env"


def _list_keys(path: Path) -> list[str]:
    if not path.is_file():
        return []
    keys: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:]
        key = stripped.split("=", 1)[0].strip()
        if key:
            keys.append(key)
    return keys


def _upsert_env_file(path: Path, key: str, value: str) -> None:
    from hermes_cli.config import (
        _ENV_VAR_NAME_RE,
        _check_non_ascii_credential,
        _env_line_defines_key,
        _quote_env_value,
        _reject_denylisted_env_var,
        _sanitize_env_lines,
        _secure_file,
    )

    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    _reject_denylisted_env_var(key)
    value = value.replace("\n", "").replace("\r", "")
    value = _check_non_ascii_credential(key, value)

    path.parent.mkdir(parents=True, exist_ok=True)
    read_kw = {"encoding": "utf-8-sig", "errors": "replace"}
    write_kw = {"encoding": "utf-8"}
    lines: list[str] = []
    if path.exists():
        with open(path, **read_kw) as handle:
            lines = handle.readlines()
        lines = _sanitize_env_lines(lines)

    serialized = _quote_env_value(value)
    found = False
    for index, line in enumerate(lines):
        if _env_line_defines_key(line, key):
            lines[index] = f"{key}={serialized}\n"
            found = True
            break
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={serialized}\n")

    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".env_")
    original_mode = None
    if path.exists():
        try:
            original_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            original_mode = None
    try:
        with os.fdopen(fd, "w", **write_kw) as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, path)
        if original_mode is not None:
            try:
                os.chmod(path, original_mode)
            except OSError:
                pass
        else:
            _secure_file(path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _remove_env_file_key(path: Path, key: str) -> bool:
    from hermes_cli.config import (
        _ENV_VAR_NAME_RE,
        _env_line_defines_key,
        _sanitize_env_lines,
        _secure_file,
    )

    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    if not path.exists():
        return False

    read_kw = {"encoding": "utf-8-sig", "errors": "replace"}
    write_kw = {"encoding": "utf-8"}
    with open(path, **read_kw) as handle:
        lines = handle.readlines()
    lines = _sanitize_env_lines(lines)
    new_lines = [line for line in lines if not _env_line_defines_key(line, key)]
    found = len(new_lines) < len(lines)
    if not found:
        return False

    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".env_")
    original_mode = None
    try:
        original_mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        original_mode = None
    try:
        with os.fdopen(fd, "w", **write_kw) as handle:
            handle.writelines(new_lines)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, path)
        if original_mode is not None:
            try:
                os.chmod(path, original_mode)
            except OSError:
                pass
        else:
            _secure_file(path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True


def _scope_label(scope: str, skill: str) -> str:
    if scope == "skill":
        return f"skill {skill} env"
    return "active profile env"


def run_senv(args: str, *, messenger: bool = False) -> SenvResult:
    """Execute /senv. Return text never contains the secret value."""
    parsed = parse_senv_args(args)
    if "error" in parsed:
        return SenvResult(text=parsed["error"], ok=False, action="error")

    action = parsed["action"]
    scope = parsed["scope"]
    skill = parsed.get("skill") or ""
    secret = parsed.get("value") or ""

    try:
        if action == "list":
            if scope == "skill":
                env_path = _skill_env_path(skill)
                if isinstance(env_path, dict):
                    return SenvResult(text=env_path["error"], ok=False, action="list")
            else:
                from hermes_cli.config import get_env_path

                env_path = get_env_path()
            keys = _list_keys(env_path)
            label = _scope_label(scope, skill)
            if not keys:
                return SenvResult(
                    text=f"No keys in {label}.",
                    ok=True,
                    action="list",
                    scope=scope,
                )
            listed = ", ".join(keys)
            return SenvResult(
                text=f"Keys in {label}: {listed}",
                ok=True,
                action="list",
                scope=scope,
            )

        if action == "delete":
            key = parsed["key"]
            if scope == "skill":
                env_path = _skill_env_path(skill)
                if isinstance(env_path, dict):
                    return SenvResult(text=env_path["error"], ok=False, action="delete")
                removed = _remove_env_file_key(env_path, key)
            else:
                from hermes_cli.config import remove_env_value

                removed = remove_env_value(key)
            label = _scope_label(scope, skill)
            if not removed:
                return SenvResult(
                    text=f"{key} was not set in {label}.",
                    ok=True,
                    action="delete",
                    key=key,
                    scope=scope,
                )
            return SenvResult(
                text=f"Deleted {key} from {label}.",
                ok=True,
                action="delete",
                key=key,
                scope=scope,
            )

        # set
        key = parsed["key"]
        if scope == "skill":
            env_path = _skill_env_path(skill)
            if isinstance(env_path, dict):
                return SenvResult(text=env_path["error"], ok=False, action="set")
            _upsert_env_file(env_path, key, secret)
        else:
            from hermes_cli.config import save_env_value

            save_env_value(key, secret)
        label = _scope_label(scope, skill)
        text = f"Saved {key} to {label}."
        if messenger:
            text = f"{text} {DELETE_USER_MESSAGE_HINT}"
        return SenvResult(text=text, ok=True, action="set", key=key, scope=scope)
    except ValueError as exc:
        logger.debug("senv rejected key %s: %s", parsed.get("key", ""), type(exc).__name__)
        return SenvResult(text="Could not save that key.", ok=False, action=action)
    except Exception:
        logger.debug("senv failed for action=%s key=%s", action, parsed.get("key", ""), exc_info=True)
        return SenvResult(text="Could not update the env file.", ok=False, action=action)
    finally:
        parsed.pop("value", None)


def adapter_can_delete_user_message(adapter: object | None) -> bool:
    """True when the adapter overrides BasePlatformAdapter.delete_message."""
    if adapter is None:
        return False
    try:
        from gateway.platforms.base import BasePlatformAdapter
    except Exception:
        return False
    delete_fn = getattr(type(adapter), "delete_message", None)
    return bool(delete_fn) and delete_fn is not BasePlatformAdapter.delete_message


async def maybe_delete_user_senv_message(
    adapter: object | None,
    event: object | None,
    *,
    enabled: bool,
) -> bool:
    """Best-effort inbound delete. Failures never include the secret."""
    if not enabled or not adapter_can_delete_user_message(adapter) or event is None:
        return False
    source = getattr(event, "source", None)
    chat_id = getattr(source, "chat_id", None) if source is not None else None
    message_id = getattr(event, "message_id", None)
    if not chat_id or not message_id:
        return False
    delete_fn = getattr(adapter, "delete_message", None)
    if delete_fn is None:
        return False
    try:
        result = delete_fn(str(chat_id), str(message_id))
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)
    except Exception:
        logger.debug("senv inbound message delete failed", exc_info=True)
        return False
