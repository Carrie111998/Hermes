"""Action-specific policies for the side-effect idempotency ledger."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionTypePolicy:
    stale_seconds: int
    verifiable: bool
    vendor: str
    allow_legit_duplicates: bool


ACTION_POLICIES: dict[str, ActionTypePolicy] = {
    "sms.send": ActionTypePolicy(
        stale_seconds=900,
        verifiable=True,
        vendor="retell",
        allow_legit_duplicates=True,
    ),
    "retell.call": ActionTypePolicy(
        stale_seconds=1800,
        verifiable=True,
        vendor="retell",
        allow_legit_duplicates=True,
    ),
    "email.send": ActionTypePolicy(
        stale_seconds=900,
        verifiable=True,
        vendor="gmail",
        allow_legit_duplicates=True,
    ),
    "calendar.create": ActionTypePolicy(
        stale_seconds=300,
        verifiable=True,
        vendor="gcal",
        allow_legit_duplicates=False,
    ),
    "calendar.update": ActionTypePolicy(
        stale_seconds=300,
        verifiable=True,
        vendor="gcal",
        allow_legit_duplicates=True,
    ),
    "calendar.delete": ActionTypePolicy(
        stale_seconds=300,
        verifiable=True,
        vendor="gcal",
        allow_legit_duplicates=False,
    ),
    "gbp.post": ActionTypePolicy(
        stale_seconds=1800,
        verifiable=False,
        vendor="gbp",
        allow_legit_duplicates=False,
    ),
    "gbp.reply": ActionTypePolicy(
        stale_seconds=1800,
        verifiable=False,
        vendor="gbp",
        allow_legit_duplicates=False,
    ),
    "appstore.reply": ActionTypePolicy(
        stale_seconds=1800,
        verifiable=True,
        vendor="apple",
        allow_legit_duplicates=False,
    ),
    "github.pr.open": ActionTypePolicy(
        stale_seconds=600,
        verifiable=True,
        vendor="github",
        allow_legit_duplicates=False,
    ),
    "github.pr.comment": ActionTypePolicy(
        stale_seconds=600,
        verifiable=True,
        vendor="github",
        allow_legit_duplicates=True,
    ),
    "telegram.send": ActionTypePolicy(
        stale_seconds=3600,
        verifiable=False,
        vendor="telegram",
        allow_legit_duplicates=True,
    ),
    "test.action": ActionTypePolicy(
        stale_seconds=60,
        verifiable=False,
        vendor="test",
        allow_legit_duplicates=True,
    ),
}


def get_policy(action_type: str) -> ActionTypePolicy:
    try:
        return ACTION_POLICIES[action_type]
    except KeyError as exc:
        raise ValueError(f"unknown action_type: {action_type}") from exc


__all__ = ["ACTION_POLICIES", "ActionTypePolicy", "get_policy"]
