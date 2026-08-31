"""Pure-planner tests: classification, ancestry, transcripts, the trigger
truth table, and the plan builder's ordering/strike/budget/cooldown rules."""

import dataclasses

from claude_fleet_control import planner
from claude_fleet_control.models import (
    DECISION_ENFORCE_PROJECTED,
    DECISION_NO_ACTION,
    DECISION_SHADOW_PROJECTED,
    REASON_COOLDOWN_ACTIVE,
    REASON_FIRST_STRIKE,
    REASON_FLEET_BELOW_MIN,
    FleetPolicy,
    TranscriptEvidence,
)
from tests.claude_fleet_control.conftest import NOW, cli_rec, iso, rec

POLICY = FleetPolicy(mode="shadow", policy_version="test", fleet_min_roots=3)


# ---------------------------------------------------------------- classify

def test_cli_markers_classify_as_cli():
    assert planner.classify_process(cli_rec(-1)) == planner.CLASS_CLI
    r = rec(-2, name="claude", cmdline=("claude", "claude-code", "run"))
    assert planner.classify_process(r) == planner.CLASS_CLI


def test_desktop_exe_and_electron_helpers_are_excluded():
    desktop = rec(-3, name="claude.exe",
                  exe=r"C:\Users\d\AppData\Local\AnthropicClaude\app\Claude.exe",
                  cmdline=("claude.exe", "claude-code"))
    helper = rec(-4, name="claude.exe", cmdline=("claude.exe", "--type=renderer"))
    assert planner.classify_process(desktop) == planner.CLASS_DESKTOP
    assert planner.classify_process(helper) == planner.CLASS_DESKTOP


def test_unreadable_or_foreign_processes_are_other():
    assert planner.classify_process(rec(-5, name="claude.exe", cmdline=())) == planner.CLASS_OTHER
    assert planner.classify_process(rec(-6, name="python.exe")) == planner.CLASS_OTHER


def test_resume_uuid_is_structural_on_argv():
    u = "12345678-abcd-4000-8000-1234567890ab"
    assert planner.resume_session_uuid(("claude", "--resume", u)) == u
    assert planner.resume_session_uuid((f"--resume={u}",)) == u
    assert planner.resume_session_uuid(("claude", "--resume", "not-a-uuid")) is None
    # A quoted script body CONTAINING the flag must not match (the joined-scan
    # trap from the 2026-08-16 sweep incident).
    assert planner.resume_session_uuid((f"powershell -c 'x --resume {u}'",)) is None


def test_mangle_cwd_matches_claude_code_convention():
    assert planner.mangle_cwd(r"C:\Users\diego\.hermes") == "C--Users-diego--hermes"


# ---------------------------------------------------------------- ancestry

def test_recycled_ppid_stops_the_ancestor_climb():
    child = rec(-10, ppid=-11, create_time=NOW - 100)
    imposter = rec(-11, create_time=NOW - 50)  # "parent" born after the child
    by_pid = {r.pid: r for r in (child, imposter)}
    chain = planner.ancestor_chain(-10, by_pid)
    assert [r.pid for r in chain] == [-10]


def test_collect_tree_excludes_children_predating_their_parent():
    root = cli_rec(-20, create_time=NOW - 500)
    genuine = rec(-21, ppid=-20, create_time=NOW - 400)
    recycled = rec(-22, ppid=-20, create_time=NOW - 9000)  # older than root
    members = planner.collect_tree(root, (root, genuine, recycled))
    assert {m.pid for m in members} == {-20, -21}


def test_find_cli_roots_counts_only_topmost_cli():
    outer = cli_rec(-30, create_time=NOW - 500)
    inner = cli_rec(-31, ppid=-30, create_time=NOW - 400)
    roots = planner.find_cli_roots((outer, inner))
    assert [r.pid for r in roots] == [-30]


