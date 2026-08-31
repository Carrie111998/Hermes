"""Continuous single-writer reconciliation of the Desktop session registries.

``DesktopRegistrySyncWorker`` is the durable driver around the pure planning
core in :mod:`session_bridge.desktop_registry`.  One cycle is::

    recover -> scan -> plan -> stage run -> apply -> rescan -> verify
            -> advance baselines -> record conflicts -> commit run

Baselines advance only for records whose intended group values verified on
every enrolled root, and only after the run's mutations were staged durably.
Because every individual file write is atomic (same-directory temporary plus
``os.replace``) and baselines advance last, an interrupted cycle needs no byte
replay: the stale run is abandoned with an audit trail and the next cycle
re-derives the remaining deltas from the standing baselines.  A scan that
cannot produce complete, stable evidence writes nothing.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from .desktop_registry import (
    DESKTOP_REGISTRY_GROUPING_VERSION,
    RegistryBaseline,
    RegistryMutationConflict,
    RegistryScanError,
    apply_registry_mutation,
    build_registry_sync_plan,
    scan_desktop_registry_roots,
    verify_registry_sync_plan,
)


class DesktopRegistrySyncWorker:
    """Reconcile enrolled Desktop registry roots against durable baselines."""

    def __init__(
        self,
        store: Any,
        *,
        registry_roots: Iterable[Path],
        run_min_interval_seconds: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        roots = tuple(registry_roots)
        if not roots:
            raise ValueError("registry_roots must not be empty")
        for root in roots:
            if not isinstance(root, Path):
                raise TypeError("registry_roots must contain Path entries")
        interval = float(run_min_interval_seconds)
        if not math.isfinite(interval) or interval < 0:
            raise ValueError(
                "run_min_interval_seconds must be finite and non-negative"
            )
        self._store = store
        self._registry_roots = roots
        self._run_min_interval_seconds = interval
        self._monotonic = monotonic
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._last_run_at: float | None = None

    def run_once(self) -> dict[str, int]:
        counters = {
            "examined": 0,
            "patched": 0,
            "created": 0,
            "raced": 0,
            "conflicts": 0,
            "verify_failures": 0,
            "baseline_rows_advanced": 0,
            "recovered_runs": 0,
            "scan_failed": 0,
            "throttled": 0,
        }
        now = self._monotonic()
        if (
            self._last_run_at is not None
            and now - self._last_run_at < self._run_min_interval_seconds
        ):
            counters["throttled"] = 1
            return counters
        self._last_run_at = now

        pending = self._store.pending_desktop_registry_run()
        if pending is not None:
            # File writes are individually atomic and baselines advance last,
            # so a run that never committed left disk in a mixture of old and
            # accepted values -- exactly what a fresh plan against the standing
            # baselines converges. Abandon with audit and replan.
            self._store.finish_desktop_registry_run(
                pending["id"], "abandoned", resolution="recovered_by_replan"
            )
            counters["recovered_runs"] = 1

        try:
            scan = scan_desktop_registry_roots(self._registry_roots)
        except RegistryScanError:
            counters["scan_failed"] = 1
            return counters

        stored_rows = self._store.load_desktop_registry_baselines()
        baselines = [RegistryBaseline(**row) for row in stored_rows]
        plan = build_registry_sync_plan(scan, baselines=baselines)
        counters["examined"] = len(plan.records)
        counters["conflicts"] = len(plan.conflicts)

        mutations = [
            mutation
            for record in plan.records.values()
            for mutation in record.mutations
        ]
        run_id: str | None = None
        if mutations:
            run_id = self._id_factory()
            payload = {
                "grouping_version": DESKTOP_REGISTRY_GROUPING_VERSION,
                "roots": {
                    root_id: observation.canonical_path
                    for root_id, observation in scan.roots.items()
                },
                "mutations": [
                    {
                        "root_id": mutation.root_id,
                        "filename": mutation.filename,
                        "operation": mutation.operation,
                        "expected_before_hash": mutation.expected_before_hash,
                        "changed_groups": sorted(mutation.changed_fields),
                    }
                    for mutation in mutations
                ],
            }
            self._store.stage_desktop_registry_run(
                run_id,
                DESKTOP_REGISTRY_GROUPING_VERSION,
                json.dumps(payload, ensure_ascii=False),
            )
            for mutation in mutations:
                try:
                    apply_registry_mutation(scan, mutation)
                except RegistryMutationConflict:
                    counters["raced"] += 1
                else:
                    key = "created" if mutation.operation == "create" else "patched"
                    counters[key] += 1

        try:
            fresh = scan_desktop_registry_roots(self._registry_roots)
            verification = verify_registry_sync_plan(plan, fresh)
        except (RegistryScanError, ValueError):
            if run_id is not None:
                self._store.finish_desktop_registry_run(
                    run_id, "abandoned", resolution="verify_scan_failed"
                )
            counters["scan_failed"] = 1
            return counters

        failed_files = {failure.filename for failure in verification.failures}
        counters["verify_failures"] = len(verification.failures)

        stored_index = {
            (row["filename"], row["root_id"], row["group_name"]): row["value_json"]
            for row in stored_rows
        }
        advance = [
            {
                "filename": baseline.filename,
                "root_id": baseline.root_id,
                "group_name": baseline.group_name,
                "value_json": baseline.value_json,
                "revision": baseline.revision,
            }
            for baseline in plan.proposed_baselines
            if baseline.filename not in failed_files
            and stored_index.get(
                (baseline.filename, baseline.root_id, baseline.group_name)
            )
            != baseline.value_json
        ]
        if advance:
            counters["baseline_rows_advanced"] = (
                self._store.upsert_desktop_registry_baselines(advance)
            )
        self._store.replace_desktop_registry_conflicts(
            [
                {
                    "filename": conflict.filename,
                    "group_name": conflict.group_name,
                    "reason": conflict.reason,
                    "candidates_json": json.dumps(
                        dict(conflict.candidates), ensure_ascii=False, sort_keys=True
                    ),
                }
                for conflict in plan.conflicts
            ]
        )
        if run_id is not None:
            resolution = None
            if failed_files or counters["raced"]:
                resolution = (
                    f"partial: raced={counters['raced']} "
                    f"verify_failed_files={len(failed_files)}"
                )
            self._store.finish_desktop_registry_run(
                run_id, "committed", resolution=resolution
            )
        return counters
