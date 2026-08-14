from agent.routing_snapshot import RoutingSnapshotAdapter


class FakeAgent:
    provider = "openrouter-free"
    model = "openrouter/free"
    base_url = "https://user:secret@openrouter.ai/api/v1?token=secret"
    api_key = "must-never-leak"
    _fallback_activated = True
    _rate_limited_until = 1_060.0


def test_snapshot_is_redacted_and_does_not_mutate_agent():
    agent = FakeAgent()
    before = dict(agent.__dict__)

    snapshot = RoutingSnapshotAdapter.from_agent(
        agent,
        captured_at_monotonic=1_020.0,
    )

    assert snapshot.provider == "openrouter-free"
    assert snapshot.model == "openrouter/free"
    assert snapshot.base_url_host == "openrouter.ai"
    assert snapshot.fallback_active
    assert snapshot.cooldown_remaining_s == 40.0
    assert agent.__dict__ == before
    assert "secret" not in repr(snapshot)
    assert "api_key" not in repr(snapshot)


def test_absent_runtime_state_is_explicitly_inactive():
    agent = object()
    snapshot = RoutingSnapshotAdapter.from_agent(
        agent,
        captured_at_monotonic=1_020.0,
    )
    assert snapshot.provider == ""
    assert snapshot.model == ""
    assert snapshot.base_url_host == ""
    assert not snapshot.fallback_active
    assert snapshot.cooldown_remaining_s == 0.0