def test_enrichment_pids_covers_claude_procs_and_their_trees_only():
    """The two-phase census enriches only Claude-named processes and their
    create-time-valid descendants; the un-related majority stays cheap."""
    root = cli_rec(-30, create_time=NOW - 500)
    child = rec(-31, ppid=-30, name="node.exe", create_time=NOW - 400)      # MCP child
    grandchild = rec(-32, ppid=-31, name="bun.exe", create_time=NOW - 300)
    unrelated = rec(-99, name="chrome.exe", create_time=NOW - 400)
    # A claude-named process seeds by NAME even before classification.
    desktop = rec(-40, name="claude.exe",
                  exe=r"C:\a\app\Claude.exe", create_time=NOW - 900)
    recycled = rec(-33, ppid=-30, name="python.exe", create_time=NOW - 9000)  # predates root
    records = (root, child, grandchild, unrelated, desktop, recycled)
    pids = planner.enrichment_pids(records)
    assert pids == frozenset({-30, -31, -32, -40})
    assert -99 not in pids           # unrelated stays cheap
    assert -33 not in pids           # recycled ppid excluded from the tree


# ---------------------------------------------------------------- transcripts

def test_transcript_exact_wins():
    ev = planner.resolve_transcript_decision(("t.jsonl", 5.0), [("x.jsonl", 9.0)], 1)
    assert (ev.resolution, ev.path, ev.mtime) == ("exact", "t.jsonl", 5.0)


def test_transcript_fallback_needs_sole_tenancy():
    entries = [("a.jsonl", 1.0), ("b.jsonl", 2.0)]
    assert planner.resolve_transcript_decision(None, entries, 1).path == "b.jsonl"
    assert planner.resolve_transcript_decision(None, entries, 2).resolution == "ambiguous"
    assert planner.resolve_transcript_decision(None, None, 1).resolution == "missing"
    assert planner.resolve_transcript_decision(None, [], 1).resolution == "missing"


# ---------------------------------------------------------------- pressure

def _pressure_event(ts, reasons, sustained=2100.0, event_id="e1"):
    payload = {"reasons": reasons}
    if sustained is not None:
        payload["spawn_latency_sustained_ms"] = sustained
    return {"event_id": event_id, "timestamp": iso(ts), "payload": payload}


def test_pressure_truth_table():
    fresh = _pressure_event(NOW - 60, ["spawn_latency"])
    assert planner.evaluate_pressure([fresh], NOW, POLICY).valid

    assert planner.evaluate_pressure([], NOW, POLICY).reason_code == "missing"
    stale = _pressure_event(NOW - 400, ["spawn_latency"])
    assert planner.evaluate_pressure([stale], NOW, POLICY).reason_code == "stale"
    future = _pressure_event(NOW + 120, ["spawn_latency"])
    assert planner.evaluate_pressure([future], NOW, POLICY).reason_code == "future"
    no_sustained = _pressure_event(NOW - 60, ["spawn_latency"], sustained=None)
    assert planner.evaluate_pressure([no_sustained], NOW, POLICY).reason_code == "malformed"
    bad = {"event_id": "e", "timestamp": iso(NOW - 60), "payload": {"reasons": "nope"}}
    assert planner.evaluate_pressure([bad], NOW, POLICY).reason_code == "malformed"
    unparseable = {"event_id": "e", "timestamp": "not-a-time", "payload": {"reasons": []}}
    assert planner.evaluate_pressure([unparseable], NOW, POLICY).reason_code == "malformed"


def test_newer_event_without_spawn_latency_disarms():
    """A commit/disk-only pressure event AFTER the spawn event means the axis
    is no longer the newest word — authorization is gone."""
    armed = _pressure_event(NOW - 120, ["spawn_latency"], event_id="old")
    cleared = _pressure_event(NOW - 30, ["commit_high"], sustained=None, event_id="new")
    ev = planner.evaluate_pressure([armed, cleared], NOW, POLICY)
    assert (ev.valid, ev.reason_code) == (False, "disarmed")


def test_tied_contradictory_newest_fails_closed():
    a = _pressure_event(NOW - 30, ["spawn_latency"], event_id="a")
    b = _pressure_event(NOW - 30, ["commit_high"], sustained=None, event_id="b")
    ev = planner.evaluate_pressure([a, b], NOW, POLICY)
    assert (ev.valid, ev.reason_code) == (False, "tied_contradictory")


# ---------------------------------------------------------------- assessment

def _idle_transcript(mtime=NOW - 3600.0, path="t.jsonl"):
    return TranscriptEvidence("exact", path, mtime)


