"""Task-local frozen skill allowlist used by model-facing skill tools."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Iterator


_ENABLED_SKILLS: ContextVar[tuple[str, ...] | None] = ContextVar(
    "hermes_enabled_skills", default=None
)


def current_enabled_skills() -> tuple[str, ...] | None:
    return _ENABLED_SKILLS.get()


@contextmanager
def bind_enabled_skills(
    identities: Iterable[str] | None,
) -> Iterator[None]:
    value = tuple(identities) if identities is not None else None
    token = _ENABLED_SKILLS.set(value)
    try:
        yield
    finally:
        _ENABLED_SKILLS.reset(token)
