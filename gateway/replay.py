"""Native gateway replay primitives.

Replay runs bridge-message corpora through the real gateway/adapter message path
without connecting live adapters or delivering live outbound messages.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
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


def canonical_json(value: Any) -> str:
    """Return the canonical JSON representation used for replay digests."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def canonical_digest(value: Any) -> str:
    """Return a stable sha256 digest for a manifest-like Python value."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _manifest_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


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


def _message_id(message: Mapping[str, Any]) -> str | None:
    value = message.get("messageId") or message.get("message_id") or message.get("id")
    return str(value) if value is not None and str(value) else None


def _message_timestamp(message: Mapping[str, Any]) -> int | None:
    return _timestamp_to_epoch(message.get("timestamp") or message.get("ts"))


def _default_code_manifest() -> dict[str, Any]:
    """Best-effort readable code/artifact manifest for a replay attempt."""
    repo = Path(__file__).resolve().parents[1]
    manifest: dict[str, Any] = {
        "repo": str(repo),
        "runtime": "hermes",
        "replay_module": "gateway.replay",
        "replay_cli": "hermes replay",
    }
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        if commit:
            manifest["git_commit"] = commit
    except Exception:
        pass
    try:
        dirty = subprocess.call(
            ["git", "-C", str(repo), "diff", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        staged_dirty = subprocess.call(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        manifest["git_dirty"] = bool(dirty or staged_dirty)
    except Exception:
        pass
    if os.environ.get("HERMES_HOME"):
        manifest["hermes_home"] = os.environ.get("HERMES_HOME")
    return manifest


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
    replay_namespace: Optional[str] = None
    replay_policy: Mapping[str, Any] = field(default_factory=dict)
    corpus_manifest: Mapping[str, Any] = field(default_factory=dict)
    config_overlay_manifest: Mapping[str, Any] = field(default_factory=dict)
    target_descriptor_manifest: Mapping[str, Any] = field(default_factory=dict)
    target_baseline_manifest: Mapping[str, Any] = field(default_factory=dict)
    code_manifest: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        namespace = str(self.replay_namespace or f"agent:replay:{self.run_id}").strip(":")
        if not namespace:
            raise ValueError("replay_namespace cannot be empty")
        if namespace.startswith("agent:main"):
            raise ValueError("replay_namespace must not use the live agent:main namespace")
        object.__setattr__(self, "replay_namespace", namespace)

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

        replay_namespace = data.get("replay_namespace") or data.get("replayNamespace")
        replay_policy = data.get("replay_policy") or data.get("replayPolicy") or {}
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
            replay_namespace=str(replay_namespace) if replay_namespace else None,
            replay_policy=_manifest_mapping(replay_policy),
            corpus_manifest=_manifest_mapping(data.get("corpus_manifest") or data.get("corpusManifest")),
            config_overlay_manifest=_manifest_mapping(
                data.get("config_overlay_manifest") or data.get("configOverlayManifest") or data.get("config_overlay") or data.get("configOverlay")
            ),
            target_descriptor_manifest=_manifest_mapping(
                data.get("target_descriptor_manifest") or data.get("targetDescriptorManifest") or data.get("target_descriptor") or data.get("targetDescriptor")
            ),
            target_baseline_manifest=_manifest_mapping(
                data.get("target_baseline_manifest") or data.get("targetBaselineManifest") or data.get("target_baseline") or data.get("targetBaseline")
            ),
            code_manifest=_manifest_mapping(data.get("code_manifest") or data.get("codeManifest")),
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
            "replay_namespace": self.replay_namespace,
            "delivery_mode": self.delivery_mode,
            "bypass_require_mention": self.bypass_require_mention,
            "bypass_auth": self.bypass_auth,
            "replay_safe_commands": list(self.replay_safe_commands),
            "history_before_ts": self.history_before_ts,
            "source_path": self.source_path,
            "replay_policy": dict(self.replay_policy or {}),
            "corpus_manifest": dict(self.corpus_manifest or {}),
            "config_overlay_manifest": dict(self.config_overlay_manifest or {}),
            "target_descriptor_manifest": dict(self.target_descriptor_manifest or {}),
            "target_baseline_manifest": dict(self.target_baseline_manifest or {}),
            "code_manifest": dict(self.code_manifest or {}),
        }

    def policy_manifest(self) -> dict[str, Any]:
        manifest = dict(self.replay_policy or {})
        manifest.update({
            "execution_mode": "replay",
            "delivery_mode": self.delivery_mode,
            "bypass_require_mention": self.bypass_require_mention,
            "bypass_auth": self.bypass_auth,
            "history_before_ts": self.history_before_ts,
            "replay_safe_commands": list(self.replay_safe_commands),
            "replay_namespace": self.replay_namespace,
            "session_namespace_strategy": "replace agent:main prefix",
        })
        return manifest

    def readable_corpus_manifest(self) -> dict[str, Any]:
        manifest = dict(self.corpus_manifest or {})
        messages = list(self.messages)
        timestamps = [
            ts for ts in (_message_timestamp(m) for m in messages if isinstance(m, Mapping))
            if ts is not None
        ]
        ids = [
            mid for mid in (_message_id(m) for m in messages if isinstance(m, Mapping))
            if mid is not None
        ]
        manifest.update({
            "source_path": self.source_path,
            "message_count": len(messages),
            "message_ids": ids[:50],
            "message_ids_truncated": len(ids) > 50,
            "first_timestamp": min(timestamps) if timestamps else None,
            "last_timestamp": max(timestamps) if timestamps else None,
            "messages_digest": canonical_digest(messages),
        })
        return manifest

    def provenance_manifests(self) -> dict[str, dict[str, Any]]:
        target_descriptor = dict(self.target_descriptor_manifest or {})
        if target_descriptor and "run_id" not in target_descriptor:
            target_descriptor["run_id"] = self.run_id
        code = dict(self.code_manifest or {}) or _default_code_manifest()
        return {
            "corpus": self.readable_corpus_manifest(),
            "config_overlay": dict(self.config_overlay_manifest or {}),
            "target_descriptor": target_descriptor,
            "target_baseline": dict(self.target_baseline_manifest or {}),
            "code": code,
            "replay_policy": self.policy_manifest(),
        }

    def manifest_digest(self) -> str:
        return canonical_digest(self.provenance_manifests())


def namespace_session_key(session_key: str, replay_namespace: str) -> str:
    """Map a live session key into the replay namespace."""
    namespace = str(replay_namespace).strip(":")
    if not namespace:
        raise ValueError("replay namespace cannot be empty")
    if session_key.startswith(namespace + ":"):
        return session_key
    live_prefix = "agent:main:"
    if session_key.startswith(live_prefix):
        return f"{namespace}:{session_key[len(live_prefix):]}"
    return f"{namespace}:{session_key}"


@dataclass(frozen=True)
class ReplayAttempt:
    """Persisted replay provenance card.

    This intentionally stores manifests + digests only. Execution reports are
    derived from normal Hermes/PA rows tagged by run/attempt id.
    """

    attempt_id: str
    run_id: str
    replay_namespace: str
    platform: str
    delivery_mode: str
    status: str
    started_at: float
    completed_at: Optional[float] = None
    corpus_manifest: Mapping[str, Any] = field(default_factory=dict)
    corpus_digest: str = ""
    config_overlay_manifest: Mapping[str, Any] = field(default_factory=dict)
    config_overlay_digest: str = ""
    target_descriptor_manifest: Mapping[str, Any] = field(default_factory=dict)
    target_descriptor_digest: str = ""
    target_baseline_manifest: Mapping[str, Any] = field(default_factory=dict)
    target_baseline_digest: str = ""
    code_manifest: Mapping[str, Any] = field(default_factory=dict)
    code_digest: str = ""
    replay_policy_manifest: Mapping[str, Any] = field(default_factory=dict)
    replay_policy_digest: str = ""
    plan_manifest: Mapping[str, Any] = field(default_factory=dict)
    plan_digest: str = ""
    error: Optional[Any] = None

    @classmethod
    def from_plan(cls, plan: ReplayPlan, *, status: str = "running") -> "ReplayAttempt":
        manifests = plan.provenance_manifests()
        plan_manifest = {
            "run_id": plan.run_id,
            "attempt_id": plan.attempt_id,
            "platform": plan.platform,
            "replay_namespace": plan.replay_namespace,
            "manifest_digests": {
                name: canonical_digest(value)
                for name, value in manifests.items()
            },
        }
        return cls(
            attempt_id=plan.attempt_id,
            run_id=plan.run_id,
            replay_namespace=str(plan.replay_namespace),
            platform=plan.platform,
            delivery_mode=plan.delivery_mode,
            status=status,
            started_at=time.time(),
            corpus_manifest=manifests["corpus"],
            corpus_digest=canonical_digest(manifests["corpus"]),
            config_overlay_manifest=manifests["config_overlay"],
            config_overlay_digest=canonical_digest(manifests["config_overlay"]),
            target_descriptor_manifest=manifests["target_descriptor"],
            target_descriptor_digest=canonical_digest(manifests["target_descriptor"]),
            target_baseline_manifest=manifests["target_baseline"],
            target_baseline_digest=canonical_digest(manifests["target_baseline"]),
            code_manifest=manifests["code"],
            code_digest=canonical_digest(manifests["code"]),
            replay_policy_manifest=manifests["replay_policy"],
            replay_policy_digest=canonical_digest(manifests["replay_policy"]),
            plan_manifest=plan_manifest,
            plan_digest=canonical_digest(plan_manifest),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "replay_namespace": self.replay_namespace,
            "platform": self.platform,
            "delivery_mode": self.delivery_mode,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "corpus_manifest": dict(self.corpus_manifest or {}),
            "corpus_digest": self.corpus_digest,
            "config_overlay_manifest": dict(self.config_overlay_manifest or {}),
            "config_overlay_digest": self.config_overlay_digest,
            "target_descriptor_manifest": dict(self.target_descriptor_manifest or {}),
            "target_descriptor_digest": self.target_descriptor_digest,
            "target_baseline_manifest": dict(self.target_baseline_manifest or {}),
            "target_baseline_digest": self.target_baseline_digest,
            "code_manifest": dict(self.code_manifest or {}),
            "code_digest": self.code_digest,
            "replay_policy_manifest": dict(self.replay_policy_manifest or {}),
            "replay_policy_digest": self.replay_policy_digest,
            "plan_manifest": dict(self.plan_manifest or {}),
            "plan_digest": self.plan_digest,
            "error": self.error,
        }

    def to_db_kwargs(self) -> dict[str, Any]:
        return self.to_dict()


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
    def replay_namespace(self) -> str:
        return str(self.plan.replay_namespace)

    @property
    def delivery_mode(self) -> str:
        return self.plan.delivery_mode

    @property
    def bypass_auth(self) -> bool:
        return self.plan.bypass_auth

    @property
    def replay_safe_commands(self) -> set[str]:
        return {cmd.lstrip("/").lower() for cmd in self.plan.replay_safe_commands}

    def namespace_session_key(self, session_key: str) -> str:
        return namespace_session_key(session_key, self.replay_namespace)

    def bridge_headers(self) -> dict[str, str]:
        return {
            "X-Replay-Run-Id": self.run_id,
            "X-Replay-Attempt-Id": self.attempt_id,
            "X-Replay-Namespace": self.replay_namespace,
        }

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
            "replay_run_id": self.run_id,
            "replay_attempt_id": self.attempt_id,
            "replay_namespace": self.replay_namespace,
            "headers": self.bridge_headers(),
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
    attempt: Optional[dict[str, Any]] = None
    execution_report: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "platform": self.platform,
            "processed": self.processed,
            "outbound": self.outbound,
            "blocked_commands": self.blocked_commands,
            "delivery_mode": self.delivery_mode,
            "attempt": self.attempt,
            "execution_report": self.execution_report,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=_json_default)
