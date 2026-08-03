from gateway.fleet_safety.deadloop_guard import GuardThresholds, SessionFamilyGuard, SessionObservation


def obs(sid, action, progress, *, parent="parent", started=0.0, current_tool=None):
    return SessionObservation(
        session_id=sid,
        started_at=started,
        family_id="parent",
        parent_session_id=parent,
        action_fingerprint=action,
        progress_evidence=progress,
        state_hash=progress,
        current_tool=current_tool,
    )


def test_parent_waiting_does_not_reset_child_family_clock():
    guard = SessionFamilyGuard(GuardThresholds(family_idle_seconds=900, family_cycle_repeats=3))
    child = obs("child", "A", "stable")
    assert guard.observe([child], 0.0) is None
    assert guard.observe([child], 300.0) is None
    assert guard.observe([child], 600.0) is None
    result = guard.observe([child], 900.0)
    assert result is not None
    assert result[0] == "parent"
    assert "period=1" in result[1]


def test_parentless_solo_and_active_tool_are_not_family_trips():
    guard = SessionFamilyGuard(GuardThresholds(family_idle_seconds=1, family_cycle_repeats=2))
    solo = SessionObservation(session_id="solo", started_at=0, action_fingerprint="A")
    active = obs("child", "A", "stable", current_tool="build")
    for now in (0.0, 1.0, 2.0, 3.0):
        assert guard.observe([solo], now) is None
        assert guard.observe([active], now) is None


def test_period_two_ab_loop_trips_without_progress():
    guard = SessionFamilyGuard(GuardThresholds(family_idle_seconds=10, family_cycle_repeats=3))
    assert guard.observe([obs("child", "A", "stable")], 0.0) is None
    assert guard.observe([obs("child", "B", "stable")], 2.0) is None
    assert guard.observe([obs("child", "A", "stable")], 4.0) is None
    assert guard.observe([obs("child", "B", "stable")], 6.0) is None
    assert guard.observe([obs("child", "A", "stable")], 8.0) is None
    result = guard.observe([obs("child", "B", "stable")], 10.0)
    assert result is not None
    assert "period=2" in result[1]


def test_period_three_abc_loop_trips_without_progress():
    guard = SessionFamilyGuard(GuardThresholds(family_idle_seconds=10, family_cycle_repeats=2))
    for now, action in enumerate(("A", "B", "C", "A", "B", "C")):
        result = guard.observe([obs("child", action, "stable")], float(now * 2))
    assert result is not None
    assert "period=3" in result[1]


def test_verified_progress_resets_cycle_and_does_not_trip():
    guard = SessionFamilyGuard(GuardThresholds(family_idle_seconds=10, family_cycle_repeats=2))
    assert guard.observe([obs("child", "A", "p0")], 0.0) is None
    assert guard.observe([obs("child", "B", "p0")], 2.0) is None
    assert guard.observe([obs("child", "A", "p1")], 4.0) is None
    assert guard.observe([obs("child", "B", "p1")], 6.0) is None
    assert guard.observe([obs("child", "A", "p1")], 8.0) is None
    assert guard.observe([obs("child", "B", "p1")], 10.0) is None
