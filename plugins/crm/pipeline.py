"""Keep-in-touch computation engine for the personal CRM plugin.

This is the "pipeline" that turns raw contacts into an actionable picture:
who is overdue for a touchpoint, who is coming due, and whose birthday is
around the corner. Everything here is pure and takes an explicit ``now`` so
the board, digest, and due list are deterministic and unit-testable.

Status model (mirrors a Dex keep-in-touch board):

* ``overdue``   — a cadence is set and the next-due date is in the past
* ``due_soon``  — next-due date is within ``soon_days`` of ``now``
* ``on_track``  — a cadence is set and the next touchpoint is further out
* ``no_cadence``— no keep-in-touch cadence configured for this contact
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from plugins.crm.models import Contact, ImportantDate


STATUS_OVERDUE = "overdue"
STATUS_DUE_SOON = "due_soon"
STATUS_ON_TRACK = "on_track"
STATUS_NO_CADENCE = "no_cadence"

# Board column order, most-urgent first.
BOARD_COLUMNS = (STATUS_OVERDUE, STATUS_DUE_SOON, STATUS_ON_TRACK, STATUS_NO_CADENCE)

_STATUS_LABELS = {
    STATUS_OVERDUE: "Overdue",
    STATUS_DUE_SOON: "Due soon",
    STATUS_ON_TRACK: "On track",
    STATUS_NO_CADENCE: "No cadence",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_contact(contact: Contact | dict[str, Any]) -> Contact:
    return contact if isinstance(contact, Contact) else Contact.from_dict(contact)


@dataclass
class KeepInTouchStatus:
    """Computed keep-in-touch state for a single contact."""

    contact_id: str
    name: str
    status: str
    keep_in_touch_days: Optional[int]
    last_contacted_at: Optional[datetime]
    next_due_at: Optional[datetime]
    days_overdue: Optional[int]  # positive = overdue, negative = days until due
    tags: list[str]

    @property
    def status_label(self) -> str:
        return _STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> dict[str, Any]:
        def _iso(value: Optional[datetime]) -> Optional[str]:
            if value is None:
                return None
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        return {
            "contact_id": self.contact_id,
            "name": self.name,
            "status": self.status,
            "status_label": self.status_label,
            "keep_in_touch_days": self.keep_in_touch_days,
            "last_contacted_at": _iso(self.last_contacted_at),
            "next_due_at": _iso(self.next_due_at),
            "days_overdue": self.days_overdue,
            "tags": list(self.tags),
        }


def compute_status(
    contact: Contact | dict[str, Any],
    *,
    now: Optional[datetime] = None,
    soon_days: int = 3,
) -> KeepInTouchStatus:
    """Compute the keep-in-touch status for a single contact.

    A contact that has never been contacted but has a cadence is anchored to
    its ``created_at`` (falling back to ``now``), so brand-new contacts surface
    as due rather than silently sitting on_track forever.
    """
    now = now or _utc_now()
    c = _as_contact(contact)
    cadence = c.keep_in_touch_days

    if not cadence:
        return KeepInTouchStatus(
            contact_id=c.contact_id,
            name=c.name,
            status=STATUS_NO_CADENCE,
            keep_in_touch_days=None,
            last_contacted_at=c.last_contacted_at,
            next_due_at=None,
            days_overdue=None,
            tags=list(c.tags),
        )

    anchor = c.last_contacted_at or c.created_at or now
    try:
        next_due = anchor + timedelta(days=cadence)
    except OverflowError:
        # A hand-edited store can carry a cadence past datetime's range;
        # clamp instead of crashing every read command.
        next_due = datetime.max.replace(tzinfo=timezone.utc)
    # Whole-day granularity so "due today" reads cleanly regardless of time.
    delta_days = (now.date() - next_due.date()).days

    if delta_days >= 0:
        status = STATUS_OVERDUE
    elif -delta_days <= soon_days:
        status = STATUS_DUE_SOON
    else:
        status = STATUS_ON_TRACK

    return KeepInTouchStatus(
        contact_id=c.contact_id,
        name=c.name,
        status=status,
        keep_in_touch_days=cadence,
        last_contacted_at=c.last_contacted_at,
        next_due_at=next_due,
        days_overdue=delta_days,
        tags=list(c.tags),
    )


def _sort_key(item: KeepInTouchStatus) -> tuple:
    # Within a column, most-overdue (largest days_overdue) first; ties by name.
    overdue = item.days_overdue if item.days_overdue is not None else -10**9
    return (-overdue, item.name.lower())


def compute_board(
    contacts: Iterable[Contact | dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    soon_days: int = 3,
    tag: Optional[str] = None,
) -> dict[str, list[KeepInTouchStatus]]:
    """Group contacts into keep-in-touch board columns.

    Returns a dict keyed by the four status columns (always present, possibly
    empty), each a list of :class:`KeepInTouchStatus` sorted most-urgent first.
    """
    now = now or _utc_now()
    columns: dict[str, list[KeepInTouchStatus]] = {col: [] for col in BOARD_COLUMNS}
    tag_filter = (tag or "").strip().lower() or None
    for contact in contacts:
        c = _as_contact(contact)
        if tag_filter and tag_filter not in {t.lower() for t in c.tags}:
            continue
        status = compute_status(c, now=now, soon_days=soon_days)
        columns[status.status].append(status)
    for col in columns.values():
        col.sort(key=_sort_key)
    return columns


def due_contacts(
    contacts: Iterable[Contact | dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    soon_days: int = 3,
    include_due_soon: bool = True,
    tag: Optional[str] = None,
) -> list[KeepInTouchStatus]:
    """Return contacts needing outreach (overdue first, then due_soon)."""
    board = compute_board(contacts, now=now, soon_days=soon_days, tag=tag)
    result = list(board[STATUS_OVERDUE])
    if include_due_soon:
        result += list(board[STATUS_DUE_SOON])
    return result


def _next_occurrence(d: ImportantDate, today: date) -> date:
    """Return the next calendar occurrence of a month/day on or after ``today``."""
    year = today.year
    month, day = d.month, d.day
    # Clamp Feb 29 to Feb 28 in non-leap years so we never build an invalid date.
    if month == 2 and day == 29:
        try:
            date(year, 2, 29)
        except ValueError:
            day = 28
    candidate = date(year, month, day)
    if candidate < today:
        year += 1
        day = d.day
        if month == 2 and day == 29:
            try:
                date(year, 2, 29)
            except ValueError:
                day = 28
        candidate = date(year, month, day)
    return candidate


@dataclass
class UpcomingDate:
    contact_id: str
    name: str
    label: str
    date: date
    days_until: int
    turns: Optional[int]  # age/anniversary count if a year is known
    note: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "name": self.name,
            "label": self.label,
            "date": self.date.isoformat(),
            "days_until": self.days_until,
            "turns": self.turns,
            "note": self.note,
        }


def upcoming_dates(
    contacts: Iterable[Contact | dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    within_days: int = 30,
    tag: Optional[str] = None,
) -> list[UpcomingDate]:
    """Return important dates (birthdays, anniversaries) within ``within_days``."""
    now = now or _utc_now()
    today = now.date()
    horizon = today + timedelta(days=within_days)
    tag_filter = (tag or "").strip().lower() or None
    results: list[UpcomingDate] = []
    for contact in contacts:
        c = _as_contact(contact)
        if tag_filter and tag_filter not in {t.lower() for t in c.tags}:
            continue
        for d in c.important_dates:
            occ = _next_occurrence(d, today)
            if occ > horizon:
                continue
            turns = (occ.year - d.year) if d.year else None
            results.append(
                UpcomingDate(
                    contact_id=c.contact_id,
                    name=c.name,
                    label=d.label,
                    date=occ,
                    days_until=(occ - today).days,
                    turns=turns,
                    note=d.note,
                )
            )
    results.sort(key=lambda u: (u.days_until, u.name.lower()))
    return results


def render_digest(
    contacts: Iterable[Contact | dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    soon_days: int = 3,
    dates_within_days: int = 14,
    tag: Optional[str] = None,
    silent_token: str = "[SILENT]",
    silent_if_empty: bool = False,
) -> str:
    """Render a human-readable keep-in-touch digest for delivery.

    Designed to be dropped straight into ``hermes cron ... --deliver`` so a
    daily nudge lists who to reach out to and whose birthday is coming up.
    When ``silent_if_empty`` is set and there is nothing to report, returns
    ``silent_token`` (the ``[SILENT]`` convention used by Hermes automations
    to suppress empty notifications).

    ``tag`` filters the whole digest — both the due list and the upcoming
    dates — so a tagged digest never leaks other groups' birthdays and
    ``silent_if_empty`` fires correctly for quiet tags.
    """
    now = now or _utc_now()
    contacts = [_as_contact(c) for c in contacts]
    tag_filter = (tag or "").strip().lower() or None
    if tag_filter:
        contacts = [
            c for c in contacts if tag_filter in {t.lower() for t in c.tags}
        ]
    due = due_contacts(contacts, now=now, soon_days=soon_days)
    dates = upcoming_dates(contacts, now=now, within_days=dates_within_days)

    if not due and not dates:
        if silent_if_empty:
            return silent_token
        return "✅ You're all caught up — no one is due for a check-in."

    lines: list[str] = ["🤝 Keep in touch"]
    overdue = [d for d in due if d.status == STATUS_OVERDUE]
    due_soon = [d for d in due if d.status == STATUS_DUE_SOON]

    if overdue:
        lines.append("")
        lines.append(f"⏰ Overdue ({len(overdue)}):")
        for item in overdue:
            span = (
                f"{item.days_overdue}d overdue"
                if item.days_overdue and item.days_overdue > 0
                else "due today"
            )
            lines.append(f"  • {item.name} — {span}")

    if due_soon:
        lines.append("")
        lines.append(f"🔜 Due soon ({len(due_soon)}):")
        for item in due_soon:
            days = -(item.days_overdue or 0)
            when = "tomorrow" if days == 1 else f"in {days}d"
            lines.append(f"  • {item.name} — {when}")

    if dates:
        lines.append("")
        lines.append(f"🎂 Upcoming dates ({len(dates)}):")
        for d in dates:
            when = "today" if d.days_until == 0 else (
                "tomorrow" if d.days_until == 1 else f"in {d.days_until}d"
            )
            suffix = f" (turning {d.turns})" if d.turns else ""
            lines.append(f"  • {d.name} — {d.label} {when}{suffix}")

    return "\n".join(lines)


__all__ = [
    "BOARD_COLUMNS",
    "KeepInTouchStatus",
    "STATUS_DUE_SOON",
    "STATUS_NO_CADENCE",
    "STATUS_ON_TRACK",
    "STATUS_OVERDUE",
    "UpcomingDate",
    "compute_board",
    "compute_status",
    "due_contacts",
    "render_digest",
    "upcoming_dates",
]
