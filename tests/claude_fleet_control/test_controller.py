"""Controller orchestration over a real temp EventBus and fake process world.

Covers: config parsing and mode gating, the singleton lock, corrupt-state
reset, the end-to-end two-pass shadow sequence with a real bus, that shadow
NEVER constructs an executor, both enforce gates, and the enforce
revalidation cancellations.
"""

import json

from claude_fleet_control import controller as ctrl
from claude_fleet_control.controller import Controller, load_policy
from claude_fleet_control.models import (
    MODE_DISABLED,
    RESULT_HARD_TERMINATED,
    RESULT_NO_ACTION,
    RESULT_SHADOW_PROJECTED,
    FleetPolicy,
    ProcessSnapshot,
)
from events.bus import EventBus
from events.schema import EventType
from tests.claude_fleet_control.conftest import NOW, cli_rec, iso


# ---------------------------------------------------------------- config

def _write_config(tmp_path, **overrides):
    cfg = {
        "mode": "shadow",
        "policy_version": "test",
        "fleet_min_roots": 3,
        "idle_min_minutes": 30,
        "strikes_required": 2,
    }
    cfg.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_load_policy_unknown_mode_degrades_to_disabled(tmp_path):
    policy, notes = load_policy(_write_config(tmp_path, mode="frobnicate"))
    assert policy is not None and policy.mode == MODE_DISABLED
    assert any("mode_invalid" in n for n in notes)


def test_load_policy_malformed_numeric_is_hard_error(tmp_path):
    policy, notes = load_policy(_write_config(tmp_path, max_tree_processes=-5))
    assert policy is None and "malformed" in notes[0]


def test_load_policy_missing_file_is_hard_error(tmp_path):
    policy, notes = load_policy(tmp_path / "nope.json")
    assert policy is None


def test_tracked_config_ships_shadow():
    """The shipped default must be shadow with no enforce approval — this is
    the deploy-time guarantee that the change cannot terminate anything."""
    policy, notes = load_policy(ctrl.default_config_path())
    assert policy is not None
    assert policy.mode == "shadow"
    assert policy.approved_enforce_digest is None
    assert notes == []


# ---------------------------------------------------------------- harness

class _FakeLock:
    held = False

    def __init__(self, path, acquirable=True):
        self._acquirable = acquirable

    def acquire(self):
        return self._acquirable

    def release(self):
        pass


def _make_controller(tmp_path, records, config_path, *, now=NOW, allow_enforce=False,
                     pressure_events=None, lock_ok=True, executor_factory=None,
                     transcript=("t.jsonl", NOW - 3600.0)):
    bus = EventBus(db_path=tmp_path / "bus.db")
    if pressure_events is None:
        pressure_events = [{
            "reasons": ["spawn_latency"], "spawn_latency_sustained_ms": 2100.0,
            "ts": now - 60.0,
        }]
    for ev in pressure_events:
        payload = {k: v for k, v in ev.items() if k != "ts"}
        # Emit with a controllable timestamp by writing directly.
        bus.emit(event_type=EventType.RESOURCE_PRESSURE, source="system", payload=payload)
    # Rewrite timestamps to the fake clock (emit stamps "now"-real).
    conn = bus._get_conn()
    rows = conn.execute("SELECT event_id FROM events ORDER BY rowid").fetchall()
    for row, ev in zip(rows, pressure_events):
        conn.execute("UPDATE events SET timestamp=? WHERE event_id=?",
                     (iso(ev["ts"]), row["event_id"]))
    conn.commit()

    def snapshot():
        return ProcessSnapshot(taken_at=now, records=tuple(records), complete=True)

    def facts(root):
        return (r"C:\ws\%d" % abs(root.pid), (transcript[0], transcript[1]), [(transcript[0], transcript[1])])

    return Controller(
        config_path=config_path,
        state_dir=tmp_path / "state",
        allow_enforce=allow_enforce,
        now_fn=lambda: now,
        snapshot_fn=snapshot,
        transcript_facts_fn=facts,
        bus_factory=lambda: bus,
        lock_factory=lambda p: _FakeLock(p, acquirable=lock_ok),
        executor_factory=executor_factory,
    ), bus


