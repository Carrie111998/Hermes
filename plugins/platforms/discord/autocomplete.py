"""Discord slash-command autocomplete option fidelity (feature I3).

Pure logic helpers for building Discord autocomplete choices and
normalizing a clicked suggestion back to a canonical option, so values
round-trip exactly through the picker.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "AutocompleteChoice",
    "AutocompleteError",
    "MAX_CHOICES",
    "build_choices",
    "normalize_clicked_value",
    "roundtrip_value",
]

# Discord's hard limit on the number of autocomplete choices per response.
MAX_CHOICES = 25


class AutocompleteError(ValueError):
    """Raised when a clicked autocomplete value cannot be normalized."""


@dataclass(frozen=True)
class AutocompleteChoice:
    """A single Discord autocomplete choice.

    ``name`` is what the user sees in the picker and ``value`` is what gets
    submitted; for option fidelity they are always identical.
    """

    name: str
    value: str


def build_choices(
    options: list[str], *, max_choices: int = MAX_CHOICES, query: str = ""
) -> list[AutocompleteChoice]:
    """Build Discord autocomplete choices from canonical option strings.

    Every option is stripped; only options containing the query as a
    case-insensitive substring are kept (empty query keeps everything); and
    the result is clamped to ``max_choices``. Each choice has ``name ==
    value`` so the picker round-trips exactly.
    """
    normalized = [option.strip() for option in options]
    needle = query.strip().lower()
    if needle:
        matched = [option for option in normalized if needle in option.lower()]
    else:
        matched = normalized
    choices = [AutocompleteChoice(name=option, value=option) for option in matched]
    return choices[:max_choices]


def normalize_clicked_value(clicked: str, options: list[str]) -> str:
    """Map a clicked slash-command suggestion back to a canonical option.

    Resolution order: exact match wins, then case-insensitive match, then the
    clicked value itself (trimmed). Raises :class:`AutocompleteError` when the
    clicked value is empty (or whitespace only).
    """
    clicked = clicked.strip()
    if not clicked:
        raise AutocompleteError("clicked autocomplete value must not be empty")
    for option in options:
        if option == clicked:
            return option
    lowered = clicked.lower()
    for option in options:
        if option.lower() == lowered:
            return option
    return clicked


def roundtrip_value(model_value: str) -> str:
    """Return the value unchanged.

    Identity by design: the value submitted through the picker must equal the
    canonical model value exactly.
    """
    return model_value
