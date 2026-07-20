# Tracker Applier Convergence-Reaper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-clear *capped* tracker partial-intents that native Postgres **and** the tracker canonical `pipeline.json` both show converged at/past their target stage, so the `TRACKER_PARTIAL_BACKLOG` alert stops firing forever on already-done work.

**Architecture:** Add a two-gate, fail-closed `IntentApplier.reap_converged_partials()` that reuses the existing Fix A pre-flight (`_already_satisfied` → native PG) as gate A and a new canonical-`pipeline.json` reader (`currentBusinessState`) as gate B. It runs on the single-writer applier thread, once/min, right after `redrive_partials()`, behind its own default-off flag. Pair it with re-enabling a finite re-drive cap. Remove the dead `max_redrive_attempts` field.

**Tech Stack:** Python 3.11, pytest. Repo: `~/.hermes/agent-src` (its OWN git repo, **local-only — never push**). Config lives in `~/.hermes/profiles/main/.env` (parent `~/.hermes` repo, also local-only).

## Global Constraints

- **agent-src is local-only. NEVER `git push`.** Author = Diego. End commit messages with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.
- **Run tests with `PYTHONPATH=$(pwd)` from the agent-src root** (the package layout requires it). Example: `PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/test_applier.py -v`.
- **Do NOT auto-restart the gateway.** Code + config land inert; enablement happens on the next *natural* gateway restart. Report PID + time, never restart.
- **The reaper must stay read-only until converged and must NEVER move a file to `inbox/`** (re-driving a past-stage intent regresses state). A reap = `mark_applied` (burn key) + move to `processed/`, mirroring the audited `satisfied` path in `apply_one`.
- **Fail closed:** any ambiguity (reader off/None, stage not in `_STAGE_SATISFIED_BY`, job absent from canonical, parse error, canonical reader unwired) ⇒ do NOT reap; leave the partial capped so it keeps alerting.
- **Gate B field is `currentBusinessState`, NOT `.stage`.** In the tracker canonical file `.stage`/`.business_state`/`.pipeline_stage` are legacy-space (`review`); `_STAGE_SATISFIED_BY` is valued in business_states (`materials_ready`), so only `currentBusinessState` lines up.
- **The tracker canonical `pipeline.json` has `jobs` as a DICT** keyed by `job_id` (== `external_job_key` == intent `job_id`). This is a *different shape* from the applier's legacy `PipelineManager` projection (`jobs` = list). Gate B reads the dict-shaped canonical file only.
- Design spec: `docs/superpowers/specs/2026-07-20-tracker-applier-convergence-reaper-design.md`.

---

### Task 1: Canonical pipeline business-state reader

New module supplying gate B: a fresh `{job_id: currentBusinessState}` map read from the tracker canonical `pipeline.json`. Fail-soft (any error ⇒ empty map).

**Files:**
- Create: `intent_applier/canonical_pipeline_reader.py`
- Modify: `intent_applier/__init__.py` (export the builder)
- Test: `tests/intent_applier/test_canonical_pipeline_reader.py`

**Interfaces:**
- Consumes: nothing (leaf module; stdlib only).
- Produces:
  - `load_canonical_business_states(path: Path) -> dict[str, str]`
  - `build_default_canonical_reader(path: Optional[Path] = None) -> Callable[[], dict[str, str]]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/intent_applier/test_canonical_pipeline_reader.py
import json
from pathlib import Path

from intent_applier.canonical_pipeline_reader import (
    build_default_canonical_reader,
    load_canonical_business_states,
)


def _write(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def test_dict_shaped_jobs_maps_job_id_to_current_business_state(tmp_path):
    p = _write(tmp_path / "pipeline.json", {
        "jobs": {
            "job-a": {"stage": "review", "currentBusinessState": "materials_ready"},
            "job-b": {"stage": "archived", "currentBusinessState": "archived"},
        }
    })
    assert load_canonical_business_states(p) == {
        "job-a": "materials_ready",
        "job-b": "archived",
    }


def test_jobs_without_current_business_state_are_omitted(tmp_path):
    p = _write(tmp_path / "pipeline.json", {
        "jobs": {
            "job-a": {"stage": "review"},                       # no currentBusinessState
            "job-b": {"currentBusinessState": ""},              # empty -> omit
            "job-c": {"currentBusinessState": "submitted"},     # kept
        }
    })
    assert load_canonical_business_states(p) == {"job-c": "submitted"}


def test_legacy_list_shaped_jobs_yields_empty_map(tmp_path):
    # The PipelineManager legacy projection uses jobs=list; gate B must ignore it.
    p = _write(tmp_path / "pipeline.json", {"jobs": [{"job_id": "x", "stage": "review"}]})
    assert load_canonical_business_states(p) == {}


def test_missing_or_corrupt_file_yields_empty_map(tmp_path):
    assert load_canonical_business_states(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_canonical_business_states(bad) == {}


def test_build_default_reader_is_zero_arg_and_reads_the_bound_path(tmp_path):
    p = _write(tmp_path / "pipeline.json", {
        "jobs": {"job-a": {"currentBusinessState": "offer"}}
    })
    reader = build_default_canonical_reader(p)
    assert reader() == {"job-a": "offer"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/test_canonical_pipeline_reader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'intent_applier.canonical_pipeline_reader'`

