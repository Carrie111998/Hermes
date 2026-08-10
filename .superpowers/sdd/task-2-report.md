# Task 2 Report

## Scope
Implemented validated semantic-outcome types and SQLite WAL persistence for immutable logical activity runs, parent/child attribution, per-served-route additive usage, exact Decimal costs, session linkage, and single-assignment terminal enrichment.

## RED evidence
- Initial focused run failed collection with `ModuleNotFoundError: No module named 'activity_telemetry'` for both schema and store tests.
- The first GREEN attempt exposed a duplicate-finish clock-order defect: `StopIteration` occurred before the expected `already finished` error.
- Child-count coverage failed with `KeyError: 'child_count'` before parent accounting was added.
- Expanded validation exposed raw `sqlite3.IntegrityError` for duplicate session IDs.
- Independent review regressions failed for finished-parent child creation, concurrent Decimal costs, and unsafe evidence payloads before fixes.

## Implementation properties
- Thread-local SQLite connections with `check_same_thread=False`, 10-second connection timeout, WAL, `busy_timeout=5000`, `synchronous=NORMAL`, `journal_size_limit=33554432`, and `wal_autocheckpoint=1000`.
- Explicit `BEGIN IMMEDIATE` serializes read-modify-write operations across store instances/processes; every failed write rolls back before re-raising.
- Logical identity is insert-only. Session linkage is single-assignment. Terminal enrichment is single-assignment. Finished parents cannot gain children.
- Integer usage counters are additive. Costs remain nullable when absent, preserve zero, round-trip as `Decimal`, and are summed without float conversion.
- Missing semantic or delivery evidence remains `unknown`. `no_work` and terminal precedence follow the approved contract.
- Evidence accepts only bounded opaque references (`kind:identifier`), rejecting URLs, serialized payloads, credential-shaped values, and oversized content.

## Verification evidence
- `python -m pytest tests/activity_telemetry/test_schema.py tests/activity_telemetry/test_store.py tests/activity_policy/test_registry.py tests/test_packaging_metadata.py tests/events/test_bus.py -q`: **131 passed in 22.37s**.
- `python -m ruff check activity_telemetry tests/activity_telemetry`: **All checks passed**.
- `git diff --check`: clean.

## Review
Independent review identified four Important findings: cross-store terminal race, lost concurrent Decimal costs, children added after parent finish, and unrestricted evidence payloads. All four were addressed with focused regressions and deterministic verification. No live cron state, routing, provider/model selection, schedule, delivery, credential, or service configuration was changed.