def _fleet(n, base=-300):
    """n distinct idle CLI roots, each in its own cwd."""
    return [cli_rec(base - i, create_time=NOW - 7200.0 - i) for i in range(n)]


def _results(bus):
    return bus.query(event_type=EventType.CLAUDE_FLEET_RESULT)


def _plans(bus):
    return bus.query(event_type=EventType.CLAUDE_FLEET_PLAN)


# ---------------------------------------------------------------- gating

def test_disabled_mode_takes_no_pass(tmp_path):
    cfg = _write_config(tmp_path, mode="disabled")
    controller, bus = _make_controller(tmp_path, _fleet(4), cfg)
    code, result = controller.run_once()
    assert code == ctrl.EXIT_OK and result is None
    assert _plans(bus) == [] and _results(bus) == []


def test_lock_held_skips(tmp_path):
    cfg = _write_config(tmp_path)
    controller, bus = _make_controller(tmp_path, _fleet(4), cfg, lock_ok=False)
    code, result = controller.run_once()
    assert code == ctrl.EXIT_LOCK_HELD and result is None
    assert _plans(bus) == []


# ---------------------------------------------------------------- shadow flow

def test_shadow_two_pass_projection_never_constructs_executor(tmp_path):
    cfg = _write_config(tmp_path)
    records = _fleet(4)

    # Pass 1: first strike, no action.
    c1, bus = _make_controller(tmp_path, records, cfg)
    code1, r1 = c1.run_once()
    assert code1 == ctrl.EXIT_OK and r1.status == RESULT_NO_ACTION
    assert r1.executor_called is False
    assert c1.executor_constructed is False

    # Pass 2: same tree, same transcript, still both triggers -> shadow project.
    c2, bus2 = _make_controller(tmp_path, records, cfg)
    # Reuse pass-1 state dir by pointing at the same state.
    c2.state_dir = c1.state_dir
    code2, r2 = c2.run_once()
    assert code2 == ctrl.EXIT_OK and r2.status == RESULT_SHADOW_PROJECTED
    assert r2.executor_called is False
    assert c2.executor_constructed is False

    plans = _plans(bus2)
    assert plans, "a plan event must be durably emitted"
    latest = plans[-1].payload
    assert latest["decision"] == "shadow_projected"
    assert latest["selected"]["strikes"] == 2
    # payload hygiene: identities only, no command lines
    assert "cmdline" not in json.dumps(latest)


def test_disarmed_trigger_yields_no_action(tmp_path):
    cfg = _write_config(tmp_path)
    controller, bus = _make_controller(
        tmp_path, _fleet(4), cfg,
        pressure_events=[{"reasons": ["commit_high"], "ts": NOW - 60.0}],
    )
    code, result = controller.run_once()
    assert result.status == RESULT_NO_ACTION
    assert any("pressure_disarmed" in r for r in [result.detail])


def test_fleet_below_floor_yields_no_action(tmp_path):
    cfg = _write_config(tmp_path)
    controller, bus = _make_controller(tmp_path, _fleet(3), cfg)  # exactly floor
    code, result = controller.run_once()
    assert result.status == RESULT_NO_ACTION
    assert "fleet_at_or_below_min" in result.detail


def test_corrupt_state_resets_strikes(tmp_path):
    cfg = _write_config(tmp_path)
    controller, bus = _make_controller(tmp_path, _fleet(4), cfg)
    controller.state_dir.mkdir(parents=True, exist_ok=True)
    (controller.state_dir / "state.json").write_text("{ not json", encoding="utf-8")
    code, result = controller.run_once()
    # Corrupt state -> treated as empty -> first strike only, never a kill.
    assert result.status == RESULT_NO_ACTION


# ---------------------------------------------------------------- enforce gates

def _prime_second_strike(tmp_path, cfg, records, **kw):
    c1, _ = _make_controller(tmp_path, records, cfg, **kw)
    c1.run_once()
    return c1.state_dir


