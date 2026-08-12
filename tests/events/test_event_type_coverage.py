"""Standing guard against EventType / sibling-table drift.

Adding a member to ``EventType`` requires paired entries in the lookup tables
contracted to be total over the enum. That pairing has silently broken four
times (2026-04-27 twice, 2026-05-29, 2026-08-11), and ruff structurally cannot
catch it: ``EVENT_TYPE_EMOJI`` keys are attribute accesses, which F601 (repeated
*literal* keys) and F602 (bare *name* keys) do not reach.

These tests sit on ``events.coverage``, which owns the manifest of which tables
must be total, and which discovers EventType-keyed tables by walking the package
so a table added later is covered without a new hand-written test.
"""

import importlib
import logging
from collections.abc import Mapping

import pytest

from events import coverage
from events.coverage import (
    KNOWN_PARTIAL,
    MANIFEST,
    REQUIRED_TOTAL,
    TableSpec,
    coverage_gaps,
    discover_tables,
    format_report,
    missing_members,
    unclassified_tables,
    unimportable_modules,
)
from events.schema import EventType


# ── the actual coverage contract ────────────────────────────────────────────

@pytest.mark.parametrize(
    "spec", REQUIRED_TOTAL, ids=lambda s: s.qualname,
)
def test_required_total_table_covers_every_event_type(spec):
    """Every EventType member has an entry in every required-total table.

    The whole missing set is reported, not just the first miss.
    """
    missing = missing_members(spec.resolve())
    assert not missing, (
        f"{spec.qualname} is missing {len(missing)} of "
        f"{len(list(EventType))} EventType members: {', '.join(missing)}.\n"
        f"Why it matters: {spec.why}.\n"
        f"Run `python -m events.coverage` for the full report."
    )


def test_no_undeclared_event_type_tables():
    """A NEW EventType-keyed table must declare whether it has to be total.

    This is what makes the guard self-extending: adding another sibling lookup
    table anywhere under ``events/`` fails here until it is classified, instead
    of waiting for someone to hand-write a matching coverage test.
    """
    undeclared = unclassified_tables()
    assert not undeclared, (
        "EventType-keyed tables not declared in events.coverage.MANIFEST: "
        + "; ".join(
            f"{' / '.join(t.qualnames)} ({t.size} entries, "
            f"{len(t.missing)} EventType members absent)"
            for t in undeclared
        )
        + ". Add each to REQUIRED_TOTAL (must cover every EventType) or to "
        "KNOWN_PARTIAL (deliberately a subset) in events/coverage.py."
    )


def test_every_events_module_imports():
    """Discovery can only see tables in modules it can import.

    An unimportable module is a blind spot, and a silent blind spot is how this
    drift survived four recurrences.
    """
    broken = unimportable_modules()
    assert not broken, "events modules failed to import: " + "; ".join(
        f"{name}: {err}" for name, err in broken
    )


def test_manifest_entries_still_resolve():
    """No MANIFEST entry points at a renamed or deleted table.

    ``Mapping``, not ``dict``: EVENT_TYPE_EMOJI is a MappingProxyType derived
    from EventType.icon, which is not a dict. Same widening as
    coverage.discover_tables() — see that function's docstring.
    """
    for spec in MANIFEST:
        table = spec.resolve()
        assert isinstance(table, Mapping), f"{spec.qualname} is not a mapping"
        assert all(isinstance(k, EventType) for k in table), (
            f"{spec.qualname} has non-EventType keys; it does not belong in "
            f"the events.coverage manifest"
        )


def test_historically_drifted_tables_are_required_total():
    """Pin the two tables whose drift caused the four recorded recurrences.

    Demoting either to KNOWN_PARTIAL would silently reopen the gap, so it has
    to be a deliberate, test-breaking act.
    """
    required = {spec.qualname for spec in REQUIRED_TOTAL}
    assert "events.formatting.EVENT_TYPE_EMOJI" in required
    assert "events.routing_policy._POLICY" in required


