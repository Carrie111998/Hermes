"""Process-local, one-time authentication grants for DevFlow Mission Control."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


class DevflowGrantError(ValueError):
    """A login grant could not be minted or redeemed."""


class DevflowRateLimited(DevflowGrantError):
    """Grant redemption is temporarily rate limited."""


@dataclass(frozen=True)
class RedeemedGrant:
    subject: str


@dataclass(frozen=True)
class _GrantRecord:
    actor: str
    audience: str
    subject: str
    expires_at_monotonic: float


class DevflowLoginGrantStore:
    """Mint digest-stored grants and resolve opaque process-local subjects."""

    DEFAULT_TTL_SECONDS = 60

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        secret: bytes | None = None,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        audit: Callable[[str, dict[str, str]], None] | None = None,
        max_failed_redemptions: int = 10,
        rate_limit_window_seconds: float = 60.0,
    ) -> None:
        self._monotonic = monotonic
        self._secret = secret or secrets.token_bytes(32)
        self._token_factory = token_factory
        self._audit = audit or (lambda _event, _fields: None)
        self._max_failed_redemptions = max(1, int(max_failed_redemptions))
        self._rate_limit_window_seconds = max(1.0, float(rate_limit_window_seconds))
        self._grants: dict[bytes, _GrantRecord] = {}
        self._subjects: dict[str, str] = {}
        self._failed_redemptions: deque[float] = deque()

    @property
    def failed_attempt_count(self) -> int:
        self._prune_failures()
        return len(self._failed_redemptions)

    def mint(
        self,
        *,
        authenticated_actor: str,
        audience: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        actor = _required(authenticated_actor)
        bound_audience = _required(audience)
        if ttl_seconds <= 0 or ttl_seconds > self.DEFAULT_TTL_SECONDS:
            self._audit_safe("devflow_grant_minted", "failure")
            raise DevflowGrantError("grant unavailable")
        grant = self._token_factory()
        subject = self._token_factory()
        if not grant or not subject:
            self._audit_safe("devflow_grant_minted", "failure")
            raise DevflowGrantError("grant unavailable")
        self._grants[self._digest(grant)] = _GrantRecord(
            actor=actor,
            audience=bound_audience,
            subject=subject,
            expires_at_monotonic=self._monotonic() + float(ttl_seconds),
        )
        self._audit_safe("devflow_grant_minted", "success")
        return grant

    def redeem(self, *, grant: str, audience: str) -> RedeemedGrant:
        self._prune_failures()
        if len(self._failed_redemptions) >= self._max_failed_redemptions:
            self._audit_safe("devflow_grant_redeemed", "rate_limited")
            raise DevflowRateLimited("grant temporarily unavailable")

        candidate = grant if isinstance(grant, str) else ""
        bound_audience = audience if isinstance(audience, str) else ""
        digest = self._digest(candidate)
        record = self._grants.get(digest)
        valid = bool(
            record is not None
            and hmac.compare_digest(record.audience.encode(), bound_audience.encode())
            and self._monotonic() <= record.expires_at_monotonic
        )
        if not valid:
            self._failed_redemptions.append(self._monotonic())
            self._audit_safe("devflow_grant_redeemed", "failure")
            raise DevflowGrantError("grant unavailable")

        self._grants.pop(digest, None)
        self._subjects[record.subject] = record.actor
        self._audit_safe("devflow_grant_redeemed", "success")
        return RedeemedGrant(subject=record.subject)

    def actor_for_subject(self, subject: str) -> str | None:
        if not isinstance(subject, str) or not subject:
            return None
        return self._subjects.get(subject)

    def _digest(self, grant: str) -> bytes:
        return hmac.new(self._secret, grant.encode(), hashlib.sha256).digest()

    def _prune_failures(self) -> None:
        cutoff = self._monotonic() - self._rate_limit_window_seconds
        while self._failed_redemptions and self._failed_redemptions[0] <= cutoff:
            self._failed_redemptions.popleft()

    def _audit_safe(self, event: str, outcome: str) -> None:
        try:
            self._audit(event, {"outcome": outcome})
        except Exception:
            pass


def _required(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DevflowGrantError("grant unavailable")
    return value.strip()