def _assess(root, members, transcript=None, protected_pids=frozenset(), **overrides):
    policy = dataclasses.replace(POLICY, **overrides) if overrides else POLICY
    return planner.assess_tree(
        root, members, transcript or _idle_transcript(),
        now=NOW, policy=policy, protected_pids=protected_pids,
        current_user="diego",
    )


def test_clean_idle_tree_is_eligible():
    root = cli_rec(-40)
    a = _assess(root, (root, rec(-41, ppid=-40)))
    assert a.eligible and not a.protected
    assert a.strike_key is not None
    assert a.idle_minutes == 60.0


def test_every_protection_reason_protects_the_whole_tree():
    root = cli_rec(-50)
    cases = {
        "incomplete_member": (root, rec(-51, ppid=-50, complete=False)),
        "infra_member": (root, rec(-52, ppid=-50, name="wslhost.exe")),
        "cross_user_member": (root, rec(-53, ppid=-50, username="NT AUTHORITY\\SYSTEM")),
        "desktop_member": (root, rec(-54, ppid=-50, name="claude.exe",
                                     cmdline=("claude.exe", "--type=gpu"))),
    }
    for reason, members in cases.items():
        a = _assess(root, members)
        assert a.protected and reason in a.reasons, reason
        assert a.strike_key is None

    actor = _assess(root, (root, rec(-55, ppid=-50)), protected_pids=frozenset({-55}))
    assert actor.protected and "actor_member" in actor.reasons


def test_transcript_states_gate_eligibility():
    root = cli_rec(-60)
    members = (root,)
    active = _assess(root, members, TranscriptEvidence("exact", "t", NOW - 60))
    assert not active.eligible and "transcript_active" in active.reasons
    future = _assess(root, members, TranscriptEvidence("exact", "t", NOW + 600))
    assert future.protected and "transcript_future_mtime" in future.reasons
    missing = _assess(root, members, TranscriptEvidence("missing", None, None))
    assert missing.protected and "transcript_missing" in missing.reasons
    ambiguous = _assess(root, members, TranscriptEvidence("ambiguous", None, None))
    assert ambiguous.protected and "transcript_ambiguous" in ambiguous.reasons
    # Exactly the idle threshold is idle (>= 30 min).
    boundary = _assess(root, members, TranscriptEvidence("exact", "t", NOW - 1800))
    assert boundary.eligible


def test_budgets_protect_never_trim():
    root = cli_rec(-70)
    big = tuple([root] + [rec(-71 - i, ppid=-70) for i in range(24)])  # 25 total
    a = _assess(root, big)
    assert a.protected and "oversize_processes" in a.reasons
    fat = (root, rec(-99, ppid=-70, rss=3 * 1024 ** 3))
    b = _assess(root, fat)
    assert b.protected and "oversize_rss" in b.reasons


def test_transcript_write_mints_a_new_strike_key():
    root = cli_rec(-80)
    k1 = planner.strike_key_for(root, _idle_transcript(mtime=NOW - 3600))
    k2 = planner.strike_key_for(root, _idle_transcript(mtime=NOW - 3599))
    assert k1 != k2


# ---------------------------------------------------------------- planning

def _armed_pressure():
    return planner.evaluate_pressure(
        [_pressure_event(NOW - 60, ["spawn_latency"])], NOW, POLICY
    )


def _eligible_assessment(pid, idle_min=60.0, create_time=NOW - 7200.0, rss=10 * 1024 * 1024):
    root = cli_rec(pid, create_time=create_time, rss=rss)
    return planner.assess_tree(
        root, (root,),
        TranscriptEvidence("exact", f"t{pid}.jsonl", NOW - idle_min * 60.0),
        now=NOW, policy=POLICY, protected_pids=frozenset(), current_user="diego",
    )


def _plan(assessments, *, root_count=4, pressure=None, prior=None, last_intent=None,
          policy=POLICY, extra=()):
    return planner.build_plan(
        assessments=assessments,
        fleet_root_count=root_count,
        pressure=pressure or _armed_pressure(),
        prior_strikes=prior or {},
        last_enforce_intent_at=last_intent,
        policy=policy,
        now=NOW,
        run_id="run",
        extra_reasons=extra,
    )


