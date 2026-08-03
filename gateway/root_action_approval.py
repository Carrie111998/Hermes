"""Immutable root-action approval state shared by Hermes gateway adapters.

Hermes is deliberately only a human approval relay. The authenticated
proposal contains an action id, its parameter digest, an exact preview, and an
expiry. The action itself remains in Pythia's authoritative store; Telegram
callback data is only an action-id reference.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT_ACTION_KIND = "restore_same_run"
ROOT_ACTION_DECISIONS = frozenset({"approve", "deny"})
ROOT_ACTION_PROPOSAL_FIELDS = frozenset(
    {"action_id", "parameter_digest", "preview", "expires_at"}
)
_RFC3339_UTC_Z = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NUMERIC_CHAT_ID = re.compile(r"^-?\d+$")


class RootActionProtocolError(ValueError):
    """Raised when a proposal or decision violates the action contract."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        terminal: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.terminal = terminal

def canonical_json(value: Any) -> bytes:
    """Encode JSON canonically for protocol signatures and digests."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RootActionProtocolError("value is not canonical JSON") from exc


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_UTC_Z.fullmatch(value):
        raise RootActionProtocolError(f"{field} must be RFC3339 UTC with Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RootActionProtocolError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RootActionProtocolError(f"{field} must be UTC")
    return parsed


def _required_text(value: Any, field: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise RootActionProtocolError(f"{field} must be a non-empty string")
    return value


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _default_state_path() -> Path:
    configured = os.getenv("HERMES_ROOT_ACTION_STATE", "").strip()
    if configured:
        return Path(configured)
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "root-action-approvals.json"
    except Exception:
        return Path.home() / ".hermes" / "root-action-approvals.json"


@dataclass(frozen=True)
class RootActionProposal:
    """The only immutable values accepted from a Pythia proposal."""

    action_id: str
    parameter_digest: str
    preview: str
    expires_at: str
    created_at: str

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, now: datetime | None = None
    ) -> "RootActionProposal":
        if not isinstance(payload, Mapping):
            raise RootActionProtocolError("proposal must be a JSON object")
        if set(payload) != ROOT_ACTION_PROPOSAL_FIELDS:
            missing = sorted(ROOT_ACTION_PROPOSAL_FIELDS - set(payload))
            extra = sorted(set(payload) - ROOT_ACTION_PROPOSAL_FIELDS)
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if extra:
                detail.append("unsupported " + ", ".join(extra))
            raise RootActionProtocolError("invalid proposal fields: " + "; ".join(detail))
        action_id = _required_text(payload["action_id"], "action_id", max_length=45)
        digest = _required_text(payload["parameter_digest"], "parameter_digest", max_length=64)
        if not _HEX_SHA256.fullmatch(digest):
            raise RootActionProtocolError("parameter_digest must be lowercase SHA-256")
        preview = _required_text(payload["preview"], "preview", max_length=4096)
        expires_at = _required_text(payload["expires_at"], "expires_at", max_length=32)
        expires = _parse_utc(expires_at, "expires_at")
        current = now or datetime.now(timezone.utc)
        if expires <= current:
            raise RootActionProtocolError("action proposal is expired")
        created_at = _now_rfc3339()
        if _parse_utc(created_at, "created_at") >= expires:
            raise RootActionProtocolError("expires_at is not after server receipt")
        return cls(
            action_id=action_id,
            parameter_digest=digest,
            preview=preview,
            expires_at=expires_at,
            created_at=created_at,
        )

    def payload(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "parameter_digest": self.parameter_digest,
            "preview": self.preview,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class PendingRootAction:
    proposal: RootActionProposal
    callback_url: str
    callback_secret: str
    chat_id: str
    message_id: str | None = None
    decision: str | None = None
    principal: str | None = None
    decided_at: str | None = None
    acknowledged: bool = False
    terminal_failure: bool = False
    terminal_error: str | None = None


class RootActionApprovalStore:
    """Durable, thread-safe store with one-shot decision locking."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = Path(path) if path is not None else _default_state_path()
        self._pending: dict[str, PendingRootAction] = {}
        self._load()

    def _load(self) -> None:
        try:
            records = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return
        if not isinstance(records, dict):
            return
        for action_id, record in records.items():
            if not isinstance(record, dict):
                continue
            try:
                proposal = RootActionProposal(
                    action_id=record["action_id"],
                    parameter_digest=record["parameter_digest"],
                    preview=record["preview"],
                    expires_at=record["expires_at"],
                    created_at=record["created_at"],
                )
                if proposal.action_id != action_id:
                    continue
                pending = PendingRootAction(
                    proposal=proposal,
                    callback_url=record["callback_url"],
                    callback_secret=record["callback_secret"],
                    chat_id=str(record["chat_id"]),
                    message_id=record.get("message_id"),
                    decision=record.get("decision"),
                    principal=record.get("principal"),
                    decided_at=record.get("decided_at"),
                    acknowledged=bool(record.get("acknowledged", False)),
                    terminal_failure=bool(record.get("terminal_failure", False)),
                    terminal_error=record.get("terminal_error"),
                )
                if pending.decision not in (None, *ROOT_ACTION_DECISIONS):
                    continue
                self._pending[action_id] = pending
            except (KeyError, TypeError, ValueError):
                continue

    def _persist_locked(self) -> None:
        payload = {
            action_id: {
                "action_id": pending.proposal.action_id,
                "parameter_digest": pending.proposal.parameter_digest,
                "preview": pending.proposal.preview,
                "expires_at": pending.proposal.expires_at,
                "created_at": pending.proposal.created_at,
                "callback_url": pending.callback_url,
                "callback_secret": pending.callback_secret,
                "chat_id": pending.chat_id,
                "message_id": pending.message_id,
                "decision": pending.decision,
                "principal": pending.principal,
                "decided_at": pending.decided_at,
                "acknowledged": pending.acknowledged,
                "terminal_failure": pending.terminal_failure,
                "terminal_error": pending.terminal_error,
            }
            for action_id, pending in self._pending.items()
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=self._path.name + ".", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def put(self, pending: PendingRootAction) -> bool:
        """Store an action, returning False for an identical duplicate."""
        with self._lock:
            previous = self._pending.get(pending.proposal.action_id)
            if previous is not None:
                if previous.proposal.payload() != pending.proposal.payload():
                    raise RootActionProtocolError(
                        "action_id already has different immutable parameters"
                    )
                return False
            self._pending[pending.proposal.action_id] = pending
            self._persist_locked()
            return True

    def set_message_id(self, action_id: str, message_id: str) -> None:
        with self._lock:
            pending = self._pending.get(action_id)
            if pending is not None:
                self._pending[action_id] = replace(pending, message_id=str(message_id))
                self._persist_locked()

    def bind_chat_id(self, action_id: str, chat_id: str) -> None:
        """Bind the numeric chat id returned by Telegram after send."""
        normalized = str(chat_id).strip()
        if not _NUMERIC_CHAT_ID.fullmatch(normalized):
            raise RootActionProtocolError("Telegram send did not return a numeric chat id")
        with self._lock:
            pending = self._pending.get(action_id)
            if pending is not None:
                self._pending[action_id] = replace(pending, chat_id=normalized)
                self._persist_locked()

    def get(self, action_id: str) -> PendingRootAction | None:
        with self._lock:
            return self._pending.get(action_id)

    def pending_deliveries(self) -> list[PendingRootAction]:
        with self._lock:
            return [
                p
                for p in self._pending.values()
                if (
                    p.decision is not None
                    and not p.acknowledged
                    and not p.terminal_failure
                )
            ]

    def mark_terminal_failure(self, action_id: str, error: str) -> None:
        """Persist a terminal authenticated callback failure and stop retries."""
        with self._lock:
            pending = self._pending.get(action_id)
            if pending is not None:
                self._pending[action_id] = replace(
                    pending,
                    terminal_failure=True,
                    terminal_error=str(error)[:500],
                )
                self._persist_locked()

    def remove(self, action_id: str) -> None:
        """Drop an undelivered proposal before a human decision only."""
        with self._lock:
            pending = self._pending.get(action_id)
            if pending is not None and pending.decision is None:
                self._pending.pop(action_id, None)
                self._persist_locked()

    def acknowledge(self, action_id: str) -> None:
        """Retain a durable tombstone after positive Pythia acknowledgement."""
        with self._lock:
            pending = self._pending.get(action_id)
            if pending is not None:
                self._pending[action_id] = replace(pending, acknowledged=True)
                self._persist_locked()

    def consume(
        self,
        action_id: str,
        *,
        decision: str,
        chat_id: str,
        principal: str,
        now: datetime | None = None,
    ) -> PendingRootAction:
        if decision not in ROOT_ACTION_DECISIONS:
            raise RootActionProtocolError("unsupported decision")
        current = now or datetime.now(timezone.utc)
        with self._lock:
            pending = self._pending.get(action_id)
            if pending is None:
                raise RootActionProtocolError("action is unknown or already resolved")
            if pending.acknowledged or pending.decision is not None:
                raise RootActionProtocolError("action decision is already locked")
            if _parse_utc(pending.proposal.expires_at, "expires_at") <= current:
                self._persist_locked()
                raise RootActionProtocolError("action proposal is expired")
            if str(chat_id) != pending.chat_id:
                raise RootActionProtocolError("callback chat does not match action")
            principal = _required_text(principal, "approval_identity.principal")
            decided_at = _now_rfc3339()
            locked = replace(
                pending,
                decision=decision,
                principal=principal,
                decided_at=decided_at,
            )
            self._pending[action_id] = locked
            self._persist_locked()
            return locked


def signed_decision_payload(
    pending: PendingRootAction,
    *,
    decision: str,
    principal: str,
    chat_id: str,
    decided_at: str,
) -> tuple[bytes, str]:
    """Build the callback body and Hermes HMAC over its unsigned fields."""
    if decision not in ROOT_ACTION_DECISIONS:
        raise RootActionProtocolError("unsupported decision")
    principal = _required_text(principal, "approval_identity.principal")
    chat_id = _required_text(chat_id, "approval_identity.chat_id", max_length=128)
    if str(chat_id) != pending.chat_id:
        raise RootActionProtocolError("callback chat does not match action")
    _parse_utc(decided_at, "decided_at")
    unsigned = {
        "action_id": pending.proposal.action_id,
        "parameter_digest": pending.proposal.parameter_digest,
        "decision": decision,
        "approval_identity": {"principal": principal, "chat_id": str(chat_id)},
        "decided_at": decided_at,
    }
    encoded = canonical_json(unsigned)
    signature = hmac.new(
        pending.callback_secret.encode("utf-8"), encoded, hashlib.sha256
    ).hexdigest()
    return encoded, signature


def post_signed_decision(
    pending: PendingRootAction,
    *,
    decision: str,
    principal: str,
    chat_id: str,
    decided_at: str,
    timeout: float = 10.0,
) -> int:
    """POST one signed decision to the fixed Pythia callback URL."""
    body, signature = signed_decision_payload(
        pending,
        decision=decision,
        principal=principal,
        chat_id=chat_id,
        decided_at=decided_at,
    )
    request = urllib.request.Request(
        pending.callback_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Root-Action-Signature": "sha256=" + signature,
            "X-Request-ID": "root-action:" + pending.proposal.action_id + ":" + decision,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        if 400 <= status < 500:
            raise RootActionProtocolError(
                f"Pythia decision callback returned HTTP {status}",
                status=status,
                terminal=True,
            ) from exc
        raise RootActionProtocolError(
            f"Pythia decision callback returned HTTP {status}",
            status=status,
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RootActionProtocolError("Pythia decision callback failed") from exc
    if status < 200 or status >= 300:
        raise RootActionProtocolError(
            f"Pythia decision callback returned HTTP {status}",
            status=status,
            terminal=400 <= status < 500,
        )
    return status


_GLOBAL_STORE = RootActionApprovalStore()


def get_root_action_store() -> RootActionApprovalStore:
    """Return the process-local store shared by Hermes gateway adapters."""
    return _GLOBAL_STORE
