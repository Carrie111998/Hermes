"""Normalized models for the personal CRM ("keep-in-touch") pipeline plugin.

The CRM plugin is a Dex-style personal relationship manager. Its two core
records are :class:`Contact` (a person you want to stay in touch with, plus a
keep-in-touch cadence and any important dates) and :class:`Interaction` (a
timestamped touchpoint — a call, email, meeting, message, or note). The
keep-in-touch math lives in :mod:`plugins.crm.pipeline`; these models are pure
data with ``from_dict``/``to_dict`` round-tripping so they persist cleanly in
the JSON store.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, cast


InteractionKind = Literal["call", "email", "meeting", "message", "note", "other"]

VALID_INTERACTION_KINDS: frozenset[str] = frozenset(
    {"call", "email", "meeting", "message", "note", "other"}
)

# Human-friendly cadence presets → number of days between touchpoints. Callers
# may also pass a raw integer number of days.
CADENCE_PRESETS: dict[str, int] = {
    "weekly": 7,
    "biweekly": 14,
    "fortnightly": 14,
    "monthly": 30,
    "quarterly": 90,
    "biannual": 182,
    "semiannual": 182,
    "yearly": 365,
    "annual": 365,
}


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _clean_dict(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _str_list(value: Any) -> list[str]:
    """Coerce ``value`` into a de-duplicated list of trimmed non-empty strings.

    Accepts a list/tuple or a single comma-separated string, so both
    ``["a", "b"]`` and ``"a, b"`` normalize identically.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    seen: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def slugify_contact_id(name: str, *, existing: set[str] | None = None) -> str:
    """Derive a stable, filesystem-safe contact id from a display name.

    Lowercases, strips accents, and collapses non-alphanumerics to single
    hyphens. When ``existing`` ids are supplied, a numeric suffix is appended
    to avoid collisions (``jane-doe`` → ``jane-doe-2``).
    """
    normalized = unicodedata.normalize("NFKD", str(name or ""))
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    if not slug:
        slug = "contact"
    if not existing:
        return slug
    if slug not in existing:
        return slug
    suffix = 2
    while f"{slug}-{suffix}" in existing:
        suffix += 1
    return f"{slug}-{suffix}"


