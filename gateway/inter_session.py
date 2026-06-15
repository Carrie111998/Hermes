"""Generic inter-session messaging support for Hermes gateway runtimes.

The configuration is runtime-scoped.  A profile declares stable session names
and their ``SessionSource`` addresses; tools and the gateway use those names to
write durable mailbox rows and dispatch them back through the normal gateway
message path.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from utils import is_truthy_value

from .config import Platform
from .session import SessionSource, build_session_key


@dataclass(frozen=True)
class InterSessionPeer:
    """One configured addressable Hermes session."""

    name: str
    label: str
    role: str
    source: SessionSource
    pa_job_type: Optional[str] = None
    can_send_to: tuple[str, ...] = ()
    external_output: str = "normal"
    description: str = ""
    session_key: Optional[str] = None


@dataclass(frozen=True)
class InterSessionConfig:
    """Parsed ``inter_session`` config block."""

    enabled: bool = False
    agent_id: str = ""
    sessions: dict[str, InterSessionPeer] = field(default_factory=dict)
    poll_interval_seconds: float = 2.0

    def peer(self, name: str) -> Optional[InterSessionPeer]:
        return self.sessions.get(str(name or "").strip())


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_str_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


def _default_agent_id(raw_config: Mapping[str, Any]) -> str:
    pa = _as_mapping(raw_config.get("pa"))
    overlay = _as_mapping(pa.get("overlay"))
    client = _as_mapping(overlay.get("client"))
    for value in (
        pa.get("agent_id"),
        client.get("agent_id"),
        client.get("id"),
    ):
        text = _as_str(value).strip()
        if text:
            return text
    return "default"


def _source_from_mapping(raw: Mapping[str, Any]) -> SessionSource:
    platform_raw = _as_str(raw.get("platform") or Platform.LOCAL.value).strip().lower()
    platform = Platform(platform_raw)
    chat_id = _as_str(raw.get("chat_id") or ("local" if platform == Platform.LOCAL else "")).strip()
    chat_type = _as_str(
        raw.get("chat_type")
        or ("dm" if platform == Platform.LOCAL else "group")
    ).strip() or "dm"
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_name=_as_str(raw.get("chat_name")).strip() or None,
        chat_type=chat_type,
        user_id=_as_str(raw.get("user_id")).strip() or None,
        user_name=_as_str(raw.get("user_name")).strip() or None,
        thread_id=_as_str(raw.get("thread_id")).strip() or None,
        chat_topic=_as_str(raw.get("chat_topic")).strip() or None,
        user_id_alt=_as_str(raw.get("user_id_alt")).strip() or None,
        chat_id_alt=_as_str(raw.get("chat_id_alt")).strip() or None,
        guild_id=_as_str(raw.get("guild_id")).strip() or None,
        parent_chat_id=_as_str(raw.get("parent_chat_id")).strip() or None,
    )


def parse_inter_session_config(raw_config: Mapping[str, Any] | None) -> InterSessionConfig:
    """Parse ``inter_session`` from raw config.yaml data.

    Invalid session entries are skipped.  A malformed platform value still
    raises from ``Platform(...)`` for the specific peer; the caller receives a
    config with the remaining valid peers instead of losing the whole runtime.
    """

    cfg = _as_mapping(raw_config)
    block = _as_mapping(cfg.get("inter_session"))
    enabled = is_truthy_value(block.get("enabled"), default=False)
    agent_id = _as_str(block.get("agent_id") or _default_agent_id(cfg)).strip() or "default"
    sessions_raw = _as_mapping(block.get("sessions"))

    sessions: dict[str, InterSessionPeer] = {}
    for raw_name, raw_peer in sessions_raw.items():
        name = _as_str(raw_name).strip()
        peer_data = _as_mapping(raw_peer)
        if not name or not peer_data:
            continue
        source_data = _as_mapping(peer_data.get("source"))
        try:
            source = _source_from_mapping(source_data)
        except Exception:
            continue
        label = _as_str(peer_data.get("label") or name).strip() or name
        role = _as_str(peer_data.get("role") or "").strip()
        external_output = _as_str(peer_data.get("external_output") or "normal").strip().lower()
        if external_output not in {"normal", "never"}:
            external_output = "normal"
        sessions[name] = InterSessionPeer(
            name=name,
            label=label,
            role=role,
            source=source,
            pa_job_type=_as_str(peer_data.get("pa_job_type")).strip() or None,
            can_send_to=_as_str_list(peer_data.get("can_send_to")),
            external_output=external_output,
            description=_as_str(peer_data.get("description")).strip(),
            session_key=_as_str(peer_data.get("session_key")).strip() or None,
        )

    try:
        poll_interval = float(block.get("poll_interval_seconds", 2.0))
    except (TypeError, ValueError):
        poll_interval = 2.0
    if poll_interval <= 0:
        poll_interval = 2.0

    return InterSessionConfig(
        enabled=enabled,
        agent_id=agent_id,
        sessions=sessions,
        poll_interval_seconds=poll_interval,
    )


def _session_key_options(
    raw_config: Mapping[str, Any] | None,
    *,
    group_sessions_per_user: Optional[bool] = None,
    thread_sessions_per_user: Optional[bool] = None,
) -> tuple[bool, bool]:
    cfg = _as_mapping(raw_config)
    group = (
        bool(group_sessions_per_user)
        if group_sessions_per_user is not None
        else is_truthy_value(cfg.get("group_sessions_per_user"), default=True)
    )
    thread = (
        bool(thread_sessions_per_user)
        if thread_sessions_per_user is not None
        else is_truthy_value(cfg.get("thread_sessions_per_user"), default=False)
    )
    return group, thread


def configured_session_key(
    peer: InterSessionPeer,
    raw_config: Mapping[str, Any] | None = None,
    *,
    group_sessions_per_user: Optional[bool] = None,
    thread_sessions_per_user: Optional[bool] = None,
) -> str:
    """Return the session key for a configured peer."""

    if peer.session_key:
        return peer.session_key
    group, thread = _session_key_options(
        raw_config,
        group_sessions_per_user=group_sessions_per_user,
        thread_sessions_per_user=thread_sessions_per_user,
    )
    return build_session_key(
        peer.source,
        group_sessions_per_user=group,
        thread_sessions_per_user=thread,
    )


def source_matches_configured_peer(source: SessionSource, peer: InterSessionPeer) -> bool:
    """Best-effort source match used when the live session key has per-user salt."""

    if source.platform != peer.source.platform:
        return False
    if str(source.chat_id or "") != str(peer.source.chat_id or ""):
        return False
    if peer.source.thread_id and str(source.thread_id or "") != str(peer.source.thread_id):
        return False
    if peer.source.chat_type and source.chat_type != peer.source.chat_type:
        return False
    return True


def find_current_session(
    cfg: InterSessionConfig,
    *,
    session_key: str = "",
    source: SessionSource | None = None,
    raw_config: Mapping[str, Any] | None = None,
) -> Optional[InterSessionPeer]:
    """Resolve the configured peer for a live turn."""

    if not cfg.enabled:
        return None
    normalized_key = str(session_key or "").strip()
    if normalized_key:
        for peer in cfg.sessions.values():
            if configured_session_key(peer, raw_config) == normalized_key:
                return peer
    if source is not None:
        for peer in cfg.sessions.values():
            if source_matches_configured_peer(source, peer):
                return peer
    return None


def render_inter_session_prompt(
    cfg: InterSessionConfig,
    current: InterSessionPeer,
) -> str:
    """Render the compact session map injected into the system prompt."""

    can_send = ", ".join(current.can_send_to) if current.can_send_to else "(none)"
    lines = [
        "## Inter-Session Messaging",
        "",
        "Current Hermes session:",
        f"- name: {current.name}",
        f"- label: {current.label}",
        f"- role: {current.role or 'unspecified'}",
        f"- can_send_to: {can_send}",
    ]
    if current.external_output == "never":
        lines.append("- external_output: never (do not expect this session to post externally)")
    lines.extend(["", f"Other {cfg.agent_id} sessions:"])
    for name, peer in cfg.sessions.items():
        if name == current.name:
            continue
        summary = f"- {name}: {peer.label}."
        if peer.role:
            summary += f" Role: {peer.role}."
        if peer.description:
            summary += f" {peer.description}"
        lines.append(summary)
    lines.extend([
        "",
        "To message another configured session, use send_session_message(to, body).",
        "Use only a session name listed in can_send_to.",
    ])
    return "\n".join(lines)


def render_prompt_for_turn(
    raw_config: Mapping[str, Any] | None,
    *,
    session_key: str = "",
    source: SessionSource | None = None,
) -> str:
    """Return prompt context for this turn, or ``\"\"`` if not configured."""

    cfg = parse_inter_session_config(raw_config)
    current = find_current_session(
        cfg,
        session_key=session_key,
        source=source,
        raw_config=raw_config,
    )
    if not current:
        return ""
    return render_inter_session_prompt(cfg, current)


def rendered_mailbox_body(
    *,
    from_peer: InterSessionPeer,
    body: str,
    agent_id: str = "Hermes",
) -> str:
    """Render the target-session input with provenance owned by Hermes."""

    label = f"{from_peer.name} / {from_peer.label}" if from_peer.label else from_peer.name
    agent_label = _as_str(agent_id or "Hermes").strip() or "Hermes"
    return f"[Message from {agent_label} session: {label}]\n\n{body.strip()}"


def source_for_peer(peer: InterSessionPeer) -> SessionSource:
    """Return a copy of a peer's configured source for mutation-safe dispatch."""

    return dataclasses.replace(peer.source)
