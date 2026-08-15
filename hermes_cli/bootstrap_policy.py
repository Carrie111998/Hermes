"""Process-local CLI bootstrap policy with no filesystem side effects."""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class BootstrapPolicy(Enum):
    NORMAL = "normal"
    ISOLATED_ONESHOT = "isolated_oneshot"


_policy = BootstrapPolicy.NORMAL


def classify_argv(argv: Iterable[str]) -> BootstrapPolicy:
    args = tuple(argv)
    has_isolated = "--isolated" in args
    has_oneshot = "-z" in args or "--oneshot" in args
    if has_isolated and has_oneshot:
        return BootstrapPolicy.ISOLATED_ONESHOT
    return BootstrapPolicy.NORMAL


def set_policy(policy: BootstrapPolicy) -> None:
    global _policy
    _policy = policy


def is_isolated_oneshot() -> bool:
    return _policy is BootstrapPolicy.ISOLATED_ONESHOT