def normalize_cadence(value: Any) -> int | None:
    """Return a cadence as an integer number of days, or ``None`` for no cadence.

    Accepts preset names (``"monthly"``), integer-like values (``30``,
    ``"30"``), and the empty/``"none"``/``"off"`` sentinels which clear the
    cadence. Raises ``ValueError`` for unrecognized or non-positive values.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        raise ValueError("cadence must be a preset name or number of days")
    if isinstance(value, (int, float)):
        days = int(value)
        if days <= 0:
            raise ValueError("cadence days must be a positive integer")
        return days
    text = str(value).strip().lower()
    if text in {"", "none", "off", "never", "0"}:
        return None
    if text in CADENCE_PRESETS:
        return CADENCE_PRESETS[text]
    # Allow suffixed forms like "30d", "6w", "3m", "1y".
    match = re.fullmatch(r"(\d+)\s*([dwmy]?)", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2) or "d"
        multiplier = {"d": 1, "w": 7, "m": 30, "y": 365}[unit]
        days = amount * multiplier
        if days <= 0:
            raise ValueError("cadence days must be a positive integer")
        return days
    raise ValueError(
        f"Unrecognized cadence {value!r}. Use a number of days or one of: "
        + ", ".join(sorted(CADENCE_PRESETS))
    )


@dataclass
class ImportantDate:
    """A recurring or one-off date attached to a contact (birthday, etc.)."""

    label: str
    month: int
    day: int
    year: int | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        self.label = str(self.label or "").strip() or "date"
        self.month = int(self.month)
        self.day = int(self.day)
        if not 1 <= self.month <= 12:
            raise ValueError("ImportantDate.month must be between 1 and 12.")
        if not 1 <= self.day <= 31:
            raise ValueError("ImportantDate.day must be between 1 and 31.")
        if self.year is not None:
            self.year = int(self.year)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ImportantDate":
        return cls(
            label=payload.get("label") or payload.get("kind") or "date",
            month=int(payload.get("month")),
            day=int(payload.get("day")),
            year=payload.get("year"),
            note=payload.get("note"),
        )

    @classmethod
    def parse(cls, label: str, value: str, note: str | None = None) -> "ImportantDate":
        """Parse a ``MM-DD`` or ``YYYY-MM-DD`` string into an ImportantDate."""
        text = str(value or "").strip()
        parts = re.split(r"[-/.]", text)
        year: int | None = None
        if len(parts) == 3:
            first = int(parts[0])
            if first > 31:  # YYYY-MM-DD
                year, month, day = first, int(parts[1]), int(parts[2])
            else:  # MM-DD-YYYY
                month, day, year = first, int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            month, day = int(parts[0]), int(parts[1])
        else:
            raise ValueError(f"Could not parse date {value!r}; use MM-DD or YYYY-MM-DD.")
        return cls(label=label, month=month, day=day, year=year, note=note)

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(
            {
                "label": self.label,
                "month": self.month,
                "day": self.day,
                "year": self.year,
                "note": self.note,
            }
        )


@dataclass
class Contact:
    """A person tracked in the personal CRM."""

    contact_id: str
    name: str
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    company: str | None = None
    title: str | None = None
    location: str | None = None
    tags: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    notes: str | None = None
    keep_in_touch_days: int | None = None
    last_contacted_at: datetime | None = None
    important_dates: list[ImportantDate] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.contact_id.strip():
            raise ValueError("Contact.contact_id is required.")
        if not self.name.strip():
            raise ValueError("Contact.name is required.")
        self.emails = _str_list(self.emails)
        self.phones = _str_list(self.phones)
        self.tags = _str_list(self.tags)
        self.socials = {
            str(k).strip(): str(v).strip()
            for k, v in dict(self.socials or {}).items()
            if str(k).strip() and str(v).strip()
        }
        if self.keep_in_touch_days is not None:
            self.keep_in_touch_days = int(self.keep_in_touch_days)
            if self.keep_in_touch_days <= 0:
                raise ValueError("Contact.keep_in_touch_days must be positive or None.")
        self.last_contacted_at = _parse_datetime(self.last_contacted_at)
        self.created_at = _parse_datetime(self.created_at)
        self.updated_at = _parse_datetime(self.updated_at)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Contact":
        dates = [
            ImportantDate.from_dict(item)
            for item in (payload.get("important_dates") or [])
        ]
        return cls(
            contact_id=str(payload.get("contact_id") or payload.get("id") or "").strip(),
            name=str(payload.get("name") or "").strip(),
            emails=payload.get("emails"),
            phones=payload.get("phones"),
            company=payload.get("company"),
            title=payload.get("title"),
            location=payload.get("location"),
            tags=payload.get("tags"),
            socials=payload.get("socials") or {},
            notes=payload.get("notes"),
            keep_in_touch_days=payload.get("keep_in_touch_days"),
            last_contacted_at=payload.get("last_contacted_at"),
            important_dates=dates,
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(
            {
                "contact_id": self.contact_id,
                "name": self.name,
                "emails": self.emails or None,
                "phones": self.phones or None,
                "company": self.company,
                "title": self.title,
                "location": self.location,
                "tags": self.tags or None,
                "socials": self.socials or None,
                "notes": self.notes,
                "keep_in_touch_days": self.keep_in_touch_days,
                "last_contacted_at": _serialize_datetime(self.last_contacted_at),
                "important_dates": [d.to_dict() for d in self.important_dates] or None,
                "created_at": _serialize_datetime(self.created_at),
                "updated_at": _serialize_datetime(self.updated_at),
            }
        )


@dataclass
class Interaction:
    """A single timestamped touchpoint with a contact."""

    interaction_id: str
    contact_id: str
    kind: InteractionKind
    occurred_at: datetime
    summary: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.interaction_id.strip():
            raise ValueError("Interaction.interaction_id is required.")
        if not self.contact_id.strip():
            raise ValueError("Interaction.contact_id is required.")
        kind = str(self.kind or "").strip().lower()
        if kind not in VALID_INTERACTION_KINDS:
            raise ValueError(
                "Interaction.kind must be one of: "
                + ", ".join(sorted(VALID_INTERACTION_KINDS))
            )
        self.kind = cast(InteractionKind, kind)
        occurred = _parse_datetime(self.occurred_at)
        if occurred is None:
            raise ValueError("Interaction.occurred_at is required.")
        self.occurred_at = occurred
        self.created_at = _parse_datetime(self.created_at)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Interaction":
        return cls(
            interaction_id=str(
                payload.get("interaction_id") or payload.get("id") or ""
            ).strip(),
            contact_id=str(payload.get("contact_id") or "").strip(),
            kind=payload.get("kind") or "note",
            occurred_at=payload.get("occurred_at") or payload.get("occurredAt"),
            summary=payload.get("summary"),
            created_at=payload.get("created_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(
            {
                "interaction_id": self.interaction_id,
                "contact_id": self.contact_id,
                "kind": self.kind,
                "occurred_at": _serialize_datetime(self.occurred_at),
                "summary": self.summary,
                "created_at": _serialize_datetime(self.created_at),
            }
        )


__all__ = [
    "CADENCE_PRESETS",
    "VALID_INTERACTION_KINDS",
    "Contact",
    "ImportantDate",
    "Interaction",
    "InteractionKind",
    "normalize_cadence",
    "slugify_contact_id",
]
