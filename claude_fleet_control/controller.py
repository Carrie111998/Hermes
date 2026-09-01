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

def _live_ppid_map() -> Optional[Dict[int, Optional[int]]]:
    """The whole pid->ppid table in one call, or None to fall back.

    psutil exposes this only as a private platform helper, so it is looked up
    defensively: a psutil upgrade that moves or drops it degrades to the
    per-process ppid path (correct, just slow) instead of raising. An empty
    result is also treated as unavailable -- a real box always has processes,
    so empty means the call did not work, and believing it would hand the
    planner a census with no ancestry at all.
    """
    try:
        from psutil import _psutil_windows as _pw  # type: ignore
    except Exception:
        try:
            from psutil import _psplatform as _pw  # type: ignore
        except Exception:
            return None
    fn = getattr(_pw, "ppid_map", None)
    if fn is None:
        return None
    try:
        m = fn()
    except Exception:
        return None
    if not m:
        return None
    try:
        return {int(k): (int(v) if v is not None else None) for k, v in m.items()}
    except Exception:
        return None


def live_snapshot() -> ProcessSnapshot:
    """Whole-box census via psutil, in three phases.

    Phase 1 reads pid/name for every process and takes the whole pid->ppid
    table in ONE call. Phase 1b fills in ``create_time`` for
    ``planner.census_ctime_pids`` only. Phase 2 enriches ONLY the
    Claude-named processes and their trees (``planner.enrichment_pids``) with
    the remaining expensive fields (cmdline/exe/username/rss). Every one of
    those opens a per-process handle on Windows, so fetching them across the
    whole table costs tens of seconds — worst during the very churn storm P6
    exists to act on. Every process the planner actually classifies or
    protects is Claude-named or a descendant of one, so the majority never
    needs any of them.

    PPID WAS THE MISSED FIELD (2026-09-01), and it is not intuitive: ppid
    looks as cheap as pid or name, but psutil's Windows implementation has no
    per-process ppid — it takes a WHOLE-SYSTEM Toolhelp snapshot to answer
    each call, so asking every process is quadratic. Measured on this box,
    each the FIRST enumeration in a fresh interpreter (the scheduled task is
    always a fresh interpreter, so only first-call numbers are honest here):

        process_iter(pid)                          0.14s
        process_iter(pid, name)                    0.20s
        process_iter(pid, ppid, name)             56.48s
        _psutil_windows.ppid_map()                 0.06s   <- whole table, one call

    That is the same map, ~800x cheaper. Phase 1 now costs well under a
    second where it had grown to ~50s, which drove the controller's pass from
    a steady 15s past the task's PT4M30S ExecutionTimeLimit — killed by Task
    Scheduler (event 329) every cycle, so fleet growth disabled the guard
    that exists to contain fleet growth and its silence watchdog with it. See
    loops claim tray-329-kills-fleet-controller-20260901.

    BEWARE THE WARM/COLD TRAP that hid this: a SECOND process_iter in the
    same interpreter costs ~1-3s because psutil caches, so an A/B run inside
    one script credits whichever field set ran first with the entire cold
    cost. That mismeasurement first blamed create_time here. Compare field
    sets only across separate interpreters.

    A process that could not be enriched (access denied, recycled between
    phases) is marked incomplete, which protects its whole tree — fail-safe.
    A process whose create_time could not be READ is marked incomplete for
    the same reason -- but one that has EXITED between phases is dropped from
    the census instead, because the old single-phase census would never have
    listed it and an incomplete member protects its whole tree, so keeping
    dead rows would let ordinary churn quietly veto every cull. A process OUTSIDE the
    create_time closure keeps create_time 0.0 and stays complete, because it
    is never a tree member or an ancestor and so that field is never read for
    it; non-enriched, non-Claude processes keep empty expensive fields on the
    same argument."""
    import psutil

    # Phase 1 — genuinely cheap whole-table census: pid/name from
    # process_iter, and the entire pid->ppid table from ONE snapshot call.
    # Neither ppid nor create_time is asked of process_iter here; see the
    # docstring for why ppid in particular is not the cheap field it looks
    # like. An empty/failed map degrades to the per-process path rather than
    # inventing an ancestry-free census, because a record with no ppid is not
    # linkable to its tree.
    ppid_map = _live_ppid_map()

    cheap: List[ProcessRecord] = []
    complete = True
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                info = proc.info
                pid = int(info["pid"])
                if ppid_map is not None and pid in ppid_map:
                    ppid, ppid_ok = ppid_map[pid], True
                else:
                    # Either no map at all, or this pid was BORN between the
                    # map call and this enumeration. Ask it directly: that is
                    # a handful of processes, not the whole table, so it keeps
                    # the fix's win while still answering correctly.
                    #
                    # It must not simply be marked incomplete. An incomplete
                    # member protects its WHOLE tree (planner's default-deny),
                    # so quietly failing every process that raced the snapshot
                    # would bias the enforce lane toward never acting -- a
                    # behaviour change wearing a fail-safe's clothes.
                    try:
                        ppid, ppid_ok = proc.ppid(), True
                    except psutil.NoSuchProcess:
                        continue          # exited mid-enumeration; not a row
                    except Exception:
                        ppid, ppid_ok = None, False
                cheap.append(
                    ProcessRecord(
                        pid=pid,
                        ppid=ppid,
                        name=str(info.get("name") or ""),
                        exe=None,
                        cmdline=(),
                        create_time=0.0,
                        rss=0,
                        username=None,
                        # Cheap records are complete for how they are USED
                        # (name + ancestry only); they are never tree members.
                        complete=(info.get("name") is not None and ppid_ok),
                    )
                )
                if not ppid_ok:
                    complete = False
            except Exception:
                complete = False
                continue
    except Exception:
        complete = False

    by_pid = {r.pid: r for r in cheap}

    # Phase 1b — create_time for the ppid closure around Claude-named seeds.
    # A failed read marks the record incomplete; it must NOT silently leave
    # the 0.0 behind, because 0.0 reads as "older than everything" to
    # collect_tree's recycled-ppid guard, which decides tree membership.
    gone: set = set()
    for pid in planner.census_ctime_pids(cheap):
        base = by_pid.get(pid)
        if base is None:
            continue
        try:
            ctime = float(psutil.Process(pid).create_time())
        except psutil.NoSuchProcess:
            # EXITED between phases. Drop it rather than mark it incomplete:
            # a single-phase census would never have listed it at all, and an
            # incomplete member protects its WHOLE tree, so retaining dead
            # processes would let ordinary churn quietly veto every cull.
            gone.add(pid)
            continue
        except Exception:
            by_pid[pid] = dataclasses.replace(base, complete=False)
            complete = False
            continue
        by_pid[pid] = dataclasses.replace(base, create_time=ctime)

    # Phase 2 — enrich only Claude processes and their trees.
    cheap = [by_pid[r.pid] for r in cheap if r.pid not in gone]
    targets = planner.enrichment_pids(cheap)
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