def test_policy_check_is_still_armed_against_a_real_gap(monkeypatch):
    """The _POLICY check must FAIL on a table that is actually missing an entry.

    This is the anti-false-green test. On 2026-08-11 a competing branch
    (archive/eventtype-icon-as-enum-field-20260811) added a _finalize_policy()
    that back-filled every unmapped EventType with a fallback AT IMPORT TIME.
    coverage.py reads _POLICY *after* import, so the check reported
    "_POLICY: 79/79" and exited 0 on a table with a hole in it — a guard that
    looks armed and is not, which is worse than no guard.

    So: remove a real entry and assert the check notices. A green suite must
    not be consistent with "_POLICY is total by construction now".
    """
    import events.routing_policy as rp

    victim = EventType.NOTIFICATION_FAILED
    gapped = {k: v for k, v in rp._POLICY.items() if k is not victim}
    assert len(gapped) == len(rp._POLICY) - 1, "victim was not in _POLICY"
    monkeypatch.setattr(rp, "_POLICY", gapped)

    gaps = coverage_gaps()
    assert gaps.get("events.routing_policy._POLICY") == [victim.type_string], (
        "coverage_gaps() did not report a _POLICY entry that is genuinely "
        "missing — the table is being back-filled somewhere before the check "
        "reads it, and every green run from here is meaningless"
    )

    report, ok = format_report()
    assert ok is False
    assert victim.type_string in report


def test_policy_is_not_backfilled_at_import(monkeypatch):
    """classify() must degrade at CALL time, not by pre-filling the table.

    The graceful fallback for an unmapped type is deliberate and stays. What
    must not exist is an import-time back-fill: that is what turns the
    pre-commit check tautological. Pin the mechanism, not just the outcome.
    """
    import events.routing_policy as rp

    assert not hasattr(rp, "_finalize_policy"), (
        "_finalize_policy() back-fills _POLICY at import time and disarms "
        "events.coverage — see test_policy_check_is_still_armed_against_a_real_gap"
    )
    assert not hasattr(rp, "UNROUTED_EVENT_TYPES")

    # A type absent from the table still classifies (fallback), and is still
    # absent afterwards — .get() must not be a setdefault in disguise.
    victim = EventType.NOTIFICATION_FAILED
    gapped = {k: v for k, v in rp._POLICY.items() if k is not victim}
    monkeypatch.setattr(rp, "_POLICY", gapped)
    assert missing_members(rp._POLICY) == [victim.type_string]


def test_manifest_has_no_duplicate_entries():
    qualnames = [spec.qualname for spec in MANIFEST]
    assert len(qualnames) == len(set(qualnames)), (
        f"duplicate MANIFEST entries: {qualnames}"
    )


def test_known_partial_tables_are_documented():
    """A deliberately-partial table must say why, so the next reader can tell
    'intentionally a subset' from 'nobody looked at this yet'."""
    for spec in KNOWN_PARTIAL:
        assert spec.total is False
        assert spec.why.strip(), f"{spec.qualname} has no rationale"


# ── missing_members() semantics ─────────────────────────────────────────────

def test_missing_members_reports_all_misses_not_just_the_first():
    """Regression for the short-circuit bug this module exists to fix.

    The historic ``for et in EventType: assert table.get(et)`` form stopped at
    the first miss, so every recurrence reported one missing type while 12-13
    were gone. Collecting is the whole point.
    """
    members = list(EventType)
    table = {members[0]: "icon"}
    missing = missing_members(table)
    assert len(missing) == len(members) - 1
    assert members[1].type_string in missing
    assert members[-1].type_string in missing


def test_missing_members_treats_a_falsy_value_as_missing():
    """An entry mapped to "" is the SR-408 defect, not coverage: event_icon()
    returns the empty string either way and the header renders with a
    double-space gap."""
    table = {et: "x" for et in EventType}
    table[EventType.SECRET_DETECTED] = ""
    assert missing_members(table) == ["secret_detected"]


def test_missing_members_preserves_enum_declaration_order():
    """Report order follows schema.py so a contiguous block of new members
    reads as the block it is (all 12 DDP types together, in source order)."""
    members = list(EventType)
    table = {members[0]: "icon"}
    assert missing_members(table) == [et.type_string for et in members[1:]]


# ── coverage_gaps() / report / CLI ──────────────────────────────────────────

def test_coverage_gaps_is_empty_when_every_required_table_is_total():
    assert coverage_gaps() == {}


def test_coverage_gaps_collects_the_full_missing_set(monkeypatch):
    """Simulate the 2026-08-11 drift shape: one required table incomplete."""
    import events.formatting as formatting

    drifted = {et: "x" for et in EventType}
    dropped = [et for et in EventType if et.type_string.startswith("devflow.")]
    for et in dropped:
        del drifted[et]
    monkeypatch.setattr(formatting, "_DRIFT_FIXTURE", drifted, raising=False)

    spec = TableSpec(
        "events.formatting", "_DRIFT_FIXTURE", total=True, why="fixture",
    )
    gaps = coverage_gaps([spec])

    assert list(gaps) == [spec.qualname]
    assert gaps[spec.qualname] == [et.type_string for et in dropped]
    assert len(gaps[spec.qualname]) > 1, (
        "fixture must drop more than one member — the point is that the "
        "report does not stop at the first"
    )