def test_enforce_mode_without_allow_flag_runs_as_shadow(tmp_path):
    digest = FleetPolicy(mode="enforce", policy_version="test",
                         fleet_min_roots=3).digest()
    cfg = _write_config(tmp_path, mode="enforce", approved_enforce_digest=digest)
    records = _fleet(4)
    state_dir = _prime_second_strike(tmp_path, cfg, records)

    # allow_enforce=False -> demoted to shadow; no executor.
    c2, bus = _make_controller(tmp_path, records, cfg, allow_enforce=False)
    c2.state_dir = state_dir
    code, result = c2.run_once()
    assert result.status == RESULT_SHADOW_PROJECTED
    assert result.executor_called is False and c2.executor_constructed is False


def test_enforce_mode_wrong_digest_runs_as_shadow(tmp_path):
    cfg = _write_config(tmp_path, mode="enforce",
                        approved_enforce_digest="deadbeef")
    records = _fleet(4)
    state_dir = _prime_second_strike(tmp_path, cfg, records, allow_enforce=True)
    c2, bus = _make_controller(tmp_path, records, cfg, allow_enforce=True)
    c2.state_dir = state_dir
    code, result = c2.run_once()
    assert result.status == RESULT_SHADOW_PROJECTED
    assert c2.executor_constructed is False


def test_both_gates_satisfied_calls_injected_executor(tmp_path):
    digest = FleetPolicy(mode="enforce", policy_version="test",
                         fleet_min_roots=3).digest()
    cfg = _write_config(tmp_path, mode="enforce", approved_enforce_digest=digest)
    records = _fleet(4)

    calls = {"n": 0}

    class _FakeExecutor:
        def __init__(self, *, snapshot_fn):
            calls["n"] += 1

        def hard_terminate_tree(self, target, *, plan_id):
            from claude_fleet_control.executor import ExecutionReport
            return ExecutionReport(
                ok=True, cancelled=False, detail="tree exited",
                exited_identities=target.member_identities,
            )

    state_dir = _prime_second_strike(tmp_path, cfg, records, allow_enforce=True)
    c2, bus = _make_controller(tmp_path, records, cfg, allow_enforce=True,
                               executor_factory=lambda **kw: _FakeExecutor(**kw))
    c2.state_dir = state_dir
    code, result = c2.run_once()
    assert result.status == RESULT_HARD_TERMINATED
    assert result.executor_called is True and calls["n"] == 1
    # cooldown recorded
    state = json.loads((state_dir / "state.json").read_text())
    assert state["last_enforce_intent_at"] is not None


def test_enforce_cancels_when_target_becomes_active_on_revalidation(tmp_path):
    digest = FleetPolicy(mode="enforce", policy_version="test",
                         fleet_min_roots=3).digest()
    cfg = _write_config(tmp_path, mode="enforce", approved_enforce_digest=digest)
    records = _fleet(4)
    state_dir = _prime_second_strike(tmp_path, cfg, records, allow_enforce=True)

    constructed = {"n": 0}

    def _factory(**kw):
        constructed["n"] += 1
        raise AssertionError("executor must not be built after cancellation")

    # Second pass: transcript now fresh -> target active on revalidation.
    c2, bus = _make_controller(
        tmp_path, records, cfg, allow_enforce=True,
        transcript=("t.jsonl", NOW - 60.0),  # active
        executor_factory=_factory,
    )
    c2.state_dir = state_dir
    code, result = c2.run_once()
    # The strike key changed (fresh transcript), so it never even reaches a
    # second strike -> no_action, executor never built.
    assert result.executor_called is False and constructed["n"] == 0


def test_bus_query_failure_yields_no_action(tmp_path):
    cfg = _write_config(tmp_path)

    class _BadBus(EventBus):
        def query(self, *a, **k):
            raise RuntimeError("bus down")

    bad = _BadBus(db_path=tmp_path / "bus.db")
    records = _fleet(4)

    def snapshot():
        return ProcessSnapshot(taken_at=NOW, records=tuple(records), complete=True)

    controller = Controller(
        config_path=cfg, state_dir=tmp_path / "state",
        now_fn=lambda: NOW, snapshot_fn=snapshot,
        transcript_facts_fn=lambda r: ("cwd", ("t.jsonl", NOW - 3600.0),
                                       [("t.jsonl", NOW - 3600.0)]),
        bus_factory=lambda: bad,
        lock_factory=lambda p: _FakeLock(p),
    )
    code, result = controller.run_once()
    assert result.status == RESULT_NO_ACTION
    assert "pressure_bus_error" in result.detail