def test_trigger_truth_table():
    a = _eligible_assessment(-100)
    # count above floor + valid pressure -> armed
    assert _plan([a], root_count=4).triggers_armed
    # count AT the floor -> disarmed (strictly-above contract)
    p = _plan([a], root_count=3)
    assert not p.triggers_armed and REASON_FLEET_BELOW_MIN in p.trigger_reasons
    # fleet alone (pressure invalid) -> disarmed
    dead = planner.evaluate_pressure([], NOW, POLICY)
    q = _plan([a], root_count=4, pressure=dead)
    assert not q.triggers_armed and "pressure_missing" in q.trigger_reasons
    assert q.new_strikes == {}  # disarm clears strikes


def test_first_strike_records_but_never_selects():
    a = _eligible_assessment(-101)
    p = _plan([a])
    assert p.decision == DECISION_NO_ACTION and p.selected is None
    assert dict(p.rejections).get(REASON_FIRST_STRIKE) == 1
    assert p.new_strikes[a.strike_key]["count"] == 1.0


def test_second_strike_selects_in_shadow():
    a = _eligible_assessment(-102)
    prior = {a.strike_key: {"recorded_at": NOW - 300.0, "count": 1.0}}
    p = _plan([a], prior=prior)
    assert p.decision == DECISION_SHADOW_PROJECTED
    assert p.selected is not None and p.selected.strikes == 2
    assert p.selected.root_identity == a.root.identity


def test_stale_strike_restarts_at_one():
    a = _eligible_assessment(-103)
    prior = {a.strike_key: {"recorded_at": NOW - 2000.0, "count": 1.0}}
    p = _plan([a], prior=prior)
    assert p.decision == DECISION_NO_ACTION
    assert p.new_strikes[a.strike_key]["count"] == 1.0


def test_cooldown_blocks_selection():
    a = _eligible_assessment(-104)
    prior = {a.strike_key: {"recorded_at": NOW - 300.0, "count": 1.0}}
    p = _plan([a], prior=prior, last_intent=NOW - 600.0)
    assert p.decision == DECISION_NO_ACTION
    assert REASON_COOLDOWN_ACTIVE in p.trigger_reasons
    expired = _plan([a], prior=prior, last_intent=NOW - 1801.0)
    assert expired.decision == DECISION_SHADOW_PROJECTED


def test_ordering_most_idle_then_oldest_then_rss():
    idle = _eligible_assessment(-110, idle_min=200.0)
    less = _eligible_assessment(-111, idle_min=100.0)
    prior = {
        idle.strike_key: {"recorded_at": NOW - 300.0, "count": 1.0},
        less.strike_key: {"recorded_at": NOW - 300.0, "count": 1.0},
    }
    p = _plan([less, idle], prior=prior)
    assert p.selected.root_pid == -110
    # idle tie -> oldest root wins
    old = _eligible_assessment(-112, idle_min=100.0, create_time=NOW - 90000.0)
    prior2 = {
        less.strike_key: {"recorded_at": NOW - 300.0, "count": 1.0},
        old.strike_key: {"recorded_at": NOW - 300.0, "count": 1.0},
    }
    p2 = _plan([less, old], prior=prior2)
    assert p2.selected.root_pid == -112
    # only one tree per pass, ever
    assert p2.selected is not None and p2.decision == DECISION_SHADOW_PROJECTED


def test_enforce_mode_projects_enforce():
    a = _eligible_assessment(-120)
    prior = {a.strike_key: {"recorded_at": NOW - 300.0, "count": 1.0}}
    enforce_policy = dataclasses.replace(POLICY, mode="enforce")
    p = _plan([a], prior=prior, policy=enforce_policy)
    assert p.decision == DECISION_ENFORCE_PROJECTED


def test_plan_digest_is_deterministic_and_input_sensitive():
    a = _eligible_assessment(-130)
    assert _plan([a]).digest == _plan([a]).digest
    assert _plan([a]).digest != _plan([_eligible_assessment(-131)]).digest


def test_corrupt_state_reasons_flow_into_the_plan():
    p = _plan([_eligible_assessment(-140)], extra=["state_corrupt"])
    assert "state_corrupt" in p.trigger_reasons