- [ ] **Step 3: Write the module**

```python
# intent_applier/canonical_pipeline_reader.py
"""Canonical tracker-pipeline business-state reader for the reaper's gate B.

The convergence-reaper (IntentApplier.reap_converged_partials) confirms a capped
partial is truly converged with TWO independent snapshots: native Postgres
(gate A) AND the tracker's own canonical pipeline.json (gate B). This module
supplies gate B: a job_id -> currentBusinessState map read fresh from
profiles/tracker/workspace/pipeline.json.

IMPORTANT: the canonical file's `.stage` field is LEGACY-space (e.g. "review").
The business-state value that lines up with _STAGE_SATISFIED_BY (valued in
business_states like "materials_ready") is `.currentBusinessState`. Reading
`.stage` here would fail-closed on real convergence. Verified against the live
41MB file 2026-07-20.

The canonical file keys `jobs` as a DICT by job_id (== jobs.external_job_key ==
intent job_id) -- a different shape from the PipelineManager legacy projection
(jobs = list), which this reader deliberately ignores (returns {}).

Fail-soft: any error (missing file, bad JSON, unexpected shape) yields an empty
map, so gate B simply can't confirm convergence and the reaper leaves the
partial capped -- never a wrong reap.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def load_canonical_business_states(path: Path) -> dict[str, str]:
    """Return {job_id: currentBusinessState} from a tracker canonical pipeline.json.

    Jobs with no non-empty currentBusinessState are omitted. jobs must be a dict
    (the canonical shape); a list-shaped legacy projection yields {}. Any failure
    yields {} (fail-soft).
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        jobs = data.get("jobs")
        if not isinstance(jobs, dict):
            return {}
        out: dict[str, str] = {}
        for job_id, rec in jobs.items():
            if not isinstance(rec, dict):
                continue
            cbs = rec.get("currentBusinessState")
            if isinstance(cbs, str) and cbs:
                out[job_id] = cbs
        return out
    except Exception:
        logger.debug(
            "canonical-reader: read failed for %s (fail-soft)", path, exc_info=True
        )
        return {}


def _default_canonical_path() -> Path:
    root = Path(os.environ.get("HERMES_ROOT", str(Path.home() / ".hermes")))
    return root / "profiles" / "tracker" / "workspace" / "pipeline.json"


def build_default_canonical_reader(
    path: Optional[Path] = None,
) -> Callable[[], dict[str, str]]:
    """A zero-arg callable returning a fresh {job_id: currentBusinessState} map.

    The reaper invokes it AT MOST once per sweep. Bound to the tracker canonical
    pipeline.json under HERMES_ROOT unless an explicit path is given.
    """
    target = path or _default_canonical_path()
    return lambda: load_canonical_business_states(target)
```

- [ ] **Step 4: Export from the package**

In `intent_applier/__init__.py`, add after the `job_state_reader` import line:

```python
from .canonical_pipeline_reader import (
    build_default_canonical_reader,
    load_canonical_business_states,
)
```

And add to `__all__` (after `"NativePgJobStateReader", "build_default_reader",`):

