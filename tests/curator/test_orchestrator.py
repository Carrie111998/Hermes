"""Unit tests for curator.orchestrator."""
from __future__ import annotations



from curator.orchestrator import (
    AGENTS,
    run_backfill,
)


class _FakeBus:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _stub_search_fn(_query, _params):
    return {"results": []}


def test_orchestrator_dispatches_over_all_ten_agents(tmp_path):
    """All 10 agents are visited during backfill."""
    visited = []

    def fake_render(agent, *_args, **_kwargs):
        visited.append(agent)
        return f"# MEMORY — {agent}\n\n## Operating Stats\nrendered.\n"

    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("")  # empty
    # Build minimal profiles tree so writes succeed.
    for agent in AGENTS:
        (tmp_path / "profiles" / agent / "memories").mkdir(parents=True, exist_ok=True)

    result = run_backfill(
        window_days=30,
        dry_run=True,
        emit_event=False,
        audit_path=audit_path,
        search_fn=_stub_search_fn,
        bus=None,
        hermes_root=tmp_path,
        render_fn=fake_render,
    )
    assert sorted(visited) == sorted(AGENTS)
    assert result.mode == "backfill"


def test_orchestrator_writes_to_correct_path(tmp_path):
    """Each output lands at profiles/<agent>/memories/MEMORY.md."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("")
    for agent in AGENTS:
        (tmp_path / "profiles" / agent / "memories").mkdir(parents=True, exist_ok=True)

    def render_fn(agent, *_a, **_k):
        return f"# MEMORY — {agent}\n\nrendered\n"

    run_backfill(
        window_days=30, dry_run=False, emit_event=False,
        audit_path=audit_path, search_fn=_stub_search_fn, bus=None,
        hermes_root=tmp_path, render_fn=render_fn,
    )

    for agent in AGENTS:
        target = tmp_path / "profiles" / agent / "memories" / "MEMORY.md"
        assert target.exists()
        assert agent in target.read_text(encoding="utf-8")


def test_orchestrator_main_uses_append_mode(tmp_path):
    """For agent='main', renderer mode='append' and existing content preserved."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("")
    for agent in AGENTS:
        (tmp_path / "profiles" / agent / "memories").mkdir(parents=True, exist_ok=True)

    main_path = tmp_path / "profiles" / "main" / "memories" / "MEMORY.md"
    sentinel_text = "# Diego original 174KB content\n" + ("Original line.\n" * 200)
    main_path.write_text(sentinel_text, encoding="utf-8")
    pre_size = main_path.stat().st_size

    captured_modes: dict = {}

    def render_fn(agent, *_a, **kwargs):
        captured_modes[agent] = kwargs.get("mode")
        existing = kwargs.get("existing_content", "")
        if kwargs.get("mode") == "append":
            return existing + "\n\n---\n\n# Curator-Bootstrapped\n## Operating Stats\nx\n"
        return f"# MEMORY — {agent}\n\n## Operating Stats\nx\n"

    run_backfill(
        window_days=30, dry_run=False, emit_event=False,
        audit_path=audit_path, search_fn=_stub_search_fn, bus=None,
        hermes_root=tmp_path, render_fn=render_fn,
    )

    assert captured_modes["main"] == "append"
    post_text = main_path.read_text(encoding="utf-8")
    assert post_text.startswith(sentinel_text)
    assert main_path.stat().st_size > pre_size


def test_orchestrator_other_agents_use_preserve_with_prior(tmp_path):
    """For non-main agents with >30 lines existing, mode='preserve_with_prior'."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("")
    for agent in AGENTS:
        (tmp_path / "profiles" / agent / "memories").mkdir(parents=True, exist_ok=True)

    scout_path = tmp_path / "profiles" / "scout" / "memories" / "MEMORY.md"
    scout_path.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")

    captured: dict = {}

    def render_fn(agent, *_a, **kwargs):
        captured[agent] = kwargs.get("mode")
        return f"# MEMORY — {agent}\n## Operating Stats\nx\n"

    run_backfill(
        window_days=30, dry_run=False, emit_event=False,
        audit_path=audit_path, search_fn=_stub_search_fn, bus=None,
        hermes_root=tmp_path, render_fn=render_fn,
    )
    assert captured["scout"] == "preserve_with_prior"


def test_orchestrator_dry_run_writes_nothing(tmp_path):
    """When dry_run=True, no MEMORY.md is modified."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("")
    for agent in AGENTS:
        d = tmp_path / "profiles" / agent / "memories"
        d.mkdir(parents=True, exist_ok=True)
        (d / "MEMORY.md").write_text("ORIGINAL", encoding="utf-8")

    def render_fn(agent, *_a, **_k):
        return f"# MEMORY — {agent}\nNEW\n"

    result = run_backfill(
        window_days=30, dry_run=True, emit_event=False,
        audit_path=audit_path, search_fn=_stub_search_fn, bus=None,
        hermes_root=tmp_path, render_fn=render_fn,
    )

    for agent in AGENTS:
        target = tmp_path / "profiles" / agent / "memories" / "MEMORY.md"
        assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert result.bytes_written == 0


