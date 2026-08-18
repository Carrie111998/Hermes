"""Agent-to-agent messaging for Bot Mode installs.

Bot Mode gives each Hermes profile a teammate identity and one canonical
"Bot Chat" conversation. Until now the only way for an agent to reach a
teammate was a shell command the system prompt taught it to assemble
(``tools/bot_mode_probe.py``)::

    hermes -p <agent> chat --in ~ -c "Bot Chat" --create-if-missing -Q -q \
        "Message from 🤖 <me> (@<me>): your message"

That interpolates the message into a double-quoted shell word, so the
message body is parsed by the shell before Hermes ever sees it:

* ``he said "ship it" today`` is delivered as ``he said ship`` — the rest
  becomes stray argv, silently;
* ``$(...)`` and backticks are **executed on the sender's machine** and
  their output is substituted into the message.

Message bodies routinely come from the user or from another agent's reply,
so that is a live injection surface, not only a footgun.

This tool replaces the string-building with an argv list (never a shell),
signs the attribution prefix itself so it cannot be forgotten or malformed,
resolves the canonical Bot Chat session, and routes ``<peer>/<agent>``
targets through ``hermes peer dm`` — the cross-machine half of the same
protocol, which has the identical quoting problem.

Gated (``check_fn``) on the install being Bot-Mode-managed, so it costs
nothing in a model's schema anywhere else.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent.redact import redact_sensitive_text
from hermes_constants import get_default_hermes_root
from tools.bot_mode_probe import (
    BOT_CHAT_TITLE,
    is_bot_mode_install,
    peer_names,
    sender_handle,
)
from tools.registry import registry, tool_error

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SESSION_ID_RE = re.compile(r"^\s*session_id:\s*(\S+)\s*$", re.MULTILINE)
_DEFAULT_TIMEOUT_SECONDS = 300
_MAX_TIMEOUT_SECONDS = 1800
_ALLOWED_MODES = {"sync"}
# Peer targets are "<peer>" or "<peer>/<agent>" (hermes_cli/subcommands/peer.py).
_PEER_TARGET_RE = re.compile(
    r"^(?P<peer>[A-Za-z0-9][A-Za-z0-9_-]{0,63})(?:/(?P<agent>[A-Za-z0-9][A-Za-z0-9_-]{0,63}))?$"
)


def attribution_prefix(handle: str) -> str:
    """The exact opener Bot Mode teammates identify each other by.

    Must stay byte-identical to the one in ``tools/bot_mode_probe.py`` — a
    receiving agent keys off it to know it is being addressed by a teammate
    rather than by the user.
    """
    return f"Message from 🤖 {handle} (@{handle}): "


AGENT_MESSAGE_SCHEMA = {
    "name": "agent_message",
    "description": (
        "Message a teammate agent on this Hermes install, or on a registered "
        "peer gateway, and wait for their reply. Delivers into the "
        "teammate's canonical Bot Chat conversation with your attribution "
        "prefix already applied. Use this instead of building a "
        "'hermes -p ... chat' or 'hermes peer dm' command on the terminal "
        "tool: the message is passed as an argument, so quotes, backticks "
        "and $(...) in it stay text. For multi-step work that must outlive "
        "this turn, use kanban or a durable artifact instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": (
                    "Teammate to message: '<agent>' for one on this install "
                    "(the profile name, e.g. 'narvi'), or '<peer>/<agent>' "
                    "for one on a registered peer gateway. A bare peer name "
                    "reaches that peer's main agent."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "What to say. Send it verbatim — the attribution prefix "
                    "identifying you is added for you, so do not write one."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["sync"],
                "description": "Delivery mode. v1 supports only 'sync': wait for the teammate's final reply.",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_TIMEOUT_SECONDS,
                "description": "Maximum seconds to wait for the teammate. Defaults to 300; capped at 1800.",
            },
        },
        "required": ["to", "message"],
    },
}


def _json_error(message: str, **extra: Any) -> str:
    payload = {"success": False, "error": redact_sensitive_text(str(message))}
    payload.update(extra)
    return json.dumps(payload)


def _validate_profile_name(name: str) -> str:
    profile = str(name or "").strip()
    if not profile:
        raise ValueError("target_profile is required")
    if not _PROFILE_NAME_RE.fullmatch(profile):
        raise ValueError(
            "target_profile must contain only letters, numbers, underscores, or hyphens "
            "and must start with a letter or number"
        )
    return profile


def _profile_home(profile: str, *, root: Path | None = None) -> Path:
    hermes_root = Path(root) if root is not None else get_default_hermes_root()
    if profile == "default":
        return hermes_root
    return hermes_root / "profiles" / profile


def _profile_exists(profile: str, *, root: Path | None = None) -> bool:
    home = _profile_home(profile, root=root)
    return home.exists() and home.is_dir() and (home / "config.yaml").exists()


def _available_profiles(*, root: Path | None = None) -> list[str]:
    hermes_root = Path(root) if root is not None else get_default_hermes_root()
    profiles = []
    if (hermes_root / "config.yaml").exists():
        profiles.append("default")
    profile_dir = hermes_root / "profiles"
    if profile_dir.exists():
        profiles.extend(
            sorted(
                p.name
                for p in profile_dir.iterdir()
                if p.is_dir() and _PROFILE_NAME_RE.fullmatch(p.name) and (p / "config.yaml").exists()
            )
        )
    return profiles


def _resolve_hermes_executable() -> str:
    exe = shutil.which("hermes")
    if not exe:
        raise RuntimeError("Could not find 'hermes' on PATH")
    return exe


def _build_agent_message_command(profile: str, message: str) -> list[str]:
    """argv for a local teammate, targeting the canonical Bot Chat session.

    Mirrors the shell form the Bot Mode protocol section teaches, except the
    message is its own argv element, so no shell ever parses it. ``--in ~``
    pins the workspace the way the protocol does, and ``--create-if-missing``
    makes the first message to a teammate work.

    Keep this shell-free: callers must pass the returned list directly to
    subprocess.run/Popen with shell=False.
    """
    return [
        _resolve_hermes_executable(),
        "--profile",
        profile,
        "chat",
        "--in",
        os.path.expanduser("~"),
        "-c",
        BOT_CHAT_TITLE,
        "--create-if-missing",
        "-Q",
        "-q",
        message,
    ]


def _build_peer_message_command(target: str, message: str) -> list[str]:
    """argv for a teammate on another machine (``hermes peer dm``).

    ``peer dm`` resolves the remote's canonical Bot Chat over HTTP, so the
    session shape is the peer command's business; ours is only to keep the
    message out of a shell word.
    """
    return [_resolve_hermes_executable(), "peer", "dm", target, message]


def _parse_target(raw: str) -> tuple[str, str | None]:
    """Split a target into ``(name, peer)``.

    A ``/`` always means a peer (``<peer>/<agent>``). A bare name is a local
    profile; if no such profile exists but a peer of that name is registered,
    it is taken as that peer's main agent — the same shorthand
    ``hermes peer dm <peer>`` accepts.
    """
    target = str(raw or "").strip()
    if not target:
        raise ValueError("to is required")
    m = _PEER_TARGET_RE.fullmatch(target)
    if not m:
        raise ValueError(
            "to must be '<agent>' or '<peer>/<agent>' using letters, numbers, "
            "underscores or hyphens"
        )
    peer, agent = m.group("peer"), m.group("agent")
    if agent:
        return agent, peer
    if _profile_exists(peer):
        return peer, None
    if peer in peer_names():
        return peer, peer
    return peer, None


def _coerce_timeout(value: Any) -> int:
    if value in (None, ""):
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be an integer")
    if timeout < 1:
        raise ValueError("timeout_seconds must be >= 1")
    return min(timeout, _MAX_TIMEOUT_SECONDS)


def _extract_session_id(output: str) -> str | None:
    match = _SESSION_ID_RE.search(output or "")
    return match.group(1) if match else None


def _strip_session_id_line(output: str) -> str:
    return _SESSION_ID_RE.sub("", output or "").strip()


def _coerce_output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def agent_message_tool(args: dict, **_kw) -> str:
    """Send a message to a teammate agent, locally or on a peer gateway."""
    mode = str(args.get("mode") or "sync").strip().lower()
    if mode not in _ALLOWED_MODES:
        return tool_error("agent_message v1 supports only mode='sync'")

    message = str(args.get("message") or "")
    if not message.strip():
        return tool_error("message is required")

    raw_target = args.get("to", args.get("target_profile", ""))
    try:
        name, peer = _parse_target(raw_target)
        if peer is None:
            name = _validate_profile_name(name)
        timeout = _coerce_timeout(args.get("timeout_seconds"))
    except ValueError as exc:
        return tool_error(str(exc))

    if peer is None and not _profile_exists(name):
        # Listed from the same root _profile_exists() just consulted, so the
        # suggestion can never disagree with the check that produced it.
        return _json_error(
            f"No teammate agent named '{name}' on this install",
            available_agents=_available_profiles(),
            available_peers=peer_names(),
        )

    # Signed here, never by the model: a receiving agent keys off this exact
    # prefix to tell a teammate apart from the user, and the old shell recipe
    # left getting it right to the sender.
    body = attribution_prefix(sender_handle()) + message

    try:
        if peer is not None:
            target = f"{peer}/{name}" if name != peer else peer
            command = _build_peer_message_command(target, body)
        else:
            command = _build_agent_message_command(name, body)
    except RuntimeError as exc:
        return _json_error(str(exc))

    env = os.environ.copy()
    env.pop("HERMES_HOME", None)

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        partial = "".join(
            part for part in [_coerce_output_text(exc.stdout), _coerce_output_text(exc.stderr)] if part
        )
        return _json_error(
            f"agent_message timed out after {timeout}s",
            to=raw_target,
            timeout_seconds=timeout,
            partial_output=redact_sensitive_text(partial.strip())[:4000],
        )
    except OSError as exc:
        return _json_error(f"Failed to reach agent '{name}': {exc}", to=raw_target)

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    combined = "\n".join(part.strip() for part in [stdout, stderr] if part and part.strip())
    session_id = _extract_session_id(combined)
    reply = _strip_session_id_line(stdout)
    if not reply and stderr:
        reply = _strip_session_id_line(stderr)

    payload = {
        "success": completed.returncode == 0,
        "to": raw_target,
        "agent": name,
        "peer": peer,
        "mode": "sync",
        "returncode": completed.returncode,
        "session_id": session_id,
        "reply": redact_sensitive_text(reply),
    }
    if completed.returncode != 0:
        payload["error"] = redact_sensitive_text(combined or f"Hermes exited with status {completed.returncode}")
    return json.dumps(payload)


registry.register(
    name="agent_message",
    toolset="messaging",
    schema=AGENT_MESSAGE_SCHEMA,
    handler=agent_message_tool,
    emoji="🤝",
    # Rung 3 on the Footprint Ladder: this only makes sense where teammates
    # exist, so it stays out of every other install's schema entirely.
    check_fn=lambda: is_bot_mode_install(),
)