```python
    "build_default_canonical_reader", "load_canonical_business_states",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/test_canonical_pipeline_reader.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add intent_applier/canonical_pipeline_reader.py intent_applier/__init__.py tests/intent_applier/test_canonical_pipeline_reader.py
git commit -m "$(cat <<'EOF'
feat(intent-applier): canonical pipeline currentBusinessState reader (reaper gate B)

Fail-soft {job_id: currentBusinessState} map from the tracker canonical
pipeline.json (dict-shaped jobs). Reads currentBusinessState, not the
legacy-space .stage; ignores list-shaped legacy projections.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `reap_converged_partials()` on IntentApplier

The core sweep. Adds the `canonical_state_reader` constructor param, a `_is_capped` helper, and the two-gate fail-closed reaper.

**Files:**
- Modify: `intent_applier/applier.py` (add ctor param ~line 128; add methods after `redrive_partials`, ~line 517)
- Test: `tests/intent_applier/test_applier.py` (new `TestReapConvergedPartials` class + a `_canonical` reader in `_make_applier` calls)

**Interfaces:**
- Consumes: `IntentApplier._already_satisfied(msg)`, `_STAGE_SATISFIED_BY`, `_parse_redrive_attempt(path)`, `parse_intent_file`, `IntentParseError`, `IntentMessage`, `idempotency.mark_applied(key, *, message_id)`, `_move_to(src, dest)`, `self.processed_dir`, `self.partial_dir`, `self.redrive_give_up_attempts` — all already present.
- Produces:
  - `IntentApplier.__init__(..., canonical_state_reader: Optional[Callable[[], dict[str, str]]] = None)`
  - `IntentApplier._is_capped(path: Path) -> bool`
  - `IntentApplier.reap_converged_partials() -> dict[str, str]` returning `{filename: "reaped" | "not_converged" | "skipped"}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/intent_applier/test_applier.py` (uses existing `_make_applier`, `_write_partial`, `_stage_payload`, `VALID_INTENT_PAYLOAD`):

```python
class TestReapConvergedPartials:
    """Convergence-reaper: auto-clear CAPPED partials PG+canonical both show done."""

    def _capped_kwargs(self, **extra):
        # give_up=5 so a .rd5 partial is "capped"; both readers wired.
        base = dict(redrive_give_up_attempts=5,
                    job_state_reader=lambda jid: "materials_ready",
                    canonical_state_reader=lambda: {"linkedin-1": "materials_ready"})
        base.update(extra)
        return base

    def test_capped_and_both_gates_converged_is_reaped(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path, **self._capped_kwargs())
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        result = a.reap_converged_partials()
        assert result == {"x_APPROVAL_INTENT_main.rd5.json": "reaped"}
        # Moved to processed (never inbox), key burned.
        assert (mailbox["processed"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert not (mailbox["inbox"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert not (mailbox["partial"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert a.idempotency.is_applied(VALID_INTENT_PAYLOAD["idempotency_key"])

    def test_gate_b_disagrees_is_not_reaped(self, tmp_path, mailbox, pipeline_path):
        # PG says materials_ready (gate A pass) but canonical still shows scored.
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          **self._capped_kwargs(
                              canonical_state_reader=lambda: {"linkedin-1": "scored"}))
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {"x_APPROVAL_INTENT_main.rd5.json": "not_converged"}
        assert (mailbox["partial"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert not a.idempotency.is_applied(VALID_INTENT_PAYLOAD["idempotency_key"])

    def test_gate_a_behind_never_parses_canonical(self, tmp_path, mailbox, pipeline_path):
        from unittest.mock import MagicMock
        canonical = MagicMock(return_value={"linkedin-1": "materials_ready"})
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          **self._capped_kwargs(
                              job_state_reader=lambda jid: "scored",   # gate A fails
                              canonical_state_reader=canonical))
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {"x_APPROVAL_INTENT_main.rd5.json": "not_converged"}
        canonical.assert_not_called()  # gate B never touched

    def test_canonical_parsed_once_per_sweep(self, tmp_path, mailbox, pipeline_path):
        from unittest.mock import MagicMock
        canonical = MagicMock(return_value={"linkedin-1": "materials_ready",
                                            "linkedin-2": "materials_ready"})
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          **self._capped_kwargs(canonical_state_reader=canonical))
        p2 = json.loads(json.dumps(VALID_INTENT_PAYLOAD))
        p2["job_id"] = "linkedin-2"
        p2["idempotency_key"] = "tracker-intent:it:linkedin-2:approved"
        _write_partial(mailbox["partial"], "a_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        _write_partial(mailbox["partial"], "b_APPROVAL_INTENT_main.rd5.json",
                       p2, age_seconds=1)
        result = a.reap_converged_partials()
        assert result == {"a_APPROVAL_INTENT_main.rd5.json": "reaped",
                          "b_APPROVAL_INTENT_main.rd5.json": "reaped"}
        assert canonical.call_count == 1

    def test_non_capped_partial_is_ignored(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path, **self._capped_kwargs())
        # rd1 < give_up=5 -> not capped -> reaper leaves it entirely.
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd1.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {}
        assert (mailbox["partial"] / "x_APPROVAL_INTENT_main.rd1.json").exists()

    def test_give_up_zero_reaps_nothing(self, tmp_path, mailbox, pipeline_path):
        # Default give_up=0 => nothing is ever capped => reaper is a no-op.
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: "materials_ready",
                          canonical_state_reader=lambda: {"linkedin-1": "materials_ready"})
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {}

    def test_archived_incident_case_reaped(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          redrive_give_up_attempts=5,
                          job_state_reader=lambda jid: "archived",
                          canonical_state_reader=lambda: {"linkedin-1": "archived"})
        _write_partial(mailbox["partial"], "z_STATE_TRANSITION_INTENT_main.rd5.json",
                       _stage_payload("archived"), age_seconds=1)
        assert a.reap_converged_partials() == {"z_STATE_TRANSITION_INTENT_main.rd5.json": "reaped"}

    def test_canonical_reader_unwired_fails_closed(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          redrive_give_up_attempts=5,
                          job_state_reader=lambda jid: "materials_ready",
                          canonical_state_reader=None)
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {"x_APPROVAL_INTENT_main.rd5.json": "not_converged"}

    def test_corrupt_capped_file_is_skipped(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path, **self._capped_kwargs())
        mailbox["partial"].mkdir(parents=True, exist_ok=True)
        bad = mailbox["partial"] / "bad_APPROVAL_INTENT_main.rd5.json"
        bad.write_text("{not valid", encoding="utf-8")
        assert a.reap_converged_partials() == {"bad_APPROVAL_INTENT_main.rd5.json": "skipped"}
        assert bad.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/test_applier.py::TestReapConvergedPartials -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'canonical_state_reader'`

- [ ] **Step 3: Add the constructor param**

In `intent_applier/applier.py`, add a param to `__init__` right after `job_state_reader` (~line 128):

```python
        job_state_reader: Optional[Callable[[str], Optional[str]]] = None,
        canonical_state_reader: Optional[Callable[[], dict[str, str]]] = None,
```

And store it right after `self.job_state_reader = job_state_reader` (~line 149):

```python
        # Reaper gate B: zero-arg callable -> {job_id: currentBusinessState} from
        # the tracker canonical pipeline.json. None => gate B unsatisfiable =>
        # reaper never reaps (fail-closed).
        self.canonical_state_reader = canonical_state_reader
```

- [ ] **Step 4: Implement `_is_capped` and `reap_converged_partials`**

In `intent_applier/applier.py`, add these methods after `redrive_partials` (end of class, ~line 517):

```python
    def _is_capped(self, path: Path) -> bool:
        """True iff this partial has reached the re-drive give-up cap.

        Mirrors redrive_partials()'s capping predicate: capping is opt-in via
        redrive_give_up_attempts (0 => never capped, so the reaper is a no-op).
        """
        if not self.redrive_give_up_attempts:
            return False
        return self._parse_redrive_attempt(path) >= self.redrive_give_up_attempts

    def reap_converged_partials(self) -> dict[str, str]:
        """Auto-clear CAPPED partials already converged at/past their target stage.

        A capped partial is one redrive_partials() has given up on
        (redrive_give_up_attempts > 0 and attempt N >= it): it is never re-driven
        again, so the Fix A pre-flight never re-runs on it. If Postgres later
        catches up (the 2026-07-18 backlog), it alerts forever. This sweep closes
        that gap with a two-gate, FAIL-CLOSED convergence check:

          * Gate A (native Postgres): _already_satisfied(msg) -- current_business_state
            in _STAGE_SATISFIED_BY[requested_stage].
          * Gate B (tracker canonical pipeline.json): currentBusinessState for the
            job is ALSO in that same set.

        Both must pass. Anything ambiguous (reader off/None, stage unmapped, job
        absent from canonical, canonical reader unwired, parse error) => NOT
        reaped: the file stays capped and keeps alerting. A reap mirrors the
        'satisfied' path -- mark_applied (burn key immediately) + move to
        processed/ -- and NEVER moves to inbox/ (re-driving a past-stage intent
        regresses state).

        Cost: gate B parses the (large) canonical pipeline.json AT MOST ONCE per
        sweep, and only when >= 1 capped partial has already passed gate A. MUST
        run on the single-writer applier thread (shares _move_to/glob/idempotency
        with scan_inbox).

        Returns {filename: "reaped" | "not_converged" | "skipped"}.
        """
        results: dict[str, str] = {}
        # First pass: gate A over capped partials only. Never touch the big
        # canonical file yet.
        a_pass: list[tuple[Path, IntentMessage]] = []
        for path in sorted(self.partial_dir.glob("*_INTENT_*.json")):
            if not self._is_capped(path):
                continue  # non-capped -> handled by redrive + pre-flight
            try:
                msg = parse_intent_file(path)
            except IntentParseError:
                results[path.name] = "skipped"
                continue
            if self._already_satisfied(msg):
                a_pass.append((path, msg))
            else:
                results[path.name] = "not_converged"
        if not a_pass:
            return results

        # Second gate: parse the canonical pipeline.json ONCE. Unwired reader or a
        # failed/empty parse => fail closed (nothing reaps).
        canonical: dict[str, str] = {}
        if self.canonical_state_reader is not None:
            try:
                canonical = self.canonical_state_reader() or {}
            except Exception:
                logger.debug(
                    "reaper: canonical pipeline read failed; fail-closed",
                    exc_info=True,
                )
                canonical = {}

        for path, msg in a_pass:
            satisfied_by = _STAGE_SATISFIED_BY.get(msg.requested_stage)
            canonical_state = canonical.get(msg.job_id)
            if satisfied_by and canonical_state and canonical_state in satisfied_by:
                self.idempotency.mark_applied(
                    msg.idempotency_key, message_id=msg.message_id
                )
                self._move_to(path, self.processed_dir)
                results[path.name] = "reaped"
                logger.info(
                    "intent-applier: reaped converged capped partial %s "
                    "(job=%s stage=%s canonical=%s; PG+canonical agree) — auto-cleared",
                    path.name, msg.job_id, msg.requested_stage, canonical_state,
                )
            else:
                results[path.name] = "not_converged"
        return results
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/test_applier.py::TestReapConvergedPartials -v`
Expected: PASS (9 passed)

- [ ] **Step 6: Run the whole applier suite for regressions**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/ -v`
Expected: PASS (all prior tests + the new ones)

- [ ] **Step 7: Commit**

```bash
git add intent_applier/applier.py tests/intent_applier/test_applier.py
git commit -m "$(cat <<'EOF'
feat(intent-applier): two-gate convergence-reaper for capped partials

reap_converged_partials() clears CAPPED partials that native PG (gate A,
reuses _already_satisfied) AND the tracker canonical pipeline.json (gate B)
both show at/past target. Fail-closed; mark_applied + move to processed/,
never inbox/. Canonical file parsed at most once per sweep, only after a
gate-A candidate exists.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Subscriber flag + wiring + `reap_converged_partials()`

Give the reaper its own default-off flag, wire the canonical reader in `startup()`, and add the flag-gated subscriber method.

**Files:**
- Modify: `events/subscribers/tracker_intent_applier.py`
- Test: `tests/events/subscribers/test_tracker_intent_applier.py`

**Interfaces:**
- Consumes: `IntentApplier.reap_converged_partials()` (Task 2), `build_default_canonical_reader` (Task 1).
- Produces:
  - `_reap_enabled_from_env() -> bool` (flag `TRACKER_APPLIER_REAP_CONVERGED_ENABLED`, default False)
  - `TrackerIntentApplierSubscriber.reap_converged_partials(self) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/events/subscribers/test_tracker_intent_applier.py` (mirror the existing `_redrive_enabled_from_env` / `subscriber` fixture patterns; add `_reap_enabled_from_env` to the import from `events.subscribers.tracker_intent_applier`):

```python
class TestReapEnabledFromEnv:
    def test_default_is_disabled(self, monkeypatch):
        monkeypatch.delenv("TRACKER_APPLIER_REAP_CONVERGED_ENABLED", raising=False)
        assert _reap_enabled_from_env() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
    def test_truthy_values_enable(self, monkeypatch, val):
        monkeypatch.setenv("TRACKER_APPLIER_REAP_CONVERGED_ENABLED", val)
        assert _reap_enabled_from_env() is True

    @pytest.mark.parametrize("val", ["0", "off", "no", ""])
    def test_other_values_stay_disabled(self, monkeypatch, val):
        monkeypatch.setenv("TRACKER_APPLIER_REAP_CONVERGED_ENABLED", val)
        assert _reap_enabled_from_env() is False


class TestSubscriberReap:
    def test_flag_off_is_noop(self, subscriber):
        subscriber._reap_enabled = False
        subscriber._applier = MagicMock()
        assert subscriber.reap_converged_partials() == 0
        subscriber._applier.reap_converged_partials.assert_not_called()

    def test_flag_on_calls_applier_and_counts_reaped(self, subscriber):
        subscriber._reap_enabled = True
        subscriber._applier = MagicMock()
        subscriber._applier.reap_converged_partials.return_value = {
            "a.rd5.json": "reaped",
            "b.rd5.json": "not_converged",
            "c.rd5.json": "reaped",
        }
        assert subscriber.reap_converged_partials() == 2
        subscriber._applier.reap_converged_partials.assert_called_once()

    def test_flag_on_but_applier_not_built_is_noop(self, subscriber):
        subscriber._reap_enabled = True
        subscriber._applier = None
        assert subscriber.reap_converged_partials() == 0
```

> Note: confirm the top of the test file imports `MagicMock` and `pytest`, and that `_reap_enabled_from_env` is added to the existing `from events.subscribers.tracker_intent_applier import (...)` block. If the `subscriber` fixture doesn't already exist, mirror the one used by `TestSubscriberRedrive` (a `TrackerIntentApplierSubscriber` with `_applier` set to a `MagicMock`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/events/subscribers/test_tracker_intent_applier.py -v -k "Reap or reap"`
Expected: FAIL — `ImportError: cannot import name '_reap_enabled_from_env'`

- [ ] **Step 3: Add the flag helper**

In `events/subscribers/tracker_intent_applier.py`, after `_redrive_enabled_from_env` (~line 57):

```python
def _reap_enabled_from_env() -> bool:
    """Feature flag for the convergence-reaper. Default OFF.

    Independent of TRACKER_APPLIER_REDRIVE_ENABLED: the reaper never POSTs to
    :4100 (it reads native PG + the canonical pipeline.json and moves a file), so
    it does not need the :4100 idempotent-no-op hard gate. Default-off lets us
    enable it deliberately after a soak.
    """
    return os.environ.get(
        "TRACKER_APPLIER_REAP_CONVERGED_ENABLED", "0"
    ).strip().lower() in _TRUTHY
```

- [ ] **Step 4: Wire the canonical reader and read the flag**

Add the import (near the existing `from intent_applier import (...)` block):

```python
from intent_applier import (
    IdempotencyTracker,
    IntentApplier,
    JobOpsClient,
    build_default_canonical_reader,
    build_default_reader,
)
```

In `__init__`, after `self._redrive_config = _redrive_config_from_env()` (~line 129):

```python
        self._reap_enabled = _reap_enabled_from_env()
```

In `startup()`, after `job_state_reader = build_default_reader()` (~line 152):

```python
        # Reaper gate B: fresh {job_id: currentBusinessState} from the tracker
        # canonical pipeline.json under HERMES_ROOT.
        canonical_state_reader = build_default_canonical_reader()
```

Pass it into the `IntentApplier(...)` construction (after `job_state_reader=job_state_reader,`):

```python
            job_state_reader=job_state_reader,
            canonical_state_reader=canonical_state_reader,
```

Extend the `startup()` ready-log to surface the flag — change the existing log call to add `reap_enabled=%s`:

```python
        logger.info(
            "tracker-intent-applier: ready (inbox=%s, jobops=%s, redrive_enabled=%s, "
            "reap_enabled=%s, preflight=%s, give_up_attempts=%s)",
            self._mailbox["inbox"],
            self._jobops_url,
            self._redrive_enabled,
            self._reap_enabled,
            job_state_reader is not None,
            self._redrive_config.get("redrive_give_up_attempts"),
        )
```

- [ ] **Step 5: Add the subscriber method**

Add after `redrive_partials` (end of the class):

```python
    def reap_converged_partials(self) -> int:
        """Flag-gated wrapper: reap converged capped partials iff enabled.

        Returns the number reaped this sweep (0 when disabled or nothing
        converged). IntentApplier.reap_converged_partials() is pure/always-acts;
        THIS method is the feature flag.
        """
        if not self._reap_enabled or self._applier is None:
            return 0
        results = self._applier.reap_converged_partials()
        reaped = sum(1 for v in results.values() if v == "reaped")
        if reaped:
            logger.info(
                "tracker-intent-applier: reaped %d converged capped partial(s)",
                reaped,
            )
        return reaped
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/events/subscribers/test_tracker_intent_applier.py -v`
Expected: PASS (existing + new)

- [ ] **Step 7: Commit**

```bash
git add events/subscribers/tracker_intent_applier.py tests/events/subscribers/test_tracker_intent_applier.py
git commit -m "$(cat <<'EOF'
feat(tracker-applier): default-off reap flag + canonical reader wiring

TRACKER_APPLIER_REAP_CONVERGED_ENABLED gates a reap_converged_partials()
wrapper; startup() wires build_default_canonical_reader() as gate B.
Independent of REDRIVE_ENABLED (reaper never POSTs to :4100).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Call the reaper from the applier thread

Invoke the reaper once/min on the single-writer applier thread, right after `redrive_partials()`. This mirrors the existing (untested) redrive wiring — no unit test for the loop body itself; correctness is covered by Task 3's subscriber test.

**Files:**
- Modify: `events/gateway_integration.py` (~lines 909-915, inside `_applier_poll_loop`)

**Interfaces:**
- Consumes: `TrackerIntentApplierSubscriber.reap_converged_partials()` (Task 3).
- Produces: nothing (wiring only).

- [ ] **Step 1: Add the reaper call**

In `events/gateway_integration.py`, inside `_applier_poll_loop`, locate the once/min block:

```python
        now = time.monotonic()
        if _applier_subscriber is not None and now - last_redrive >= REDRIVE_INTERVAL_SECONDS:
            try:
                _applier_subscriber.redrive_partials()
            except Exception:
                logger.exception("tracker-intent-applier redrive failed")
            last_redrive = now
```

Change it to add the reaper call *before* `last_redrive = now`:

```python
        now = time.monotonic()
        if _applier_subscriber is not None and now - last_redrive >= REDRIVE_INTERVAL_SECONDS:
            try:
                _applier_subscriber.redrive_partials()
            except Exception:
                logger.exception("tracker-intent-applier redrive failed")
            # Reap capped partials PG+canonical both show converged (own flag,
            # default off). Runs after redrive so it mops up exactly what redrive
            # just classified capped. Single-writer thread; own try/except so a
            # reap failure never stalls the loop.
            try:
                _applier_subscriber.reap_converged_partials()
            except Exception:
                logger.exception("tracker-intent-applier reap failed")
            last_redrive = now
```

- [ ] **Step 2: Verify the module imports and the call is present**

Run: `PYTHONPATH=$(pwd) python -c "import events.gateway_integration as g; import inspect; src = inspect.getsource(g._applier_poll_loop); assert 'reap_converged_partials()' in src and 'reap failed' in src; print('reaper wired into _applier_poll_loop OK')"`
Expected: `reaper wired into _applier_poll_loop OK`

- [ ] **Step 3: Run the gateway integration suite for regressions**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/events/test_gateway_integration.py -v`
Expected: PASS (no regressions)

- [ ] **Step 4: Commit**

```bash
git add events/gateway_integration.py
git commit -m "$(cat <<'EOF'
feat(gateway): call convergence-reaper after redrive on applier thread

Once/min, same single-writer thread as redrive_partials, own try/except.
Flag-gated inside the subscriber (default off).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Remove dead `max_redrive_attempts`

Cleanup. `max_redrive_attempts` is stored but never used in logic — only `redrive_give_up_attempts` caps. Removing it stops it misleading readers (it misled the incident memory). Isolated so it can be reviewed/rejected independently.

**Files:**
- Modify: `intent_applier/applier.py` (ctor param ~line 132, assignment ~line 153, comment refs ~lines 156/473)
- Modify: `events/subscribers/tracker_intent_applier.py` (`_redrive_config_from_env`, ~line 77)
- Test: none new — existing suites must still pass.

**Interfaces:**
- Consumes: nothing.
- Produces: `IntentApplier.__init__` no longer accepts `max_redrive_attempts`; `_redrive_config_from_env()` no longer emits that key.

- [ ] **Step 1: Find every reference**

Run: `grep -rn "max_redrive_attempts\|REDRIVE_MAX_ATTEMPTS" intent_applier/ events/ tests/`
Expected references (fix all): `intent_applier/applier.py` (param, assignment, 2 comments), `events/subscribers/tracker_intent_applier.py` (`_i("TRACKER_APPLIER_REDRIVE_MAX_ATTEMPTS", 5)` in `_redrive_config_from_env`). If any **test** passes `max_redrive_attempts=`, remove that kwarg from the test call in the same step.

- [ ] **Step 2: Remove from `applier.py`**

Delete the `max_redrive_attempts: int = 5,` line from `__init__` (~line 132) and the `self.max_redrive_attempts = max_redrive_attempts` line (~line 153). In the two nearby comments that mention "Past max_redrive_attempts the backoff is already pinned at redrive_max_backoff", reword to "Past the give-up attempt count the backoff is already pinned at redrive_max_backoff" (drop the dead-symbol reference).

- [ ] **Step 3: Remove from the subscriber config**

In `events/subscribers/tracker_intent_applier.py` `_redrive_config_from_env`, delete the line:

```python
        "max_redrive_attempts": _i("TRACKER_APPLIER_REDRIVE_MAX_ATTEMPTS", 5),
```

(Leaving it would now raise `TypeError: unexpected keyword argument` when spread into `IntentApplier`.)

- [ ] **Step 4: Run the full affected suites**

Run: `PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/ tests/events/subscribers/test_tracker_intent_applier.py -v`
Expected: PASS. If a test failed on a removed kwarg, it was fixed in Step 1.

- [ ] **Step 5: Commit**

```bash
git add intent_applier/applier.py events/subscribers/tracker_intent_applier.py tests/
git commit -m "$(cat <<'EOF'
refactor(intent-applier): remove dead max_redrive_attempts field

Stored but never used in logic; only redrive_give_up_attempts caps. Removing
it (and the TRACKER_APPLIER_REDRIVE_MAX_ATTEMPTS env read) stops it misleading
readers about how capping works.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Enablement config (parent repo, activates on next natural restart)

Re-enable a finite re-drive cap **and** turn the reaper on together. They MUST ship together: re-enabling capping without the reaper recreates the exact stranding bug. This is a config change in the parent `~/.hermes` repo, not agent-src.

**Files:**
- Modify: `~/.hermes/profiles/main/.env`

**Interfaces:**
- Consumes: `_redrive_config_from_env` (`TRACKER_APPLIER_REDRIVE_GIVE_UP_ATTEMPTS`), `_reap_enabled_from_env` (`TRACKER_APPLIER_REAP_CONVERGED_ENABLED`).
- Produces: nothing (runtime config).

- [ ] **Step 1: Add the two env vars**

In `~/.hermes/profiles/main/.env`, near the existing `TRACKER_APPLIER_REDRIVE_ENABLED=1` line, add:

```
# Re-enable a finite re-drive cap: genuinely-stuck partials stop hammering :4100
# after 5 attempts (~1h of backoff). MUST be paired with the reaper below, or
# capped-but-converged partials strand and alert forever (2026-07-18 backlog).
TRACKER_APPLIER_REDRIVE_GIVE_UP_ATTEMPTS=5
# Convergence-reaper: auto-clear capped partials PG+canonical both show converged.
TRACKER_APPLIER_REAP_CONVERGED_ENABLED=1
```

- [ ] **Step 2: Verify the file parses and the values are present**

Run: `grep -n "TRACKER_APPLIER_REDRIVE_GIVE_UP_ATTEMPTS\|TRACKER_APPLIER_REAP_CONVERGED_ENABLED" ~/.hermes/profiles/main/.env`
Expected: both lines present with `=5` and `=1`.

- [ ] **Step 3: Commit in the parent repo**

```bash
cd ~/.hermes && git add profiles/main/.env && git commit -m "$(cat <<'EOF'
config(tracker-applier): re-enable finite re-drive cap + convergence-reaper

GIVE_UP_ATTEMPTS=5 stops stuck partials hammering :4100; REAP_CONVERGED_ENABLED=1
clears capped-but-converged ones (PG+canonical agree) so they don't alert forever.
Activates on the next natural gateway restart.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Report, do NOT restart**

Do not restart the gateway. Report to the operator: the change is committed and inert; it activates on the next *natural* gateway restart. Include the current gateway PID and time. After the next restart, confirm the `startup()` log shows `reap_enabled=True` and `give_up_attempts=5`, and watch for `reaped ... converged capped partial(s)` log lines plus the `TRACKER_PARTIAL_BACKLOG` alert re-arming without manual intervention.

---

## Self-Review

**1. Spec coverage:**
- Operating model A (finite cap + reaper) → Task 6 (cap=5) + Tasks 2-4 (reaper). ✓
- Two-gate fail-closed reap (A: PG via `_already_satisfied`; B: canonical `currentBusinessState`) → Task 2. ✓
- Gate B second, parse ≤1×/sweep, only after a gate-A candidate → Task 2 (`a_pass` gate + `test_gate_a_behind_never_parses_canonical`, `test_canonical_parsed_once_per_sweep`). ✓
- Reap action = mark_applied + move to processed/, never inbox/ → Task 2 (`test_capped_and_both_gates_converged_is_reaped`). ✓
- Capped-only scope; give_up=0 ⇒ no-op → Task 2 (`test_non_capped_partial_is_ignored`, `test_give_up_zero_reaps_nothing`). ✓
- Own default-off flag independent of REDRIVE_ENABLED → Task 3. ✓
- Single-writer thread, after redrive, once/min → Task 4. ✓
- Observability: per-reap INFO + reaped count in tick log → Task 2 (log line) + Task 3 (count). ✓
- `.stage` legacy vs `currentBusinessState` correction → Task 1 module + `test_legacy_list_shaped_jobs_yields_empty_map`. ✓
- Remove dead `max_redrive_attempts` → Task 5. ✓
- No auto-restart → Task 6 Step 4. ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". Every code step shows complete code. ✓

**3. Type consistency:** `canonical_state_reader: Optional[Callable[[], dict[str, str]]]` consistent across Tasks 1-3. `reap_converged_partials()` returns `dict[str, str]` (applier) consumed as `.values()` count in the subscriber (Task 3) and ignored in the gateway (Task 4). `build_default_canonical_reader` exported (Task 1) and imported (Task 3) under the same name. `_reap_enabled_from_env` defined and imported under the same name. ✓