def test_orchestrator_continues_on_per_agent_failure(tmp_path):
    """One agent's renderer raises; others still complete."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("")
    for agent in AGENTS:
        (tmp_path / "profiles" / agent / "memories").mkdir(parents=True, exist_ok=True)

    def render_fn(agent, *_a, **_k):
        if agent == "tailor":
            raise RuntimeError("simulated failure")
        return f"# MEMORY — {agent}\n## Operating Stats\nx\n"

    result = run_backfill(
        window_days=30, dry_run=False, emit_event=False,
        audit_path=audit_path, search_fn=_stub_search_fn, bus=None,
        hermes_root=tmp_path, render_fn=render_fn,
    )

    failed_agents = [a for a, _ in result.agents_failed]
    assert "tailor" in failed_agents
    assert len(result.agents_updated) == len(AGENTS) - 1


def test_orchestrator_emits_curator_daily_event(tmp_path):
    """When emit_event=True with a bus, exactly one curator_daily event lands."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("")
    for agent in AGENTS:
        (tmp_path / "profiles" / agent / "memories").mkdir(parents=True, exist_ok=True)

    def render_fn(agent, *_a, **_k):
        return f"# MEMORY — {agent}\n## Operating Stats\nx\n"

    bus = _FakeBus()
    run_backfill(
        window_days=30, dry_run=False, emit_event=True,
        audit_path=audit_path, search_fn=_stub_search_fn, bus=bus,
        hermes_root=tmp_path, render_fn=render_fn,
    )
    assert len(bus.events) == 1
    ev = bus.events[0]
    # Accept real Event (EventType enum), dict-style fallback, or string
    et_attr = getattr(ev, "event_type", None)
    if et_attr is not None:
        et_str = getattr(et_attr, "type_string", str(et_attr))
    else:
        et_str = ev.get("event_type") if isinstance(ev, dict) else None
    assert et_str == "curator_daily", (et_str, ev)


def test_curator_daily_payload_schema(tmp_path):
    """The emitted event payload includes all spec'd keys (Task 7)."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text("")
    for agent in AGENTS:
        (tmp_path / "profiles" / agent / "memories").mkdir(parents=True, exist_ok=True)

    def render_fn(agent, *_a, **_k):
        return f"# MEMORY — {agent}\n## Operating Stats\nx\n"

    bus = _FakeBus()
    run_backfill(
        window_days=30, dry_run=False, emit_event=True,
        audit_path=audit_path, search_fn=_stub_search_fn, bus=bus,
        hermes_root=tmp_path, render_fn=render_fn,
    )
    assert len(bus.events) == 1
    ev = bus.events[0]
    payload = getattr(ev, "payload", None)
    if payload is None and isinstance(ev, dict):
        payload = ev.get("payload")
    assert payload is not None
    for key in (
        "mode", "agents_updated", "patterns_seeded",
        "skills_observed", "drawers_scanned", "degraded", "duration_s",
    ):
        assert key in payload, f"missing payload key {key}: {payload}"
    assert isinstance(payload["agents_updated"], list)
    assert isinstance(payload["patterns_seeded"], int)
    assert isinstance(payload["drawers_scanned"], int)
    assert isinstance(payload["degraded"], bool)
    assert isinstance(payload["duration_s"], (int, float))


def test_emit_event_does_not_put_the_deployed_checkout_on_sys_path():
    """``_emit_event`` must not shadow the running checkout with a deployed one.

    It used to do, on every emit::

        agent_src = Path(r"C:/Users/diego/.hermes/agent-src")
        if str(agent_src) not in sys.path:
            sys.path.insert(0, str(agent_src))

    -- a hard-wired DEPLOYED path, at position 0, gated only on string
    membership rather than on whether ``events`` was already importable. Any
    process that emitted a curator event therefore resolved every later
    first-time import from ``~/.hermes/agent-src`` instead of the tree it was
    actually running from, so a fix present in the running checkout could be
    invisible and a bug fixed there could still appear. Same defect and same
    fix as ``devflow_delegation/adopt_history.py`` (e422d55ec0); pinned as a
    standing invariant by ``tests/test_live_root_isolation.py``.
    """
    import sys
    from datetime import datetime, timezone

    from curator.orchestrator import BackfillResult, _emit_event

    before = list(sys.path)
    _emit_event(
        _FakeBus(),
        "nightly",
        BackfillResult(mode="nightly"),
        datetime(2026, 8, 18, tzinfo=timezone.utc),
    )
    assert sys.path == before, (
        "_emit_event mutated sys.path; entries added: "
        f"{[e for e in sys.path if e not in set(before)]}"
    )
