"""Native gateway replay primitives.

Replay runs bridge-message corpora through the real gateway/adapter message path
without connecting live adapters or delivering live outbound messages.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional


_REPLAY_CONTEXT: ContextVar["ReplayExecutionContext | None"] = ContextVar(
    "HERMES_REPLAY_CONTEXT",
    default=None,
)
_REPLAY_TURN_HISTORY_BEFORE_TS: ContextVar[int | None] = ContextVar(
    "HERMES_REPLAY_TURN_HISTORY_BEFORE_TS",
    default=None,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _load_json_or_jsonl(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def _coerce_messages(raw: Any) -> list[dict[str, Any]]:
    """Return a bridge-message list from common corpus shapes."""
    if isinstance(raw, list):
        return [m for m in raw if isinstance(m, dict)]
    if not isinstance(raw, dict):
        raise ValueError("replay corpus must be a JSON object or list")
    for key in ("messages", "bridge_messages", "bridgeMessages", "events"):
        value = raw.get(key)
        if isinstance(value, list):
            return [m for m in value if isinstance(m, dict)]
    # Some exports wrap the interesting corpus under a chat/group key.
    corpus = raw.get("corpus")
    if isinstance(corpus, dict):
        return _coerce_messages(corpus)
    raise ValueError("replay corpus object must include messages/bridge_messages/events")


@dataclass(frozen=True)
class ReplayPlan:
    """Typed input for ``GatewayRunner.replay`` and ``hermes replay``."""

    platform: str = "whatsapp"
    messages: tuple[dict[str, Any], ...] = ()
    run_id: str = field(default_factory=lambda: f"replay-{uuid.uuid4().hex[:12]}")
    attempt_id: str = field(default_factory=lambda: f"attempt-{uuid.uuid4().hex[:12]}")
    delivery_mode: str = "capture"  # capture | drop
    bypass_require_mention: bool = True
    bypass_auth: bool = True
    replay_safe_commands: tuple[str, ...] = ()
    history_before_ts: Optional[int] = None
    source_path: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, base_dir: Path | None = None) -> "ReplayPlan":
        if not isinstance(data, Mapping):
            raise ValueError("replay plan must be a JSON object")
        platform = str(data.get("platform") or "whatsapp").strip().lower()
        delivery_mode = str(data.get("delivery_mode") or data.get("deliveryMode") or "capture").strip().lower()
        if delivery_mode not in {"capture", "drop"}:
            raise ValueError("delivery_mode must be 'capture' or 'drop'")

        messages_value = data.get("messages")
        source_path: Optional[str] = None
        if messages_value is None:
            corpus = data.get("corpus") or data.get("input")
            if isinstance(corpus, Mapping):
                corpus_path = corpus.get("path") or corpus.get("file")
                if corpus_path:
                    path = Path(str(corpus_path)).expanduser()
                    if not path.is_absolute() and base_dir is not None:
                        path = base_dir / path
                    source_path = str(path)
                    messages_value = _coerce_messages(_load_json_or_jsonl(path))
                else:
                    messages_value = _coerce_messages(corpus)
            elif isinstance(corpus, (str, Path)):
                path = Path(str(corpus)).expanduser()
                if not path.is_absolute() and base_dir is not None:
                    path = base_dir / path
                source_path = str(path)
                messages_value = _coerce_messages(_load_json_or_jsonl(path))
        messages = tuple(_coerce_messages(messages_value or []))

        safe = data.get("replay_safe_commands") or data.get("replaySafeCommands") or ()
        if isinstance(safe, str):
            safe_commands = tuple(part.strip().lstrip("/").lower() for part in safe.split(",") if part.strip())
        elif isinstance(safe, Iterable):
            safe_commands = tuple(str(part).strip().lstrip("/").lower() for part in safe if str(part).strip())
        else:
            safe_commands = ()

        before = data.get("history_before_ts", data.get("historyBeforeTs"))
        if before is not None:
            try:
                before = int(float(before))
            except (TypeError, ValueError):
                raise ValueError("history_before_ts must be an epoch second") from None

        return cls(
            platform=platform,
            messages=messages,
            run_id=str(data.get("run_id") or data.get("runId") or f"replay-{uuid.uuid4().hex[:12]}"),
            attempt_id=str(data.get("attempt_id") or data.get("attemptId") or f"attempt-{uuid.uuid4().hex[:12]}"),
            delivery_mode=delivery_mode,
            bypass_require_mention=bool(data.get("bypass_require_mention", data.get("bypassRequireMention", True))),
            bypass_auth=bool(data.get("bypass_auth", data.get("bypassAuth", True))),
            replay_safe_commands=safe_commands,
            history_before_ts=before,
            source_path=source_path or (str(data.get("source_path") or data.get("sourcePath") or "") or None),
        )

    @classmethod
    def from_path(cls, path: str | Path) -> "ReplayPlan":
        plan_path = Path(path).expanduser()
        data = _load_json_or_jsonl(plan_path)
        if isinstance(data, list):
            data = {"platform": "whatsapp", "messages": data}
        plan = cls.from_mapping(data, base_dir=plan_path.parent)
        if plan.source_path is None:
            object.__setattr__(plan, "source_path", str(plan_path))
        return plan

    @classmethod
    def from_corpus_path(
        cls,
        path: str | Path,
        *,
        platform: str = "whatsapp",
        delivery_mode: str = "capture",
        bypass_require_mention: bool = True,
        bypass_auth: bool = True,
    ) -> "ReplayPlan":
        corpus_path = Path(path).expanduser()
        messages = tuple(_coerce_messages(_load_json_or_jsonl(corpus_path)))
        return cls(
            platform=platform,
            messages=messages,
            delivery_mode=delivery_mode,
            bypass_require_mention=bypass_require_mention,
            bypass_auth=bypass_auth,
            source_path=str(corpus_path),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "messages": list(self.messages),
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "delivery_mode": self.delivery_mode,
            "bypass_require_mention": self.bypass_require_mention,
            "bypass_auth": self.bypass_auth,
            "replay_safe_commands": list(self.replay_safe_commands),
            "history_before_ts": self.history_before_ts,
            "source_path": self.source_path,
        }


@dataclass
class ReplayExecutionContext:
    plan: ReplayPlan
    started_at: float = field(default_factory=time.time)
    outbound: list[dict[str, Any]] = field(default_factory=list)
    blocked_commands: list[dict[str, Any]] = field(default_factory=list)

    @property
    def execution_mode(self) -> str:
        return "replay"

    @property
    def run_id(self) -> str:
        return self.plan.run_id

    @property
    def attempt_id(self) -> str:
        return self.plan.attempt_id

    @property
    def delivery_mode(self) -> str:
        return self.plan.delivery_mode

    @property
    def bypass_auth(self) -> bool:
        return self.plan.bypass_auth

    @property
    def replay_safe_commands(self) -> set[str]:
        return {cmd.lstrip("/").lower() for cmd in self.plan.replay_safe_commands}

    def command_allowed(self, command: str | None) -> bool:
        if not command:
            return True
        return command.lstrip("/").lower() in self.replay_safe_commands

    def record_blocked_command(self, *, command: str, platform: str = "", chat_id: str = "") -> None:
        self.blocked_commands.append({
            "command": command.lstrip("/"),
            "platform": platform,
            "chat_id": chat_id,
            "reason": "replay_command_side_effect_blocked",
        })

    def record_outbound(self, *, kind: str, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
        message_id = f"replay-{len(self.outbound) + 1}"
        self.outbound.append({
            "message_id": message_id,
            "kind": kind,
            "args": list(args),
            "kwargs": dict(kwargs),
            "delivery_mode": self.delivery_mode,
        })
        return message_id


def current_replay_context() -> ReplayExecutionContext | None:
    return _REPLAY_CONTEXT.get()


def current_history_before_ts() -> int | None:
    ctx = current_replay_context()
    if ctx is None:
        return None
    turn_value = _REPLAY_TURN_HISTORY_BEFORE_TS.get()
    if turn_value is not None:
        return turn_value
    return ctx.plan.history_before_ts


@contextmanager
def replay_context(plan: ReplayPlan) -> Iterator[ReplayExecutionContext]:
    ctx = ReplayExecutionContext(plan=plan)
    ctx_token = _REPLAY_CONTEXT.set(ctx)
    turn_token = _REPLAY_TURN_HISTORY_BEFORE_TS.set(None)
    try:
        yield ctx
    finally:
        _REPLAY_TURN_HISTORY_BEFORE_TS.reset(turn_token)
        _REPLAY_CONTEXT.reset(ctx_token)


def set_replay_turn_history_before_ts(value: int | None):
    return _REPLAY_TURN_HISTORY_BEFORE_TS.set(value)


def reset_replay_turn_history_before_ts(token) -> None:
    _REPLAY_TURN_HISTORY_BEFORE_TS.reset(token)


def _timestamp_to_epoch(value: Any) -> int | None:
    if isinstance(value, Mapping):
        value = value.get("low") or value.get("value") or value.get("seconds")
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        # Accept ISO-ish timestamps from tests/fixtures.
        from datetime import datetime, timezone

        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def history_before_ts_for_event(event: Any) -> int | None:
    raw = getattr(event, "raw_message", None)
    if not isinstance(raw, Mapping):
        return None
    timestamps: list[int] = []
    if isinstance(raw.get("messages"), list):
        for msg in raw.get("messages") or []:
            if isinstance(msg, Mapping):
                ts = _timestamp_to_epoch(msg.get("timestamp"))
                if ts is not None:
                    timestamps.append(ts)
    ts = _timestamp_to_epoch(raw.get("timestamp"))
    if ts is not None:
        timestamps.append(ts)
    if not timestamps:
        return None
    return max(timestamps) + 1


@dataclass
class ReplayResult:
    run_id: str
    attempt_id: str
    platform: str
    processed: int
    outbound: list[dict[str, Any]]
    blocked_commands: list[dict[str, Any]]
    delivery_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "platform": self.platform,
            "processed": self.processed,
            "outbound": self.outbound,
            "blocked_commands": self.blocked_commands,
            "delivery_mode": self.delivery_mode,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=_json_default)
