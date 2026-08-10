"""Versioned wire contracts for outbound Hermes workspace runners."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any, cast, Mapping

RUNNER_PROTOCOL_VERSION = 1

RUNNER_METHODS = frozenset(
    {
        "binding.inspect",
        "fs.list",
        "fs.read",
        "fs.stat",
        "fs.write",
        "git.commit",
        "git.push.request",
        "git.status",
        "git.worktree.add",
        "git.worktree.remove",
        "process.cancel",
        "terminal.run",
        "worker.codex",
    }
)
RUNNER_EVENT_TYPES = frozenset(
    {
        "binding.registered",
        "device.heartbeat",
        "run.accepted",
        "run.completed",
        "run.failed",
        "run.output",
        "run.reconciling",
        "run.started",
        "run.uncertain",
    }
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _validate_id(name: str, value: str) -> str:
    normalized = str(value or "").strip()
    if not _ID_RE.fullmatch(normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("runner payload must be JSON serializable") from exc


@dataclass(frozen=True, slots=True)
class RunnerCommand:
    attempt_id: str
    binding_id: str
    command_id: str
    fencing_token: int
    lease_id: str
    method: str
    params: dict[str, Any]
    run_id: str
    protocol_version: int = RUNNER_PROTOCOL_VERSION

    @classmethod
    def create(
        cls,
        *,
        method: str,
        run_id: str,
        attempt_id: str,
        binding_id: str,
        params: Mapping[str, Any],
        lease_id: str,
        fencing_token: int,
        command_id: str | None = None,
    ) -> "RunnerCommand":
        if method not in RUNNER_METHODS:
            raise ValueError("runner method is not allowed")
        if not isinstance(fencing_token, int) or isinstance(fencing_token, bool) or fencing_token < 0:
            raise ValueError("fencing token must be a non-negative integer")
        if not isinstance(params, Mapping):
            raise ValueError("runner params must be an object")

        command = cls(
            attempt_id=_validate_id("attempt_id", attempt_id),
            binding_id=_validate_id("binding_id", binding_id),
            command_id=_validate_id("command_id", command_id or str(uuid.uuid4())),
            fencing_token=fencing_token,
            lease_id=_validate_id("lease_id", lease_id),
            method=method,
            params=dict(params),
            run_id=_validate_id("run_id", run_id),
        )
        _canonical_json(command.to_dict())
        return command

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunnerCommand":
        if int(value.get("protocol_version", 0)) != RUNNER_PROTOCOL_VERSION:
            raise ValueError("unsupported runner protocol version")
        return cls.create(
            attempt_id=str(value.get("attempt_id") or ""),
            binding_id=str(value.get("binding_id") or ""),
            command_id=str(value.get("command_id") or ""),
            fencing_token=cast(int, value.get("fencing_token")),
            lease_id=str(value.get("lease_id") or ""),
            method=str(value.get("method") or ""),
            params=value.get("params") or {},
            run_id=str(value.get("run_id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunnerEvent:
    attempt_id: str
    event_id: str
    event_type: str
    payload: dict[str, Any]
    run_id: str
    sequence: int
    protocol_version: int = RUNNER_PROTOCOL_VERSION

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        attempt_id: str,
        sequence: int,
        event_type: str,
        payload: Mapping[str, Any],
        event_id: str | None = None,
    ) -> "RunnerEvent":
        if event_type not in RUNNER_EVENT_TYPES:
            raise ValueError("runner event type is not allowed")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise ValueError("runner event sequence must be a positive integer")
        if not isinstance(payload, Mapping):
            raise ValueError("runner event payload must be an object")

        event = cls(
            attempt_id=_validate_id("attempt_id", attempt_id),
            event_id=_validate_id("event_id", event_id or str(uuid.uuid4())),
            event_type=event_type,
            payload=dict(payload),
            run_id=_validate_id("run_id", run_id),
            sequence=sequence,
        )
        _canonical_json(event.to_dict())
        return event

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunnerEvent":
        if int(value.get("protocol_version", 0)) != RUNNER_PROTOCOL_VERSION:
            raise ValueError("unsupported runner protocol version")
        return cls.create(
            attempt_id=str(value.get("attempt_id") or ""),
            event_id=str(value.get("event_id") or ""),
            event_type=str(value.get("event_type") or ""),
            payload=value.get("payload") or {},
            run_id=str(value.get("run_id") or ""),
            sequence=cast(int, value.get("sequence")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sign_envelope(payload: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    if not isinstance(key, bytes) or not key:
        raise ValueError("runner signing key is required")
    canonical = _canonical_json(payload)
    normalized_payload = json.loads(canonical.decode("utf-8"))
    signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return {"payload": normalized_payload, "signature": signature}


def verify_envelope(envelope: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if not isinstance(payload, Mapping) or not isinstance(signature, str):
        raise ValueError("runner envelope is malformed")
    expected = hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("runner envelope signature is invalid")
    return dict(payload)
