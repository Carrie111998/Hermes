"""Pure planning core for the P6 fleet controller.

PURITY CONTRACT (pinned by tests): this module imports nothing that can do
I/O and takes every changing input — the snapshot, transcripts, pressure
events, prior strike state, and ``now`` — as a parameter. Same inputs, same
plan, same digest, on any machine at any time.

The primitives generalize the two proven implementations on this box —
``scripts/reap_stray_tests.py`` (create-time-validated ancestry, default-deny
ownership) and the home-root ``cull-claude-sessions.py`` (CLI classification,
transcript-mtime idleness, two-strike keys, infra protection) — without
importing either. The old culler is import-unsafe (top-level psutil sweep)
and must never become a dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from claude_fleet_control.models import (
    ACTION_HARD_TERMINATE,
    DECISION_ENFORCE_PROJECTED,
    DECISION_NO_ACTION,
    DECISION_SHADOW_PROJECTED,
    MODE_ENFORCE,
    REASON_ACTOR_MEMBER,
    REASON_COOLDOWN_ACTIVE,
    REASON_CROSS_USER_MEMBER,
    REASON_DESKTOP_MEMBER,
    REASON_FIRST_STRIKE,
    REASON_FLEET_BELOW_MIN,
    REASON_INCOMPLETE_MEMBER,
    REASON_INFRA_MEMBER,
    REASON_OVERSIZE_PROCESSES,
    REASON_OVERSIZE_RSS,
    REASON_TRANSCRIPT_ACTIVE,
    REASON_TRANSCRIPT_AMBIGUOUS,
    REASON_TRANSCRIPT_FUTURE,
    REASON_TRANSCRIPT_MISSING,
    REASON_TRIGGERS_DISARMED,
    SCHEMA_VERSION,
    TRANSCRIPT_AMBIGUOUS,
    TRANSCRIPT_EXACT,
    TRANSCRIPT_FALLBACK,
    TRANSCRIPT_MISSING,
    FleetPlan,
    FleetPolicy,
    PressureEvidence,
    ProcessRecord,
    TargetSummary,
    TranscriptEvidence,
    TreeAssessment,
)

# ---------------------------------------------------------------- classify

# Live-verified CLI markers (cull-claude-sessions.py, in production since
# 2026-07-08): a session CLI carries --output-format or claude-code in argv;
# Electron helpers carry --type=; the desktop app binary lives under an
# \app\ install dir (\app\claude.exe).
_SESSION_MARKERS = ("--output-format", "claude-code")
_CLI_BASENAMES = {"claude.exe", "claude"}
_DESKTOP_PATH_FRAGMENT = "\\app\\claude.exe"

CLASS_CLI = "cli"
CLASS_DESKTOP = "desktop"
CLASS_OTHER = "other"

# Shared infrastructure that must never be killed even when it appears as a
# descendant of a session tree. Copied verbatim from the proven guard in
# cull-claude-sessions.py (added after the 2026-06-19 incident where a
# recursive cull swept Docker Desktop + WSL and took down the gbrain
# postgres on :5437).
INFRA_PROTECT_NAMES = frozenset({
    "docker desktop.exe", "com.docker.backend.exe", "com.docker.service.exe",
    "com.docker.service", "com.docker.build.exe", "com.docker.cli.exe",
    "com.docker.dev-envs.exe", "dockerd.exe", "docker.exe", "docker-proxy.exe",
    "docker-sandbox.exe", "vmmem", "vmmemwsl", "wsl.exe", "wslservice.exe",
    "wslhost.exe", "vmcompute.exe", "vmwp.exe",
})

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def classify_process(record: ProcessRecord) -> str:
    """Classify a record as cli / desktop / other. Conservative: anything
    unreadable is "other" — it can then only protect a tree, never be one."""
    name = os.path.basename(str(record.name or "")).lower()
    if name not in _CLI_BASENAMES:
        return CLASS_OTHER
    exe = str(record.exe or "").lower()
    if _DESKTOP_PATH_FRAGMENT in exe:
        return CLASS_DESKTOP
    argv = [str(a) for a in record.cmdline]
    if any(a.startswith("--type=") for a in argv):
        return CLASS_DESKTOP  # Electron helper of the desktop app
    joined = " ".join(argv)
    if _DESKTOP_PATH_FRAGMENT in joined.lower().replace("/", "\\"):
        return CLASS_DESKTOP
    if any(marker in joined for marker in _SESSION_MARKERS):
        return CLASS_CLI
    return CLASS_OTHER


def is_infra(record: ProcessRecord) -> bool:
    return str(record.name or "").strip().lower() in INFRA_PROTECT_NAMES


def resume_session_uuid(cmdline: Sequence[str]) -> Optional[str]:
    """Extract the --resume <uuid> (or --resume=<uuid>) argument, structurally.

    Structural on argv elements, never a substring scan of the joined line —
    the joined-scan variant self-matched quoted script bodies (see the
    reap_stray_tests.py incident notes).
    """
    argv = [str(a) for a in (cmdline or [])]
    for i, arg in enumerate(argv):
        if arg == "--resume" and i + 1 < len(argv) and _UUID_RE.match(argv[i + 1]):
            return argv[i + 1].lower()
        if arg.startswith("--resume="):
            candidate = arg.split("=", 1)[1]
            if _UUID_RE.match(candidate):
                return candidate.lower()
    return None


def mangle_cwd(cwd: str) -> str:
    """Claude Code's cwd -> ~/.claude/projects folder mangling (verified on
    this box): drive-colon, path separators and dots all become '-'."""
    return re.sub(r"[:\\/.]", "-", cwd)


# ---------------------------------------------------------------- ancestry

def _by_pid(records: Sequence[ProcessRecord]) -> Dict[int, ProcessRecord]:
    return {r.pid: r for r in records}


def ancestor_chain(pid: int, by_pid: Mapping[int, ProcessRecord]) -> List[ProcessRecord]:
    """Records from ``pid`` upward, following ppid with create_time
    validation: a parent that started AFTER its claimed child means the ppid
    was recycled — stop rather than climb a lie."""
    chain: List[ProcessRecord] = []
    seen: set = set()
    cur = by_pid.get(pid)
    while cur is not None and cur.pid not in seen:
        seen.add(cur.pid)
        chain.append(cur)
        parent = by_pid.get(cur.ppid) if cur.ppid is not None else None
        if parent is None:
            break
        if parent.create_time > cur.create_time:
            break
        cur = parent
    return chain


def find_cli_roots(records: Sequence[ProcessRecord]) -> List[ProcessRecord]:
    """CLI sessions with no CLI ancestor: the fleet census population."""
    by_pid = _by_pid(records)
    roots: List[ProcessRecord] = []
    for r in records:
        if classify_process(r) != CLASS_CLI:
            continue
        chain = ancestor_chain(r.pid, by_pid)
        # chain[0] is r itself; any OTHER cli in the chain makes r non-root.
        if any(classify_process(a) == CLASS_CLI for a in chain[1:]):
            continue
        roots.append(r)
    return roots


def collect_tree(root: ProcessRecord, records: Sequence[ProcessRecord]) -> Tuple[ProcessRecord, ...]:
    """Root plus every descendant reachable via create-time-validated ppid
    links. A child that predates its claimed parent is a recycled ppid and is
    excluded (it belongs to a process that no longer exists)."""
    by_parent: Dict[Optional[int], List[ProcessRecord]] = {}
    for r in records:
        by_parent.setdefault(r.ppid, []).append(r)
    by_pid = _by_pid(records)

    members: List[ProcessRecord] = [root]
    out: set = {root.pid}
    stack = [root.pid]
    while stack:
        cur_pid = stack.pop()
        parent = by_pid.get(cur_pid)
        for child in by_parent.get(cur_pid, []):
            if child.pid in out:
                continue
            if parent is not None and child.create_time < parent.create_time:
                continue
            out.add(child.pid)
            members.append(child)
            stack.append(child.pid)
    return tuple(members)


# ---------------------------------------------------------------- transcripts

def resolve_transcript_decision(
    exact: Optional[Tuple[str, float]],
    folder_entries: Optional[Sequence[Tuple[str, float]]],
    roots_sharing_folder: int,
) -> TranscriptEvidence:
    """Pure resolution decision from pre-gathered filesystem facts.

    ``exact`` is the (path, mtime) of an existing --resume transcript, if any.
    ``folder_entries`` is every *.jsonl in the session's mangled-cwd folder
    (None when cwd/folder is unreadable). ``roots_sharing_folder`` is how many
    LIVE census roots map to that folder.

    Fallback is only trusted when this root is the folder's sole live tenant:
    with two live sessions sharing a cwd, the newest transcript may belong to
    the other one, and "which session is idle" becomes unanswerable — so it
    is answered "protected".
    """
    if exact is not None:
        return TranscriptEvidence(TRANSCRIPT_EXACT, exact[0], exact[1])
    if not folder_entries:
        return TranscriptEvidence(TRANSCRIPT_MISSING, None, None)
    if roots_sharing_folder > 1:
        return TranscriptEvidence(TRANSCRIPT_AMBIGUOUS, None, None)
    path, mtime = max(folder_entries, key=lambda entry: entry[1])
    return TranscriptEvidence(TRANSCRIPT_FALLBACK, path, mtime)


# ---------------------------------------------------------------- pressure

def evaluate_pressure(
    events: Sequence[Mapping[str, object]],
    now: float,
    policy: FleetPolicy,
) -> PressureEvidence:
    """Validate the D7 trigger from plain event dicts
    ({event_id, timestamp, payload}), newest-wins.

    Fail-closed catalogue: missing, stale, future-dated, malformed, a NEWER
    pressure event whose reasons omit spawn_latency (the axis cleared or
    another axis took over — either way authorization is gone), and a
    same-timestamp tie whose members contradict each other.
    """
    if not events:
        return PressureEvidence(False, "missing")

    def _ts(ev: Mapping[str, object]) -> Optional[float]:
        raw = str(ev.get("timestamp") or "")
        try:
            from datetime import datetime
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    stamped = [(ev, _ts(ev)) for ev in events]
    if any(ts is None for _, ts in stamped):
        return PressureEvidence(False, "malformed")

    newest_ts = max(ts for _, ts in stamped)
    newest = [ev for ev, ts in stamped if ts == newest_ts]

    def _has_spawn(ev: Mapping[str, object]) -> Optional[bool]:
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            return None
        reasons = payload.get("reasons")
        if not isinstance(reasons, (list, tuple)):
            return None
        return "spawn_latency" in reasons

    flags = [_has_spawn(ev) for ev in newest]
    if any(flag is None for flag in flags):
        return PressureEvidence(False, "malformed")
    if len(set(flags)) > 1:
        # Two events at the identical newest timestamp disagreeing about the
        # axis: no ordering exists to break the tie, so nothing is proven.
        return PressureEvidence(False, "tied_contradictory")
    if not flags[0]:
        return PressureEvidence(False, "disarmed")

    ev = newest[0]
    age = now - newest_ts
    if age < -60.0:
        return PressureEvidence(False, "future")
    if age > policy.d7_max_age_seconds:
        return PressureEvidence(False, "stale")

    payload = ev.get("payload")
    sustained = payload.get("spawn_latency_sustained_ms") if isinstance(payload, dict) else None
    if not isinstance(sustained, (int, float)) or isinstance(sustained, bool) or sustained <= 0:
        # D7 always stamps the sustained floor it judged the axis on; its
        # absence means this is not a well-formed D7 spawn_latency event.
        return PressureEvidence(False, "malformed")

    return PressureEvidence(
        True, "ok",
        event_id=str(ev.get("event_id") or ""),
        event_timestamp=str(ev.get("timestamp") or ""),
        age_seconds=age,
        sustained_ms=float(sustained),
    )


# ---------------------------------------------------------------- assessment

def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def strike_key_for(root: ProcessRecord, transcript: TranscriptEvidence) -> Optional[str]:
    """Two-strike identity: root (pid, create_time) + transcript path + its
    integer mtime. ANY transcript write between passes mints a new key, so a
    session that woke up resets to zero strikes automatically."""
    if transcript.path is None or transcript.mtime is None:
        return None
    return (
        f"{root.identity}:{_short_hash(transcript.path)}:{int(transcript.mtime)}"
    )


def _normalized_user(user: Optional[str]) -> str:
    # Windows usernames arrive as MACHINE\name; compare the name alone.
    return str(user or "").rsplit("\\", 1)[-1].strip().lower()


def assess_tree(
    root: ProcessRecord,
    members: Tuple[ProcessRecord, ...],
    transcript: TranscriptEvidence,
    *,
    now: float,
    policy: FleetPolicy,
    protected_pids: frozenset,
    current_user: str,
) -> TreeAssessment:
    """Classify one whole tree. Default-deny: every uncertain observation adds
    a protection reason, and a single protected/incomplete member protects the
    entire tree — a tree is killed whole or not at all."""
    reasons: List[str] = []
    my_user = _normalized_user(current_user)

    for member in members:
        if not member.complete:
            reasons.append(REASON_INCOMPLETE_MEMBER)
        if is_infra(member):
            reasons.append(REASON_INFRA_MEMBER)
        if member.pid in protected_pids:
            reasons.append(REASON_ACTOR_MEMBER)
        if _normalized_user(member.username) != my_user:
            reasons.append(REASON_CROSS_USER_MEMBER)
        if member is not root and classify_process(member) == CLASS_DESKTOP:
            reasons.append(REASON_DESKTOP_MEMBER)

    idle_minutes: Optional[float] = None
    if transcript.resolution == TRANSCRIPT_MISSING:
        reasons.append(REASON_TRANSCRIPT_MISSING)
    elif transcript.resolution == TRANSCRIPT_AMBIGUOUS:
        reasons.append(REASON_TRANSCRIPT_AMBIGUOUS)
    elif transcript.mtime is not None:
        if transcript.mtime > now + 60.0:
            reasons.append(REASON_TRANSCRIPT_FUTURE)
        else:
            idle_minutes = max(0.0, (now - transcript.mtime) / 60.0)
            if idle_minutes < policy.idle_min_minutes:
                reasons.append(REASON_TRANSCRIPT_ACTIVE)

    if len(members) > policy.max_tree_processes:
        # Oversized trees are protected for manual review; the budget is a
        # whole-tree gate, never a license to trim the tree down to fit.
        reasons.append(REASON_OVERSIZE_PROCESSES)
    total_rss = sum(m.rss for m in members)
    if total_rss > policy.max_tree_rss_bytes:
        reasons.append(REASON_OVERSIZE_RSS)

    deduped = tuple(sorted(set(reasons)))
    protected = bool(deduped)
    key = strike_key_for(root, transcript) if not protected else None
    return TreeAssessment(
        root=root,
        members=members,
        total_rss=total_rss,
        transcript=transcript,
        idle_minutes=idle_minutes,
        protected=protected,
        reasons=deduped,
        eligible=not protected and idle_minutes is not None,
        strike_key=key,
    )


# ---------------------------------------------------------------- planning

def _plan_digest(
    policy: FleetPolicy,
    pressure: PressureEvidence,
    root_count: int,
    assessments: Sequence[TreeAssessment],
    strike_keys: Sequence[str],
) -> str:
    canonical = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "policy_digest": policy.digest(),
            "mode": policy.mode,
            "pressure": [pressure.valid, pressure.reason_code,
                         pressure.event_id, pressure.event_timestamp],
            "fleet_root_count": root_count,
            "trees": [
                [
                    a.root.identity,
                    sorted(m.identity for m in a.members),
                    a.transcript.resolution,
                    a.transcript.path,
                    int(a.transcript.mtime) if a.transcript.mtime is not None else None,
                    a.protected,
                    list(a.reasons),
                ]
                for a in sorted(assessments, key=lambda a: a.root.identity)
            ],
            "strike_keys": sorted(strike_keys),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_plan(
    *,
    assessments: Sequence[TreeAssessment],
    fleet_root_count: int,
    pressure: PressureEvidence,
    prior_strikes: Mapping[str, Mapping[str, float]],
    last_enforce_intent_at: Optional[float],
    policy: FleetPolicy,
    now: float,
    run_id: str,
    extra_reasons: Sequence[str] = (),
) -> FleetPlan:
    """The one deciding function. Returns the plan, including the FULL next
    strike state (the controller persists it verbatim — strikes are cleared,
    carried, or advanced here and nowhere else)."""
    trigger_reasons: List[str] = list(extra_reasons)
    fleet_armed = fleet_root_count > policy.fleet_min_roots
    if not fleet_armed:
        trigger_reasons.append(REASON_FLEET_BELOW_MIN)
    if not pressure.valid:
        trigger_reasons.append(f"pressure_{pressure.reason_code}")
    triggers_armed = fleet_armed and pressure.valid

    rejections: Dict[str, int] = {}
    for a in assessments:
        for code in a.reasons:
            rejections[code] = rejections.get(code, 0) + 1

    new_strikes: Dict[str, Dict[str, float]] = {}
    selected: Optional[TargetSummary] = None
    decision = DECISION_NO_ACTION

    if not triggers_armed:
        # Triggers down clears every strike: "idle across two passes" only
        # counts while both triggers held for the whole span.
        rejections[REASON_TRIGGERS_DISARMED] = rejections.get(REASON_TRIGGERS_DISARMED, 0) + 1
    else:
        candidates: List[Tuple[TreeAssessment, int]] = []
        for a in assessments:
            if not a.eligible or a.strike_key is None:
                continue
            prior = prior_strikes.get(a.strike_key)
            prior_count = 0
            if isinstance(prior, Mapping):
                recorded_at = prior.get("recorded_at")
                count = prior.get("count")
                if (
                    isinstance(recorded_at, (int, float))
                    and isinstance(count, (int, float))
                    and 0 <= now - float(recorded_at) <= policy.strike_max_age_seconds
                ):
                    prior_count = int(count)
            strikes = prior_count + 1
            new_strikes[a.strike_key] = {"recorded_at": now, "count": float(strikes)}
            if strikes >= policy.strikes_required:
                candidates.append((a, strikes))
            else:
                rejections[REASON_FIRST_STRIKE] = rejections.get(REASON_FIRST_STRIKE, 0) + 1

        in_cooldown = (
            last_enforce_intent_at is not None
            and 0 <= now - last_enforce_intent_at < policy.cooldown_seconds
        )
        if candidates and in_cooldown:
            rejections[REASON_COOLDOWN_ACTIVE] = rejections.get(REASON_COOLDOWN_ACTIVE, 0) + len(candidates)
            trigger_reasons.append(REASON_COOLDOWN_ACTIVE)
        elif candidates:
            # Deterministic ordering: most-idle first, then oldest root, then
            # larger tree RSS, then stable identity. RSS is a tie-breaker,
            # never causal attribution.
            candidates.sort(
                key=lambda pair: (
                    -(pair[0].idle_minutes or 0.0),
                    pair[0].root.create_time,
                    -pair[0].total_rss,
                    pair[0].root.identity,
                )
            )
            best, strikes = candidates[0]
            selected = TargetSummary(
                root_identity=best.root.identity,
                root_pid=best.root.pid,
                root_create_time=best.root.create_time,
                member_identities=tuple(sorted(m.identity for m in best.members)),
                member_count=len(best.members),
                total_rss=best.total_rss,
                transcript_path=str(best.transcript.path),
                transcript_mtime=float(best.transcript.mtime or 0.0),
                idle_minutes=float(best.idle_minutes or 0.0),
                strike_key=str(best.strike_key),
                strikes=strikes,
                action=ACTION_HARD_TERMINATE,
            )
            decision = (
                DECISION_ENFORCE_PROJECTED
                if policy.mode == MODE_ENFORCE
                else DECISION_SHADOW_PROJECTED
            )

    digest = _plan_digest(
        policy, pressure, fleet_root_count, assessments, list(new_strikes)
    )
    return FleetPlan(
        schema_version=SCHEMA_VERSION,
        policy_version=policy.policy_version,
        policy_digest=policy.digest(),
        run_id=run_id,
        mode=policy.mode,
        decision=decision,
        triggers_armed=triggers_armed,
        trigger_reasons=tuple(trigger_reasons),
        fleet_root_count=fleet_root_count,
        pressure=pressure,
        selected=selected,
        rejections=tuple(sorted(rejections.items())),
        digest=digest,
        new_strikes=new_strikes,
    )
