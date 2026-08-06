"""Immutable pre-sandbox environment state shared by collection-time tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True, eq=False)
class OriginalCollectionEnvironment:
    """Only the path inputs needed to resolve the original native Hermes estate."""

    hermes_home_was_set: bool
    hermes_home: str | None = field(repr=False, compare=False)
    home: str | None = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.hermes_home_was_set) is not bool:
            raise TypeError("hermes_home_was_set must be a bool")
        if self.hermes_home_was_set != (self.hermes_home is not None):
            raise ValueError("hermes_home must be present exactly when HERMES_HOME was set")
        if self.hermes_home is not None and not isinstance(self.hermes_home, str):
            raise TypeError("hermes_home must be a string or None")
        if self.home is not None and not isinstance(self.home, str):
            raise TypeError("home must be a string or None")

    def __eq__(self, other: object) -> bool:
        """Retain value equality while keeping raw paths out of pytest diffs."""
        if not isinstance(other, OriginalCollectionEnvironment):
            return NotImplemented
        return (
            self.hermes_home_was_set,
            self.hermes_home,
            self.home,
        ) == (
            other.hermes_home_was_set,
            other.hermes_home,
            other.home,
        )

    def __hash__(self) -> int:
        return hash((self.hermes_home_was_set, self.hermes_home, self.home))

    @classmethod
    def capture(cls, environ: Mapping[str, str]) -> OriginalCollectionEnvironment:
        """Capture path metadata only; never read configuration or credentials."""
        hermes_home_was_set = "HERMES_HOME" in environ
        return cls(
            hermes_home_was_set=hermes_home_was_set,
            hermes_home=environ.get("HERMES_HOME") if hermes_home_was_set else None,
            home=environ.get("HOME"),
        )
