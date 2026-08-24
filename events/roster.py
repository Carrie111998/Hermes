"""Canonical roster of event-bus subscribers — loader for subscriber_roster.json.

WHY THIS EXISTS
---------------
The set of subscriber ids was maintained by hand in THREE places, and two of
them had already drifted with operator-visible consequences:

  1. ``events/gateway_integration.py`` startup() — the real registrations.
  2. ``scripts/event_bus_retention.py`` ``EXPECTED_SUBSCRIBERS`` — drifted in
     BOTH directions and was fixed on 2026-08-23 (af05110a). It was MISSING
     ``jobflow-dispatcher``, so the nightly sweep told the operator to
     ``--prune-cursor`` a LIVE subscriber every night; and it listed
     ``scribe-realtime``, whose registration had been commented out since
     2026-07-18, as live.
  3. ``hermes_cli/events_doctor.py`` ``REQUIRED_SUBSCRIBERS`` — listed
     ``telegram-mirror`` after its 2026-04-28 retirement, producing a
     permanent false FAIL on every doctor run until 2026-07-12.

``events/formatting.py`` solved the identical problem for ``EVENT_TYPE_EMOJI``
after four drifts, by DERIVING the table from a field on the source of truth
so there was no second table left to sync. Same principle here: one file, many
readers.

WHY A JSON DATA FILE AND NOT A PYTHON CONSTANT
----------------------------------------------
``scripts/event_bus_retention.py`` is a ``--no-agent`` cron with a stdlib-only
import list, which is what makes it safe to run while agent-src is being
edited by concurrent sessions. Importing ``events.roster`` would pull in
``events/__init__.py`` -> ``events.bus`` + ``events.schema`` and hand a nightly
retention job a hard dependency on another repo's Python parsing cleanly. A
JSON read cannot be broken by a syntax error three modules away, needs no
``sys.path`` surgery, and works under any interpreter. So the retention script
reads ``subscriber_roster.json`` by absolute path with ``json.load`` and never
imports this module; in-process consumers use the helpers here.

WHY NOT AST-PARSE startup() INSTEAD
-----------------------------------
Because ``_registry.register(AuditLogger(_bus))`` does not contain the string
``audit-logger``. Resolving a register() call to a subscriber_id from source
alone means either importing the class (defeats the point) or a second
heuristic AST pass over every subscriber module — and it still cannot see
``devflow-bridge``, which subscribed directly outside startup(). The runtime
check in ``gateway_integration._verify_subscriber_roster()`` reads
``subscriber_id`` off the constructed objects, so it is exact by construction
and sees conditional registrations (jobflow-dispatcher's try/except) as they
actually resolved on this boot.

VALIDATION
----------
``load_roster`` raises ``RosterError`` rather than returning a half-parsed
roster: every consumer here treats an unreadable roster as fail-closed (the
retention script refuses to prune, events_doctor FAILs the check), which is
strictly safer than acting on a guess about which cursors are live.

This module imports nothing from ``events`` — keep it that way so it stays
loadable from a bare file path if a future consumer needs that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Tuple

ROSTER_PATH = Path(__file__).with_name("subscriber_roster.json")

LIVE = "live"
RETIRED = "retired"
_STATUSES = (LIVE, RETIRED)


class RosterError(Exception):
    """The roster file is missing, malformed, or self-inconsistent."""


@dataclass(frozen=True)
class SubscriberEntry:
    id: str
    status: str
    core: bool = False
    since: Optional[str] = None
    cursor_frozen_at_rowid: Optional[int] = None
    note: str = ""

    @property
    def is_live(self) -> bool:
        return self.status == LIVE

    @property
    def is_retired(self) -> bool:
        return self.status == RETIRED


@dataclass(frozen=True)
class Roster:
    entries: Tuple[SubscriberEntry, ...]
    # The file this roster was actually READ from, when it came from one.
    # Consumers that name a path in an operator-facing message must use this
    # rather than the module-level ROSTER_PATH: a drift alert that says
    # "Fix <path>" has to name the file it really loaded, not the default it
    # assumed. Caught 2026-08-23 by the end-to-end scratch-bus test, which
    # loaded a drifted roster from a tempdir and got told to fix the shipping
    # one.
    source: Optional[Path] = None

    @property
    def live(self) -> FrozenSet[str]:
        """Ids that startup() must have registered on this boot."""
        return frozenset(e.id for e in self.entries if e.is_live)

    @property
    def retired(self) -> Dict[str, SubscriberEntry]:
        """Retired ids -> entry.  Their cursors are deliberately frozen."""
        return {e.id: e for e in self.entries if e.is_retired}

    @property
    def known(self) -> FrozenSet[str]:
        """Every id the roster accounts for — live or deliberately retired.

        This is the set a cursor row may legitimately belong to; anything else
        in ``subscriber_cursors`` is genuinely unknown.
        """
        return frozenset(e.id for e in self.entries)

    @property
    def core(self) -> Tuple[str, ...]:
        """Live ids whose cursor absence is a doctor-level FAIL, in file order."""
        return tuple(e.id for e in self.entries if e.core)

    def get(self, subscriber_id: str) -> Optional[SubscriberEntry]:
        for e in self.entries:
            if e.id == subscriber_id:
                return e
        return None


def _entry_from(raw: Any, index: int) -> SubscriberEntry:
    where = f"subscribers[{index}]"
    if not isinstance(raw, dict):
        raise RosterError(f"{where} is {type(raw).__name__}, expected an object")

    sid = raw.get("id")
    if not isinstance(sid, str) or not sid.strip():
        raise RosterError(f"{where} has no non-empty string 'id'")

    status = raw.get("status")
    if status not in _STATUSES:
        raise RosterError(
            f"{where} ('{sid}') has status {status!r}; expected one of {_STATUSES}"
        )

    core = raw.get("core", False)
    if not isinstance(core, bool):
        raise RosterError(f"{where} ('{sid}') has non-boolean 'core': {core!r}")
    if core and status != LIVE:
        # This is exactly the telegram-mirror false-FAIL: a required-cursor
        # check naming a subscriber that no longer runs.
        raise RosterError(
            f"{where} ('{sid}') is marked core but status is '{status}'; only a "
            "live subscriber can be required to have a cursor"
        )

    frozen = raw.get("cursor_frozen_at_rowid")
    if frozen is not None and not isinstance(frozen, int):
        raise RosterError(
            f"{where} ('{sid}') has non-integer 'cursor_frozen_at_rowid': {frozen!r}"
        )
    if status == LIVE and frozen is not None:
        raise RosterError(
            f"{where} ('{sid}') is live but declares cursor_frozen_at_rowid; a live "
            "subscriber's cursor is expected to move"
        )

    since = raw.get("since")
    if since is not None and not isinstance(since, str):
        raise RosterError(f"{where} ('{sid}') has non-string 'since': {since!r}")
    if status == RETIRED and not since:
        raise RosterError(f"{where} ('{sid}') is retired but carries no 'since' date")

    note = raw.get("note", "")
    if not isinstance(note, str):
        raise RosterError(f"{where} ('{sid}') has non-string 'note'")

    return SubscriberEntry(
        id=sid,
        status=status,
        core=core,
        since=since,
        cursor_frozen_at_rowid=frozen,
        note=note,
    )


def parse_roster(data: Any, *, origin: str = "<data>") -> Roster:
    """Validate an already-decoded roster document.  Raises RosterError."""
    if not isinstance(data, dict):
        raise RosterError(f"{origin}: top level is {type(data).__name__}, expected object")
    raw_subs = data.get("subscribers")
    if not isinstance(raw_subs, list) or not raw_subs:
        raise RosterError(f"{origin}: 'subscribers' must be a non-empty list")

    entries = tuple(_entry_from(raw, i) for i, raw in enumerate(raw_subs))

    seen: Dict[str, int] = {}
    for i, e in enumerate(entries):
        if e.id in seen:
            raise RosterError(
                f"{origin}: duplicate subscriber id '{e.id}' at subscribers"
                f"[{seen[e.id]}] and subscribers[{i}]"
            )
        seen[e.id] = i
    return Roster(entries=entries)


def load_roster(path: Optional[Path] = None) -> Roster:
    """Load + validate the canonical roster.  Raises RosterError on any problem."""
    p = Path(path) if path is not None else ROSTER_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RosterError(f"cannot read subscriber roster {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RosterError(f"subscriber roster {p} is not valid JSON: {exc}") from exc
    parsed = parse_roster(data, origin=str(p))
    return Roster(entries=parsed.entries, source=p)
