"""One-pass orchestration for the P6 fleet controller.

The controller is the only module here that touches the live box: psutil
snapshots, transcript files, the EventBus, the state file, the singleton
lock. Every decision is delegated to the pure planner; every irreversible
action is delegated to the executor — and the executor is only ever
constructed inside the doubly-gated enforce branch.

Fail-closed inventory (each is pinned by a test):
  * unknown/malformed mode           -> behaves as disabled, exits quietly
  * malformed config values          -> exit 2, nothing touched
  * lock held by a live sibling      -> exit 3, nothing touched
  * EventBus unreachable             -> no trigger evidence, no action
  * corrupt state file               -> strikes reset, never inherited
  * state save failure               -> pass abandoned before any emission
  * enforce without BOTH gates       -> planner runs demoted to shadow
  * any revalidation mismatch        -> whole intent cancelled
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from claude_fleet_control import planner
from claude_fleet_control.models import (
    DECISION_ENFORCE_PROJECTED,
    DECISION_SHADOW_PROJECTED,
    MODE_DISABLED,
    MODE_ENFORCE,
    MODE_SHADOW,
    REASON_STATE_CORRUPT,
    RESULT_CANCELLED,
    RESULT_FAILED,
    RESULT_HARD_TERMINATED,
    RESULT_NO_ACTION,
    RESULT_SHADOW_PROJECTED,
    VALID_MODES,
    FleetPlan,
    FleetPolicy,
    FleetResult,
    ProcessRecord,
    ProcessSnapshot,
)

logger = logging.getLogger(__name__)

EVENT_SOURCE = "claude-fleet-controller"
_STATE_VERSION = 1

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_LOCK_HELD = 3
EXIT_RUNTIME_FAILURE = 4

_NUMERIC_POLICY_FIELDS = (
    "fleet_min_roots",
    "d7_max_age_seconds",
    "idle_min_minutes",
    "strikes_required",
    "strike_max_age_seconds",
    "max_trees_per_pass",
    "max_tree_processes",
    "max_tree_rss_bytes",
    "cooldown_seconds",
)


def default_state_dir() -> Path:
    return Path.home() / ".hermes" / "fleet_control"


def default_config_path() -> Path:
    return Path(__file__).resolve().parent / "config.json"


def load_policy(config_path: Path) -> Tuple[Optional[FleetPolicy], List[str]]:
    """Parse config into a policy. Returns (policy, notes).

    A missing/unparseable file or a malformed numeric is a HARD config error
    (policy None) — the caller exits 2 without touching anything. An unknown
    ``mode`` alone degrades to disabled with a note, per the approved plan.
    """
    notes: List[str] = []
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"config unreadable: {exc}"]
    if not isinstance(raw, dict):
        return None, ["config is not a JSON object"]

    mode = str(raw.get("mode", "")).strip().lower()
    if mode not in VALID_MODES:
        notes.append(f"mode_invalid:{mode!r}")
        mode = MODE_DISABLED

    kwargs: Dict[str, object] = {
        "mode": mode,
        "policy_version": str(raw.get("policy_version", "p6-unversioned")),
    }
    for field_name in _NUMERIC_POLICY_FIELDS:
        if field_name not in raw:
            continue
        value = raw[field_name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return None, [f"config field {field_name} malformed: {value!r}"]
        default = getattr(FleetPolicy, field_name)
        kwargs[field_name] = type(default)(value)

    approved = raw.get("approved_enforce_digest")
    kwargs["approved_enforce_digest"] = str(approved) if approved else None
    return FleetPolicy(**kwargs), notes


# ---------------------------------------------------------------- lock

class SingletonLock:
    """Kernel-enforced single-instance lock. The OS releases it when the
    process dies, so a killed pass cannot wedge the lane (a bare lockfile
    would — see memory: a .lock sidecar's existence is not ownership)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - POSIX fallback for portability
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._handle.close()
            self._handle = None


# ---------------------------------------------------------------- live adapters

def live_snapshot() -> ProcessSnapshot:
    """Whole-box census via psutil, in two phases.

    Phase 1 reads only CHEAP fields (pid/ppid/name/create_time) for every
    process. Phase 2 enriches ONLY the Claude-named processes and their trees
    (``planner.enrichment_pids``) with the expensive fields
    (cmdline/exe/username/rss). Each expensive field opens a per-process
    handle on Windows, so fetching them for all ~600 processes cost tens of
    seconds — worst during the very churn storm P6 exists to act on. Every
    process the planner actually classifies or protects is Claude-named or a
    descendant of one, so the un-enriched majority never needs those fields.

    A process that could not be enriched (access denied, recycled between
    phases) is marked incomplete, which protects its whole tree — fail-safe.
    Non-enriched, non-Claude processes keep empty expensive fields; they are
    never tree members, so those fields are never read for them."""
    import psutil

    # Phase 1 — cheap whole-table census.
    cheap: List[ProcessRecord] = []
    complete = True
    try:
        for proc in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
            try:
                info = proc.info
                cheap.append(
                    ProcessRecord(
                        pid=int(info["pid"]),
                        ppid=info.get("ppid"),
                        name=str(info.get("name") or ""),
                        exe=None,
                        cmdline=(),
                        create_time=float(info.get("create_time") or 0.0),
                        rss=0,
                        username=None,
                        # Cheap records are complete for how they are USED
                        # (name + ancestry only); they are never tree members.
                        complete=(
                            info.get("name") is not None
                            and info.get("create_time") is not None
                        ),
                    )
                )
            except Exception:
                complete = False
                continue
    except Exception:
        complete = False

    # Phase 2 — enrich only Claude processes and their trees.
    targets = planner.enrichment_pids(cheap)
    by_pid = {r.pid: r for r in cheap}
    for pid in targets:
        base = by_pid.get(pid)
        if base is None:
            continue
        try:
            proc = psutil.Process(pid)
            with proc.oneshot():
                if abs(proc.create_time() - base.create_time) > 1.0:
                    # Recycled between phases — do not graft a stranger's
                    # fields onto this slot. Mark incomplete: its tree is
                    # protected rather than acted on with mixed identity.
                    by_pid[pid] = dataclasses.replace(base, complete=False)
                    complete = False
                    continue
                mem = proc.memory_info()
                enriched = dataclasses.replace(
                    base,
                    exe=proc.exe(),
                    cmdline=tuple(str(a) for a in (proc.cmdline() or [])),
                    rss=int(getattr(mem, "rss", 0) or 0),
                    username=proc.username(),
                    complete=True,
                )
            by_pid[pid] = enriched
        except Exception:
            by_pid[pid] = dataclasses.replace(base, complete=False)
            complete = False

    records = tuple(by_pid[r.pid] for r in cheap)
    return ProcessSnapshot(taken_at=time.time(), records=records, complete=complete)


def _projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def live_transcript_facts(root: ProcessRecord) -> Tuple[Optional[Path], Optional[Tuple[str, float]], Optional[List[Tuple[str, float]]]]:
    """Gather (folder, exact_entry, folder_entries) for one CLI root.

    Reads the process cwd only after re-verifying (pid, create_time) — a
    recycled PID must not donate its cwd to a dead session's record.
    """
    import psutil

    try:
        proc = psutil.Process(root.pid)
        if abs(proc.create_time() - root.create_time) > 1.0:
            return None, None, None
        cwd = proc.cwd()
    except Exception:
        return None, None, None
    if not cwd:
        return None, None, None

    folder = _projects_dir() / planner.mangle_cwd(cwd)
    exact_entry: Optional[Tuple[str, float]] = None
    session_uuid = planner.resume_session_uuid(root.cmdline)
    if session_uuid is not None:
        exact_path = folder / f"{session_uuid}.jsonl"
        try:
            exact_entry = (str(exact_path), exact_path.stat().st_mtime)
        except OSError:
            exact_entry = None

    entries: Optional[List[Tuple[str, float]]] = None
    try:
        if folder.is_dir():
            entries = []
            for item in folder.glob("*.jsonl"):
                try:
                    entries.append((str(item), item.stat().st_mtime))
                except OSError:
                    continue
    except OSError:
        entries = None
    return folder, exact_entry, entries


# ---------------------------------------------------------------- controller

class Controller:
    def __init__(
        self,
        *,
        config_path: Optional[Path] = None,
        state_dir: Optional[Path] = None,
        allow_enforce: bool = False,
        now_fn: Callable[[], float] = time.time,
        snapshot_fn: Callable[[], ProcessSnapshot] = live_snapshot,
        transcript_facts_fn: Callable[[ProcessRecord], tuple] = live_transcript_facts,
        bus_factory: Optional[Callable[[], object]] = None,
        lock_factory: Optional[Callable[[Path], object]] = None,
        executor_factory: Optional[Callable[..., object]] = None,
    ) -> None:
        self.config_path = config_path or default_config_path()
        self.state_dir = state_dir or default_state_dir()
        self.allow_enforce = allow_enforce
        self._now = now_fn
        self._snapshot = snapshot_fn
        self._transcript_facts = transcript_facts_fn
        self._bus_factory = bus_factory or self._default_bus
        self._lock_factory = lock_factory or SingletonLock
        # Test seam only. Production enforce wiring stays inside _enforce();
        # shadow paths never call this factory (a test pins that).
        self._executor_factory = executor_factory
        self.executor_constructed = False

    @staticmethod
    def _default_bus():
        from events.bus import EventBus

        return EventBus()

    # ------------------------------------------------------------ state

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    def _load_state(self) -> Tuple[Dict[str, object], bool]:
        """Returns (state, corrupt). Corrupt or wrong-shaped state resets to
        empty AND flags the pass — an inherited garbage strike must never
        become somebody's second strike."""
        empty: Dict[str, object] = {
            "version": _STATE_VERSION,
            "strikes": {},
            "last_enforce_intent_at": None,
        }
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty, False
        except (OSError, ValueError):
            return empty, True
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("strikes"), dict)
            or raw.get("version") != _STATE_VERSION
        ):
            return empty, True
        last = raw.get("last_enforce_intent_at")
        if last is not None and not isinstance(last, (int, float)):
            return empty, True
        return raw, False

    def _save_state(self, state: Mapping[str, object]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ evidence

    def _query_pressure_events(self, bus, now: float) -> Optional[List[Dict[str, object]]]:
        from events.schema import EventType

        since = datetime.fromtimestamp(now - 3600.0, tz=timezone.utc).isoformat()
        try:
            events = bus.query(event_type=EventType.RESOURCE_PRESSURE, since=since)
        except Exception as exc:
            logger.warning("fleet-controller: pressure query failed: %s", exc)
            return None
        return [
            {"event_id": e.event_id, "timestamp": e.timestamp, "payload": e.payload}
            for e in events
        ]

    def _assess_all(
        self, snapshot: ProcessSnapshot, policy: FleetPolicy, now: float
    ) -> Tuple[List, int]:
        records = snapshot.records
        roots = planner.find_cli_roots(records)

        # Folder tenancy: how many LIVE roots resolve to each projects folder.
        # Needed before per-root resolution so shared-cwd fallback can refuse.
        folder_by_root: Dict[int, Optional[str]] = {}
        tenancy: Dict[str, int] = {}
        facts_cache: Dict[int, tuple] = {}
        for root in roots:
            facts = self._transcript_facts(root)
            facts_cache[root.pid] = facts
            folder = facts[0]
            key = str(folder) if folder is not None else None
            folder_by_root[root.pid] = key
            if key is not None:
                tenancy[key] = tenancy.get(key, 0) + 1

        me = os.getpid()
        by_pid = {r.pid: r for r in records}
        protected_pids = frozenset(
            {me} | {r.pid for r in planner.ancestor_chain(me, by_pid)}
        )
        current_user = os.environ.get("USERNAME") or os.environ.get("USER") or ""

        assessments = []
        for root in roots:
            members = planner.collect_tree(root, records)
            folder, exact, entries = facts_cache[root.pid]
            sharing = tenancy.get(folder_by_root[root.pid] or "", 1)
            transcript = planner.resolve_transcript_decision(exact, entries, sharing)
            assessments.append(
                planner.assess_tree(
                    root,
                    members,
                    transcript,
                    now=now,
                    policy=policy,
                    protected_pids=protected_pids,
                    current_user=current_user,
                )
            )
        return assessments, len(roots)

    # ------------------------------------------------------------ run

    def run_once(self) -> Tuple[int, Optional[FleetResult]]:
        policy, notes = load_policy(self.config_path)
        if policy is None:
            logger.error("fleet-controller: config error: %s", "; ".join(notes))
            return EXIT_CONFIG_ERROR, None
        if policy.mode == MODE_DISABLED:
            logger.info("fleet-controller: mode disabled (%s); no pass", "; ".join(notes) or "configured")
            return EXIT_OK, None

        # Enforce needs BOTH gates; anything less runs the pass as shadow.
        enforce_authorized = (
            policy.mode == MODE_ENFORCE
            and self.allow_enforce
            and policy.approved_enforce_digest == policy.digest()
        )
        effective_policy = policy
        extra_reasons: List[str] = list(notes)
        if policy.mode == MODE_ENFORCE and not enforce_authorized:
            effective_policy = dataclasses.replace(policy, mode=MODE_SHADOW)
            extra_reasons.append("enforce_gate_blocked")

        lock = self._lock_factory(self.state_dir / "controller.lock")
        if not lock.acquire():
            logger.warning("fleet-controller: another instance holds the lock; skipping")
            return EXIT_LOCK_HELD, None
        try:
            return self._run_locked(effective_policy, enforce_authorized, extra_reasons)
        finally:
            lock.release()

    def _run_locked(
        self,
        policy: FleetPolicy,
        enforce_authorized: bool,
        extra_reasons: Sequence[str],
    ) -> Tuple[int, Optional[FleetResult]]:
        now = self._now()
        run_id = str(uuid.uuid4())
        reasons = list(extra_reasons)

        state, corrupt = self._load_state()
        if corrupt:
            reasons.append(REASON_STATE_CORRUPT)

        try:
            bus = self._bus_factory()
        except Exception as exc:
            logger.error("fleet-controller: EventBus unavailable: %s", exc)
            return EXIT_RUNTIME_FAILURE, None

        raw_events = self._query_pressure_events(bus, now)
        if raw_events is None:
            pressure = planner.PressureEvidence(False, "bus_error")
        else:
            pressure = planner.evaluate_pressure(raw_events, now, policy)

        snapshot = self._snapshot()
        assessments, root_count = self._assess_all(snapshot, policy, now)

        prior_strikes = state.get("strikes") if not corrupt else {}
        last_intent = state.get("last_enforce_intent_at")
        plan = planner.build_plan(
            assessments=assessments,
            fleet_root_count=root_count,
            pressure=pressure,
            prior_strikes=prior_strikes if isinstance(prior_strikes, dict) else {},
            last_enforce_intent_at=last_intent if isinstance(last_intent, (int, float)) else None,
            policy=policy,
            now=now,
            run_id=run_id,
            extra_reasons=reasons,
        )

        new_state = {
            "version": _STATE_VERSION,
            "strikes": dict(plan.new_strikes),
            "last_enforce_intent_at": state.get("last_enforce_intent_at"),
        }
        try:
            self._save_state(new_state)
        except OSError as exc:
            logger.error("fleet-controller: state save failed, abandoning pass: %s", exc)
            return EXIT_RUNTIME_FAILURE, None

        result = self._emit_and_maybe_act(bus, plan, new_state, enforce_authorized)
        exit_code = EXIT_OK if result is not None and result.status != RESULT_FAILED else EXIT_RUNTIME_FAILURE
        return exit_code, result

    def _emit(self, bus, event_type_name: str, payload: Mapping[str, object], run_id: str) -> bool:
        from events.schema import EventType

        try:
            bus.emit(
                event_type=getattr(EventType, event_type_name),
                source=EVENT_SOURCE,
                payload=dict(payload),
                correlation_id=run_id,
            )
            return True
        except Exception as exc:
            logger.error("fleet-controller: emit %s failed: %s", event_type_name, exc)
            return False

    def _emit_and_maybe_act(
        self, bus, plan: FleetPlan, state: Dict[str, object], enforce_authorized: bool
    ) -> Optional[FleetResult]:
        plan_emitted = self._emit(bus, "CLAUDE_FLEET_PLAN", plan.to_payload(), plan.run_id)

        if plan.decision == DECISION_SHADOW_PROJECTED:
            result = FleetResult(
                run_id=plan.run_id, plan_id=plan.plan_id,
                status=RESULT_SHADOW_PROJECTED, executor_called=False,
                detail="shadow projection only; no executor constructed",
            )
        elif plan.decision == DECISION_ENFORCE_PROJECTED and enforce_authorized:
            if not plan_emitted:
                # Durable plan emission is a precondition for action.
                result = FleetResult(
                    run_id=plan.run_id, plan_id=plan.plan_id,
                    status=RESULT_CANCELLED, executor_called=False,
                    detail="plan emission failed; enforce cancelled",
                )
            else:
                result = self._enforce(bus, plan, state)
        elif plan.decision == DECISION_ENFORCE_PROJECTED:
            # Planner can only project enforce when policy.mode == enforce,
            # and the controller demotes an ungated enforce mode to shadow
            # before planning — reaching here means that invariant broke.
            result = FleetResult(
                run_id=plan.run_id, plan_id=plan.plan_id,
                status=RESULT_CANCELLED, executor_called=False,
                detail="enforce projected without authorization; cancelled",
            )
        else:
            result = FleetResult(
                run_id=plan.run_id, plan_id=plan.plan_id,
                status=RESULT_NO_ACTION, executor_called=False,
                detail=";".join(plan.trigger_reasons) or "no eligible second-strike tree",
            )

        if not self._emit(bus, "CLAUDE_FLEET_RESULT", result.to_payload(), plan.run_id) and not plan_emitted:
            # Neither audit event landed: the pass left no durable trace, so
            # report it as a runtime failure to the scheduled runner's log.
            return FleetResult(
                run_id=plan.run_id, plan_id=plan.plan_id,
                status=RESULT_FAILED, executor_called=result.executor_called,
                detail="audit emission failed for both plan and result",
            )
        return result

    # ------------------------------------------------------------ enforce

    def _enforce(self, bus, plan: FleetPlan, state: Dict[str, object]) -> FleetResult:
        """The doubly-gated action branch. Every step re-derives what the plan
        assumed; the FIRST mismatch cancels the whole intent."""

        def cancelled(detail: str) -> FleetResult:
            return FleetResult(
                run_id=plan.run_id, plan_id=plan.plan_id,
                status=RESULT_CANCELLED, executor_called=False, detail=detail,
            )

        target = plan.selected
        if target is None:
            return cancelled("no selected target on an enforce plan")

        # Gate re-read: config may have changed since the pass began.
        policy2, _notes = load_policy(self.config_path)
        if (
            policy2 is None
            or policy2.mode != MODE_ENFORCE
            or not self.allow_enforce
            or policy2.approved_enforce_digest != policy2.digest()
            or policy2.digest() != plan.policy_digest
        ):
            return cancelled("enforce gates no longer satisfied on re-read")

        now2 = self._now()
        raw_events = self._query_pressure_events(bus, now2)
        pressure2 = (
            planner.evaluate_pressure(raw_events, now2, policy2)
            if raw_events is not None
            else planner.PressureEvidence(False, "bus_error")
        )
        if not pressure2.valid:
            return cancelled(f"pressure revalidation failed: {pressure2.reason_code}")

        snapshot2 = self._snapshot()
        assessments2, root_count2 = self._assess_all(snapshot2, policy2, now2)
        if root_count2 <= policy2.fleet_min_roots:
            return cancelled("fleet census dropped to/below the trigger floor")

        fresh = next(
            (a for a in assessments2 if a.root.identity == target.root_identity), None
        )
        if fresh is None or fresh.protected or not fresh.eligible:
            return cancelled("target no longer eligible on fresh assessment")
        if fresh.strike_key != target.strike_key:
            return cancelled("target strike identity changed (transcript activity)")
        if len(fresh.members) > policy2.max_tree_processes or fresh.total_rss > policy2.max_tree_rss_bytes:
            return cancelled("target exceeded budgets on fresh assessment")
        fresh_identities = tuple(sorted(m.identity for m in fresh.members))
        if set(fresh_identities) - set(target.member_identities):
            return cancelled("target grew new descendants since planning")

        # Durable intent BEFORE the irreversible call: cooldown starts here,
        # whether or not the kill later succeeds.
        state["last_enforce_intent_at"] = now2
        try:
            self._save_state(state)
        except OSError as exc:
            return cancelled(f"could not persist enforce intent: {exc}")
        if not self._emit(
            bus, "CLAUDE_FLEET_PLAN",
            dict(plan.to_payload(), phase="enforce_intent", revalidated_at=now2),
            plan.run_id,
        ):
            return cancelled("intent emission failed; enforce cancelled")

        def executor_snapshot() -> Sequence[ProcessRecord]:
            return self._snapshot().records

        self.executor_constructed = True
        if self._executor_factory is not None:
            executor = self._executor_factory(snapshot_fn=executor_snapshot)
        else:  # pragma: no cover - live wiring, exercised only in production
            from claude_fleet_control.executor import build_production_executor

            executor = build_production_executor(executor_snapshot)

        try:
            report = executor.hard_terminate_tree(target, plan_id=plan.plan_id)
        except Exception as exc:
            return FleetResult(
                run_id=plan.run_id, plan_id=plan.plan_id,
                status=RESULT_FAILED, executor_called=True,
                detail=f"executor raised: {exc}",
            )

        if report.cancelled:
            return FleetResult(
                run_id=plan.run_id, plan_id=plan.plan_id,
                status=RESULT_CANCELLED, executor_called=True, detail=report.detail,
                exited_identities=report.exited_identities,
                surviving_identities=report.surviving_identities,
            )
        return FleetResult(
            run_id=plan.run_id, plan_id=plan.plan_id,
            status=RESULT_HARD_TERMINATED if report.ok else RESULT_FAILED,
            executor_called=True, detail=report.detail,
            exited_identities=report.exited_identities,
            surviving_identities=report.surviving_identities,
        )