def test_format_report_is_clean_on_the_current_tree():
    report, ok = format_report()
    assert ok, report
    assert "EventType coverage OK" in report
    assert str(len(list(EventType))) in report


def test_format_report_names_every_missing_member(monkeypatch):
    missing = ["devflow.work_requested", "devflow.merged", "devflow.deployed"]
    monkeypatch.setattr(
        coverage, "coverage_gaps",
        lambda specs=REQUIRED_TOTAL: {
            "events.formatting.EVENT_TYPE_EMOJI": list(missing)
        },
    )
    report, ok = format_report()

    assert ok is False
    assert "EventType coverage FAILED" in report
    for type_string in missing:
        assert type_string in report, f"{type_string} absent from report"
    # The fix instruction has to name the file to edit.
    assert "events.formatting.EVENT_TYPE_EMOJI" in report


def test_format_report_flags_an_undeclared_table(monkeypatch):
    monkeypatch.setattr(
        coverage, "unclassified_tables",
        lambda discovered=None: [
            coverage.DiscoveredTable(
                qualnames=("events.formatting.NEW_TABLE",),
                size=1,
                missing=tuple(
                    et.type_string for et in list(EventType)[1:]
                ),
            )
        ],
    )
    report, ok = format_report()

    assert ok is False
    assert "events.formatting.NEW_TABLE" in report
    assert "MANIFEST" in report


def test_format_report_warns_about_unimportable_modules_without_failing(
    monkeypatch,
):
    """A module that will not import is a discovery blind spot, but it must not
    block a commit: the hook runs under whatever ``python`` is on PATH, which
    may lack an optional subscriber dependency. It is reported loudly and
    ``test_every_events_module_imports`` is the strict gate under the venv.
    """
    monkeypatch.setattr(
        coverage, "unimportable_modules",
        lambda: [("events.subscribers.telegram_notifier", "ImportError: x")],
    )
    report, ok = format_report()

    assert ok is True, "an import failure must not fail the coverage check"
    assert "WARNING" in report
    assert "events.subscribers.telegram_notifier" in report
    assert "EventType coverage FAILED" not in report


def test_main_exits_zero_when_coverage_is_complete(capsys):
    assert coverage.main([]) == 0
    assert "EventType coverage OK" in capsys.readouterr().out


def test_main_exits_nonzero_on_a_gap(monkeypatch, capsys):
    monkeypatch.setattr(
        coverage, "coverage_gaps",
        lambda specs=REQUIRED_TOTAL: {
            "events.formatting.EVENT_TYPE_EMOJI": ["devflow.work_requested"]
        },
    )
    assert coverage.main([]) == 1
    assert "devflow.work_requested" in capsys.readouterr().out


# ── discovery ───────────────────────────────────────────────────────────────

def test_discovery_finds_the_required_total_tables():
    found = {name for table in discover_tables() for name in table.qualnames}
    for spec in REQUIRED_TOTAL:
        assert spec.qualname in found, (
            f"{spec.qualname} was not discovered by walking the events package"
        )


def test_discovery_finds_the_known_partial_tables():
    found = {name for table in discover_tables() for name in table.qualnames}
    for spec in KNOWN_PARTIAL:
        assert spec.qualname in found, f"{spec.qualname} was not discovered"


def test_discovery_dedupes_a_re_exported_table(monkeypatch):
    """Importing a table into a second module must not read as a second,
    unclassified table."""
    import events.formatting as formatting
    import events.outcomes as outcomes

    monkeypatch.setattr(
        outcomes, "EVENT_TYPE_EMOJI", formatting.EVENT_TYPE_EMOJI,
        raising=False,
    )
    tables = discover_tables()
    matches = [
        t for t in tables
        if "events.formatting.EVENT_TYPE_EMOJI" in t.qualnames
    ]
    assert len(matches) == 1
    assert "events.outcomes.EVENT_TYPE_EMOJI" in matches[0].qualnames
    # And the alias must not trip the undeclared-table guard.
    assert not unclassified_tables(tables)


