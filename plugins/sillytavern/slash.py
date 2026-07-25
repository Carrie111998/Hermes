"""In-session slash commands for SillyTavern RP (plugin-owned, not core CLI).

Handlers are registered via ``ctx.register_command`` so they appear in CLI and
gateway without touching ``cli.py`` / ``COMMAND_REGISTRY``.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Callable

# Local tool callables — imported lazily inside handlers to avoid import cycles
# during plugin discovery.


def _loads(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except Exception as exc:
        return {"ok": False, "error": f"invalid tool payload: {exc}"}
    return data if isinstance(data, dict) else {"ok": False, "error": "non-object payload"}


def _fmt(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _call(tool: Callable[..., str], args: dict[str, Any]) -> str:
    return _fmt(_loads(tool(args)))


def handle_rp(raw_args: str) -> str:
    """``/rp <subcommand> ...`` — ST-native roleplay without requiring ST server."""
    from . import (
        st_character_create,
        st_character_list,
        st_lore_add,
        st_persona_create,
        st_session_reply,
        st_session_say,
        st_session_start,
        st_session_summary,
        st_session_to_memory,
        sillytavern_status,
    )

    text = (raw_args or "").strip()
    if not text or text.lower() in {"help", "-h", "--help"}:
        return (
            "Usage: /rp <subcommand> [args...]\n"
            "  list\n"
            "  create <name> [--desc TEXT] [--personality TEXT] [--first TEXT]\n"
            "  persona <name> [--desc TEXT]\n"
            "  start <character_id> [persona_id]\n"
            "  say <session_id> <message...>\n"
            "  reply <session_id> <content...>\n"
            "  summary <session_id> <text...>\n"
            "  memory <session_id>\n"
            "  lore <book> <content...> [--keys a,b]\n"
            "  status\n"
            "Note: prefer agent tools (st_*) for long turns; slash is for quick ops."
        )

    try:
        parts = shlex.split(text, posix=False)
    except ValueError as exc:
        return _fmt({"ok": False, "error": f"parse error: {exc}"})

    sub = parts[0].lower()
    rest = parts[1:]

    if sub == "list":
        return _call(st_character_list, {})

    if sub == "status":
        return _call(sillytavern_status, {})

    if sub == "create":
        if not rest:
            return _fmt({"ok": False, "error": "Usage: /rp create <name> [--desc ...] [--personality ...] [--first ...]"})
        name = rest[0]
        opts = _parse_flags(rest[1:], {"desc", "personality", "first", "scenario"})
        return _call(
            st_character_create,
            {
                "name": name,
                "description": opts.get("desc", ""),
                "personality": opts.get("personality", ""),
                "first_mes": opts.get("first", ""),
                "scenario": opts.get("scenario", ""),
            },
        )

    if sub == "persona":
        if not rest:
            return _fmt({"ok": False, "error": "Usage: /rp persona <name> [--desc TEXT]"})
        opts = _parse_flags(rest[1:], {"desc"})
        return _call(
            st_persona_create,
            {"name": rest[0], "description": opts.get("desc", ""), "is_default": True},
        )

    if sub == "start":
        if not rest:
            return _fmt({"ok": False, "error": "Usage: /rp start <character_id> [persona_id]"})
        try:
            character_id = int(rest[0])
            persona_id = int(rest[1]) if len(rest) > 1 else None
        except ValueError:
            return _fmt({"ok": False, "error": "character_id / persona_id must be integers"})
        payload: dict[str, Any] = {"character_id": character_id}
        if persona_id is not None:
            payload["persona_id"] = persona_id
        return _call(st_session_start, payload)

    if sub == "say":
        if len(rest) < 2:
            return _fmt({"ok": False, "error": "Usage: /rp say <session_id> <message...>"})
        try:
            session_id = int(rest[0])
        except ValueError:
            return _fmt({"ok": False, "error": "session_id must be an integer"})
        return _call(st_session_say, {"session_id": session_id, "message": " ".join(rest[1:])})

    if sub == "reply":
        if len(rest) < 2:
            return _fmt({"ok": False, "error": "Usage: /rp reply <session_id> <content...>"})
        try:
            session_id = int(rest[0])
        except ValueError:
            return _fmt({"ok": False, "error": "session_id must be an integer"})
        return _call(st_session_reply, {"session_id": session_id, "content": " ".join(rest[1:])})

    if sub == "summary":
        if len(rest) < 2:
            return _fmt({"ok": False, "error": "Usage: /rp summary <session_id> <text...>"})
        try:
            session_id = int(rest[0])
        except ValueError:
            return _fmt({"ok": False, "error": "session_id must be an integer"})
        return _call(st_session_summary, {"session_id": session_id, "summary": " ".join(rest[1:])})

    if sub == "memory":
        if not rest:
            return _fmt({"ok": False, "error": "Usage: /rp memory <session_id>"})
        try:
            session_id = int(rest[0])
        except ValueError:
            return _fmt({"ok": False, "error": "session_id must be an integer"})
        return _call(st_session_to_memory, {"session_id": session_id})

    if sub == "lore":
        if len(rest) < 2:
            return _fmt({"ok": False, "error": "Usage: /rp lore <book> <content...> [--keys a,b]"})
        book = rest[0]
        opts = _parse_flags(rest[1:], {"keys"})
        # content is remaining non-flag tokens before flags were consumed
        content_parts: list[str] = []
        i = 1
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("--"):
                break
            content_parts.append(tok)
            i += 1
        content = " ".join(content_parts).strip()
        if not content:
            return _fmt({"ok": False, "error": "lore content is required"})
        keys_raw = opts.get("keys", "")
        keys = [k.strip() for k in keys_raw.split(",") if k.strip()] if keys_raw else []
        return _call(st_lore_add, {"book": book, "content": content, "keys": keys, "enabled": True})

    if sub == "end":
        return _fmt(
            {
                "ok": True,
                "message": "Sessions are durable in the ST-native DB; start a new session with /rp start <character_id>.",
            }
        )

    return _fmt({"ok": False, "error": f"Unknown /rp subcommand: {sub}"})


def handle_st_voice_roleplay(raw_args: str) -> str:
    """``/st-voice-roleplay <start|complete|status> ...`` — thin wrapper over tools."""
    from . import st_voice_roleplay, st_voice_roleplay_complete

    text = (raw_args or "").strip()
    if not text or text.lower() in {"help", "-h", "--help"}:
        return (
            "Usage: /st-voice-roleplay <subcommand> [args...]\n"
            "  start <session_id> [duration_seconds=10]\n"
            "  complete <session_id> <reply_content...>\n"
            "  status\n"
            "Prefer agent tools st_voice_roleplay / st_voice_roleplay_complete for automation."
        )

    try:
        parts = shlex.split(text, posix=False)
    except ValueError as exc:
        return _fmt({"ok": False, "error": f"parse error: {exc}"})

    sub = parts[0].lower()
    rest = parts[1:]

    if sub == "status":
        try:
            from tools.voice_mode import create_audio_recorder

            recorder = create_audio_recorder()
            ready = recorder is not None
        except Exception as exc:
            return _fmt({"ok": False, "error": str(exc), "audio_recorder": False})
        return _fmt(
            {
                "ok": True,
                "audio_recorder": ready,
                "tools": ["st_voice_roleplay", "st_voice_roleplay_complete"],
            }
        )

    if sub == "start":
        if not rest:
            return _fmt({"ok": False, "error": "Usage: /st-voice-roleplay start <session_id> [duration_seconds]"})
        try:
            session_id = int(rest[0])
            duration = int(rest[1]) if len(rest) > 1 else 10
        except ValueError:
            return _fmt({"ok": False, "error": "session_id / duration must be integers"})
        return _call(st_voice_roleplay, {"session_id": session_id, "duration_seconds": duration})

    if sub == "complete":
        if len(rest) < 2:
            return _fmt({"ok": False, "error": "Usage: /st-voice-roleplay complete <session_id> <reply...>"})
        try:
            session_id = int(rest[0])
        except ValueError:
            return _fmt({"ok": False, "error": "session_id must be an integer"})
        return _call(
            st_voice_roleplay_complete,
            {"session_id": session_id, "reply_content": " ".join(rest[1:])},
        )

    return _fmt({"ok": False, "error": f"Unknown /st-voice-roleplay subcommand: {sub}"})


def _parse_flags(tokens: list[str], allowed: set[str]) -> dict[str, str]:
    """Parse ``--key value`` / ``--key=value`` pairs; ignore unknown flags."""
    out: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("--"):
            i += 1
            continue
        body = tok[2:]
        if "=" in body:
            key, val = body.split("=", 1)
            if key in allowed:
                out[key] = val
            i += 1
            continue
        if body in allowed and i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            out[body] = tokens[i + 1]
            i += 2
            continue
        i += 1
    return out
