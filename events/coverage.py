"""Coverage manifest for the EventType-keyed lookup tables.

Adding a member to :class:`events.schema.EventType` is not a one-file change.
The member has to appear in every *sibling lookup table* that is contracted to
be TOTAL over the enum — at minimum ``events.formatting.EVENT_TYPE_EMOJI`` (the
notification icon) and ``events.routing_policy._POLICY`` (where the event goes
and whether it pages the operator). Nothing in the language enforces the pairing,
and this table has drifted four times on record:

  2026-04-27  13 entries missing (approval_request, apply_packet,
              critic_proposal, watchdog_*, curator_daily, devflow.run_*)
  2026-04-27  the same table, again, after the first fix went dangling
  2026-05-29  gateway_started/stopped, backend_contract_drift, agent_loop_fault
  2026-08-11  all 12 DevFlow Delegation Plane members missing from
              EVENT_TYPE_EMOJI (79 enum members vs 67 icon entries), while
              routing_policy._POLICY was complete

ruff cannot backstop this. The keys are attribute accesses (``EventType.X``);
F601 only catches repeated *literal* keys and F602 only bare *names*, so neither
rule reaches an attribute key at all — and this repo's ``select`` is narrow
enough (``PLW1514``) that a green ``ruff check`` proves nothing here.

Why this is a check and not an import-time assertion
----------------------------------------------------
Both consumers already degrade gracefully on a miss: ``event_icon()`` returns
``""`` (a double-space gap in the header) and ``classify()`` falls back to
WARN-on-alerts. Raising at import would convert a cosmetic notification defect
into a gateway that will not boot — a self-inflicted outage in the very system
whose job is to report outages. So enforcement runs where a *developer* is, not
where the gateway is:

  * ``python -m events.coverage`` — exits non-zero with the FULL missing set.
    Wired into ``.pre-commit-config.yaml``, so it fires when the member is
    added, not on some later test run.
  * ``tests/events/test_event_type_coverage.py`` — the same checks as tests.

…with one non-fatal exception. All three of those are commit-time, so all three
are bypassed by ``git commit --no-verify`` and by any checkout where pre-commit
was never installed, and none of them make a *running* gateway say anything.
:func:`log_missing_members` closes that residual gap: each required-total table
calls it at the bottom of its own module and gets ONE ``logger.error`` naming
every absent member. It reports; it never repairs — see its docstring.

Discovery, not just a hand-written list
---------------------------------------
:func:`discover_tables` walks the whole ``events`` package and finds every
module-level mapping keyed entirely by ``EventType``. Anything it finds that is
not named in :data:`MANIFEST` is reported as *unclassified* — so a new sibling
table added later is covered automatically, and whoever adds it has to say once
whether it is total or deliberately partial. Only *mappings* are discovered: a
total contract is a property of a *lookup* table, whereas the ``frozenset``s in
this package (``JOBFLOW_DEMOTE_TYPES``, ``_NEVER_CONSUME``, the ``outcomes``
verdict sets) are membership filters that are partial by construction.

``collections.abc.Mapping``, not ``dict``, deliberately: ``EVENT_TYPE_EMOJI`` is
a ``MappingProxyType`` (a read-only view derived from the enum), and a
``MappingProxyType`` is not a ``dict``. Testing for ``dict`` dropped discovery
from 5 tables to 4 and quietly took the flagship table out of the unclassified
safety net — a blind spot in the very check whose job is to have none.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Mapping as _MappingABC
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from events.schema import EventType

__all__ = [
    "TableSpec",
    "DiscoveredTable",
    "REQUIRED_TOTAL",
    "KNOWN_PARTIAL",
    "MANIFEST",
    "missing_members",
    "log_missing_members",
    "coverage_gaps",
    "discover_tables",
    "unclassified_tables",
    "unimportable_modules",
    "format_report",
    "main",
]


@dataclass(frozen=True)
class TableSpec:
    """One EventType-keyed table and whether it is contracted to be total."""

    module: str
    attribute: str
    total: bool
    why: str

    @property
    def qualname(self) -> str:
        return f"{self.module}.{self.attribute}"

    def resolve(self) -> Mapping[EventType, object]:
        """Import the owning module and return the live table."""
        mod = importlib.import_module(self.module)
        try:
            return getattr(mod, self.attribute)
        except AttributeError as exc:  # pragma: no cover - manifest typo guard
            raise AttributeError(
                f"{self.qualname} is in the events.coverage MANIFEST but no "
                f"longer exists. Update MANIFEST (the table was renamed or "
                f"removed)."
            ) from exc


# Tables that MUST have an entry for every EventType member. A miss here is a
# real defect in delivered notifications, not merely a red test.
#
# TOTAL BY CONSTRUCTION vs TOTAL BY CHECK — the two entries below are no longer
# the same kind of claim, and the difference matters when reading a green run:
#
#   EVENT_TYPE_EMOJI  is TOTAL BY CONSTRUCTION. It is a MappingProxyType built
#       as ``{et: et.icon for et in EventType}``, and ``EventType.__init__``
#       rejects a missing (TypeError) or blank (ValueError) icon at class-
#       creation time. It CANNOT be partial while events.schema imports at all,
#       so its line in the report is a tautology, kept deliberately: it is the
#       regression detector for the derivation itself. If someone ever
#       re-introduces a hand-maintained icon dict — the exact shape that drifted
#       four times — this entry goes back to being a real check on the day it
#       lands, with no edit to this file.
#
#   _POLICY           is TOTAL BY CHECK. It is an ordinary partial dict with a
#       genuine runtime fallback in classify(), so this is the entry that can
#       actually fail, and the one the pre-commit hook exists for. Do NOT
#       "harden" it by back-filling unmapped types at import time: coverage.py
#       reads _POLICY after import, so a back-fill makes this check report
#       79/79 on a table that IS missing an entry. That false green is why the
#       2026-08-11 icon-hardening branch (archive/eventtype-icon-as-enum-field-
#       20260811) was rejected as-is rather than landed.
REQUIRED_TOTAL: Tuple[TableSpec, ...] = (
    TableSpec(
        "events.formatting",
        "EVENT_TYPE_EMOJI",
        total=True,
        why=(
            "event_icon() renders the Telegram/WhatsApp header's visual scan "
            "token; a missing entry used to leave a double-space gap. Now "
            "total by construction (derived from EventType.icon) — a failure "
            "here means the derivation was replaced by a hand-maintained dict"
        ),
    ),
    TableSpec(
        "events.routing_policy",
        "_POLICY",
        total=True,
        why=(
            "classify() falls back to WARN-on-watchdog_alerts for an unmapped "
            "type, so the event lands in the wrong topic and its WhatsApp "
            "escalation is decided by accident rather than by design"
        ),
    ),
)

# Tables keyed by EventType that are deliberately PARTIAL. Listed so that
# discovery can tell "intentionally a subset" apart from "nobody has looked at
# this yet" — an unlisted table fails unclassified_tables().
KNOWN_PARTIAL: Tuple[TableSpec, ...] = (
    TableSpec(
        "events.formatting",
        "_RECOVERY_WHEN",
        total=False,
        why=(
            "opt-in: only the few types whose payload can mean 'recovered' "
            "(gateway_health up, code_drift resolved, probe transition to "
            "healthy) need a rule"
        ),
    ),
    TableSpec(
        "events.formatting",
        "WHATSAPP_TITLE_BY_EVENT",
        total=False,
        why=(
            "opt-in: only types that actually escalate to WhatsApp need a "
            "custom title; the rest never reach a WhatsApp message"
        ),
    ),
    TableSpec(
        "events.subscribers.memory_writer",
        "MEMORY_ROUTING",
        total=False,
        why=(
            "opt-in: only types worth persisting to a memory file are routed; "
            "writing every event would flood MEMORY.md"
        ),
    ),
)

MANIFEST: Tuple[TableSpec, ...] = REQUIRED_TOTAL + KNOWN_PARTIAL


@dataclass(frozen=True)
class DiscoveredTable:
    """An EventType-keyed dict found by walking the ``events`` package.

    ``qualnames`` holds every ``module.attribute`` path that exposes this same
    dict object, so a re-export does not read as a second, unclassified table.
    """

    qualnames: Tuple[str, ...]
    size: int
    missing: Tuple[str, ...]

    @property
    def primary(self) -> str:
        return self.qualnames[0]


def missing_members(table: Mapping[EventType, object]) -> List[str]:
    """Return the type strings absent from ``table``, in enum declaration order.

    A key mapped to a falsy value (``""``, ``None``) counts as missing: an empty
    icon is exactly the defect the 2026-04-19 SECRET_DETECTED fix was about.
    """
    return [et.type_string for et in EventType if not table.get(et)]


def log_missing_members(
    table: Mapping[EventType, object],
    qualname: str,
    logger: Optional[logging.Logger] = None,
) -> Tuple[str, ...]:
    """Emit ONE ``logger.error`` naming every EventType absent from ``table``.

    This is the *runtime* half of the guard, and the only half a running
    gateway can reach. Everything else in this module fires where a developer
    is — the pre-commit hook, pytest, ``python -m events.coverage`` — so all of
    it is bypassed by ``git commit --no-verify`` and by any checkout (an agent
    worktree, a fresh machine) where pre-commit was never installed. Call this
    from the bottom of the module that owns a required-total table, so the
    process that is actually shipping the incomplete table says so.

    Non-fatal by construction, for the reason in this module's docstring: a
    missing icon is a cosmetic notification defect, and raising here would turn
    it into a gateway that will not boot. One line, at ERROR so it survives the
    default level, then normal service.

    It RECORDS the gap; it must never repair it. Back-filling ``table`` here
    would make :meth:`TableSpec.resolve` — which reads the live table *after*
    importing the owning module — see a complete table forever, silently
    disarming :func:`coverage_gaps`, the CLI, the hook, and the tests. Return
    the record; leave the table exactly as the source declared it.

    Costs one dict lookup per enum member at import. Returns the missing type
    strings so the caller can publish them as a module constant, which is also
    what proves the check ran.
    """
    missing = tuple(missing_members(table))
    if missing:
        (logger or logging.getLogger(__name__)).error(
            "%s is missing an entry for %d of %d EventType members, which will "
            "ship as a degraded notification for each: %s. Fix: add one entry "
            "per type to %s (do not suppress this line).",
            qualname,
            len(missing),
            len(list(EventType)),
            ", ".join(missing),
            qualname,
        )
    return missing


def coverage_gaps(
    specs: Sequence[TableSpec] = REQUIRED_TOTAL,
) -> Dict[str, List[str]]:
    """Map each incomplete required-total table to its FULL missing set.

    Returns ``{}`` when every required table covers every EventType member.
    Deliberately collects rather than raising on the first miss — a report that
    names one type when twelve are gone has understated the drift at every
    recurrence on record.
    """
    gaps: Dict[str, List[str]] = {}
    for spec in specs:
        missing = missing_members(spec.resolve())
        if missing:
            gaps[spec.qualname] = missing
    return gaps


@lru_cache(maxsize=1)
def _walk_event_modules() -> Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
    """Import every module in the ``events`` package.

    Returns ``(imported_names, failures)`` where each failure is
    ``(module_name, "ExcType: message")``. Failures are surfaced rather than
    swallowed: a module that cannot be imported is a blind spot in discovery,
    and a silent blind spot is how this gap survived four times.

    Cached: the filesystem walk is the expensive half and the module set cannot
    change within a process. Discovery still re-reads ``vars(module)`` on every
    call, so a table added or patched at runtime is still seen.
    """
    import events as events_pkg

    imported: List[str] = ["events"]
    failures: List[Tuple[str, str]] = []
    for info in pkgutil.walk_packages(events_pkg.__path__, "events."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append((info.name, f"{type(exc).__name__}: {exc}"))
        else:
            imported.append(info.name)
    return tuple(imported), tuple(failures)


def discover_tables() -> Tuple[DiscoveredTable, ...]:
    """Find every module-level mapping in ``events`` keyed entirely by EventType.

    Results are deduped by object identity, so a table imported into a second
    module is reported once with both qualified names.

    The isinstance test is against ``collections.abc.Mapping``, not ``dict``, so
    that a ``MappingProxyType`` (how ``EVENT_TYPE_EMOJI`` is now exposed) is
    still discovered — see the module docstring.
    """
    imported, _failures = _walk_event_modules()
    by_id: Dict[int, List[str]] = {}
    tables: Dict[int, Mapping[EventType, object]] = {}

    for module_name in imported:
        mod = importlib.import_module(module_name)
        for attribute, value in vars(mod).items():
            if not isinstance(value, _MappingABC) or not value:
                continue
            if not all(isinstance(key, EventType) for key in value):
                continue
            by_id.setdefault(id(value), []).append(f"{module_name}.{attribute}")
            tables[id(value)] = value

    found = []
    for obj_id, qualnames in by_id.items():
        table = tables[obj_id]
        found.append(
            DiscoveredTable(
                qualnames=tuple(sorted(qualnames)),
                size=len(table),
                missing=tuple(missing_members(table)),
            )
        )
    return tuple(sorted(found, key=lambda t: t.primary))


def unclassified_tables(
    discovered: Optional[Iterable[DiscoveredTable]] = None,
) -> List[DiscoveredTable]:
    """Return discovered tables that no MANIFEST entry names.

    This is the part that makes a *future* sibling table self-covering: add a
    new EventType-keyed dict anywhere under ``events/`` and it lands here until
    someone declares it total or deliberately partial.
    """
    known = {spec.qualname for spec in MANIFEST}
    tables = discover_tables() if discovered is None else discovered
    return [t for t in tables if not known.intersection(t.qualnames)]


def unimportable_modules() -> List[Tuple[str, str]]:
    """Return ``(module, error)`` for every ``events`` module that failed to
    import — each one is a table discovery could not see."""
    _imported, failures = _walk_event_modules()
    return [tuple(f) for f in failures]


def format_report() -> Tuple[str, bool]:
    """Build the human-readable coverage report.

    Returns ``(text, ok)``. ``ok`` is False for a real drift — a required-total
    table with a missing member, or an EventType-keyed table nobody classified.
    Modules that would not import are reported as a WARNING without flipping
    ``ok``; see the comment in the body for why.
    """
    lines: List[str] = []
    total_members = len(list(EventType))

    gaps = coverage_gaps()
    unclassified = unclassified_tables()
    broken = unimportable_modules()
    # Import failures do NOT fail the report. This runs as a pre-commit hook
    # under whatever ``python`` is on PATH, which may lack an optional
    # subscriber dependency the repo venv has; blocking every commit on that
    # would get the hook disabled, which costs more than it saves. The
    # required-total check is unaffected (it imports its two tables directly);
    # only discovery goes partially blind, and
    # tests/events/test_event_type_coverage.py asserts zero failures under the
    # real venv. The warning is printed either way.
    ok = not (gaps or unclassified)

    if ok and not broken:
        lines.append(
            f"EventType coverage OK — all {total_members} members present in "
            f"{len(REQUIRED_TOTAL)} required-total tables."
        )
        for spec in REQUIRED_TOTAL:
            lines.append(f"  {spec.qualname}: {total_members}/{total_members}")
        return "\n".join(lines), True

    if ok:
        lines.append(
            f"EventType coverage OK — all {total_members} members present in "
            f"{len(REQUIRED_TOTAL)} required-total tables, but discovery was "
            f"incomplete (see below)."
        )
    else:
        lines.append("EventType coverage FAILED.")
    lines.append("")

    for spec in REQUIRED_TOTAL:
        missing = gaps.get(spec.qualname)
        if not missing:
            continue
        lines.append(
            f"{spec.qualname} — {len(missing)} of {total_members} members "
            f"missing an entry:"
        )
        for type_string in missing:
            lines.append(f"    {type_string}")
        lines.append(f"  Why it matters: {spec.why}.")
        lines.append(
            f"  Fix: add one entry per member above to {spec.qualname}."
        )
        lines.append("")

    if unclassified:
        lines.append(
            "EventType-keyed tables not declared in events.coverage.MANIFEST:"
        )
        for table in unclassified:
            names = " / ".join(table.qualnames)
            plural = "entry" if table.size == 1 else "entries"
            lines.append(
                f"    {names} ({table.size} {plural}, "
                f"{len(table.missing)} EventType members absent)"
            )
        lines.append(
            "  Fix: add each to REQUIRED_TOTAL (must cover every EventType) "
            "or to KNOWN_PARTIAL (deliberately a subset) in events/coverage.py."
        )
        lines.append("")

    if broken:
        lines.append(
            "WARNING — modules that failed to import; discovery could not "
            "inspect them for undeclared tables (does not fail this check; "
            "tests/events/test_event_type_coverage.py does):"
        )
        for module_name, error in broken:
            lines.append(f"    {module_name}: {error}")
        lines.append("")

    return "\n".join(lines).rstrip(), ok


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: ``python -m events.coverage``. Exit 1 on any gap."""
    report, ok = format_report()
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI shim
    raise SystemExit(main())