# ── the runtime half of the guard ───────────────────────────────────────────
#
# Everything above runs where a *developer* is: the pre-commit hook, pytest,
# `python -m events.coverage`. All three are bypassed by `git commit
# --no-verify` and by any checkout where pre-commit was never installed, and
# none of them say anything from a *running gateway*. These tests cover the
# non-fatal import-time signal that closes that residual gap.
#
# The signal RECORDS the gap; it must never repair it. A table that
# back-filled itself at import would make TableSpec.resolve() — which reads the
# live table AFTER importing the owning module — see a complete table forever,
# permanently disarming every check above.

class TestLogMissingMembers:
    """events.coverage.log_missing_members: report, never repair."""

    def _table(self, drop=()):
        dropped = set(drop)
        return {
            et: "x" for et in EventType if et.type_string not in dropped
        }

    def test_total_table_returns_empty_and_logs_nothing(self, caplog):
        with caplog.at_level(logging.DEBUG):
            missing = coverage.log_missing_members(
                self._table(), "events.fake.TABLE"
            )
        assert missing == ()
        assert caplog.records == []

    def test_names_every_offender_in_one_error_record(self, caplog):
        gone = [et.type_string for et in EventType][:3]
        with caplog.at_level(logging.DEBUG):
            missing = coverage.log_missing_members(
                self._table(drop=gone), "events.fake.TABLE"
            )

        assert missing == tuple(gone)
        assert len(caplog.records) == 1, (
            "the signal must cost exactly one log line per process, not one "
            "per missing member"
        )
        record = caplog.records[0]
        assert record.levelno == logging.ERROR
        assert "events.fake.TABLE" in record.getMessage()
        for type_string in gone:
            assert type_string in record.getMessage(), (
                f"{type_string} is missing but was not named; a report that "
                f"names some of the drift has understated it every time"
            )

    def test_a_falsy_value_counts_as_missing(self, caplog):
        table = self._table()
        blank = next(iter(EventType))
        table[blank] = ""
        with caplog.at_level(logging.DEBUG):
            missing = coverage.log_missing_members(table, "events.fake.TABLE")
        assert missing == (blank.type_string,), (
            "an empty icon renders the same double-space header gap as no key "
            "at all, so it must count as missing here exactly as it does in "
            "missing_members()"
        )

    def test_does_not_back_fill_the_table_it_inspects(self, caplog):
        gone = [et.type_string for et in EventType][:2]
        table = self._table(drop=gone)
        before = dict(table)
        with caplog.at_level(logging.DEBUG):
            coverage.log_missing_members(table, "events.fake.TABLE")
        assert table == before, (
            "the runtime signal must expose the record, never fill the table: "
            "a self-healing table makes coverage_gaps() report zero forever"
        )

    def test_logs_through_the_caller_supplied_logger(self, caplog):
        logger = logging.getLogger("events.fake.owner")
        gone = [next(iter(EventType)).type_string]
        with caplog.at_level(logging.DEBUG):
            coverage.log_missing_members(
                self._table(drop=gone), "events.fake.TABLE", logger=logger
            )
        assert caplog.records[0].name == "events.fake.owner", (
            "the line must be attributed to the module that owns the table, "
            "so an operator reading gateway logs knows what to fix"
        )


class TestRequiredTotalTablesSignalAtRuntime:
    """Each required-total table publishes its own import-time record.

    The constant existing at all is the proof that the module ran the check;
    it is assigned from the return value of the same call that logs.
    """

    def test_event_type_emoji_has_no_missing_icons(self):
        import events.formatting as formatting

        assert formatting.EVENT_TYPES_WITHOUT_ICON == (), (
            "these EventType members ship with no notification icon: "
            f"{formatting.EVENT_TYPES_WITHOUT_ICON}"
        )

    def test_routing_policy_has_no_unmapped_types(self):
        import events.routing_policy as routing_policy

        assert routing_policy.EVENT_TYPES_WITHOUT_POLICY == (), (
            "these EventType members have no routing policy: "
            f"{routing_policy.EVENT_TYPES_WITHOUT_POLICY}"
        )

    @pytest.mark.parametrize(
        "module_name, attribute, record_name",
        [
            ("events.formatting", "EVENT_TYPE_EMOJI", "EVENT_TYPES_WITHOUT_ICON"),
            ("events.routing_policy", "_POLICY", "EVENT_TYPES_WITHOUT_POLICY"),
        ],
    )
    def test_record_agrees_with_the_manifest_check(
        self, module_name, attribute, record_name
    ):
        """The runtime record and the commit-time check must not diverge."""
        module = importlib.import_module(module_name)
        spec = next(
            s for s in REQUIRED_TOTAL
            if s.module == module_name and s.attribute == attribute
        )
        assert list(getattr(module, record_name)) == missing_members(
            spec.resolve()
        )
