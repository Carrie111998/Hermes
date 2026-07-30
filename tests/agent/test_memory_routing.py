from agent.memory_routing import (
    MemoryRoute,
    build_memory_route,
    parse_llm_route,
    rewrite_memory_query,
)


def test_dataclass_defaults_are_safe_and_complete():
    route = MemoryRoute()

    assert route.domains == []
    assert route.slots == []
    assert route.risk == "low"
    assert route.confidence == 0.0
    assert route.route_source == "none"
    assert route.reason == ""
    assert route.new_topic_candidate is None
    assert route.suggested_slots == []
    assert route.should_update_policy is False
    assert route.gate_required is False
    assert route.rewritten_query is None


def test_token_boundary_matching_prevents_raw_substring_false_positives():
    route = build_memory_route("Can you communicate the approach clearly?")

    assert route.domains == []
    assert route.slots == []
    assert route.route_source == "none"


def test_kr_stock_extends_investing_slots_for_korean_stock_query():
    route = build_memory_route("하이닉스 지금 어때?")

    assert route.domains == ["kr_stock"]
    assert "holdings" in route.slots
    assert "average_cost" in route.slots
    assert route.route_source == "deterministic"
    assert route.confidence > 0
    assert "하이닉스" in route.reason


def test_safety_trigger_uses_token_boundaries_and_requires_gate():
    risky = build_memory_route("Please delete my saved memories")
    safe = build_memory_route("This catalogue should stay available")

    assert risky.risk == "high"
    assert risky.gate_required is True
    assert risky.route_source == "deterministic"
    assert safe.risk == "low"
    assert safe.gate_required is False


def test_parse_llm_route_normalizes_payload_and_marks_policy_proposal():
    route = parse_llm_route(
        {
            "domains": "gardening",
            "slots": ["soil_ph"],
            "confidence": "0.7",
            "reason": "User is asking about a new recurring topic",
            "new_topic_candidate": "gardening",
            "suggested_slots": ["soil_ph", "sunlight"],
        }
    )

    assert route.domains == ["gardening"]
    assert route.slots == ["soil_ph"]
    assert route.confidence == 0.7
    assert route.route_source == "llm"
    assert route.should_update_policy is True
    assert route.new_topic_candidate == "gardening"
    assert route.suggested_slots == ["soil_ph", "sunlight"]


def test_build_route_prefers_deterministic_match_but_keeps_llm_new_topic_signal():
    route = build_memory_route(
        "하이닉스 지금 어때?",
        llm_payload={
            "domains": ["investing"],
            "slots": ["watchlist"],
            "new_topic_candidate": "semiconductor_cycle",
            "suggested_slots": ["cycle_notes"],
        },
    )

    assert route.domains == ["kr_stock"]
    assert "holdings" in route.slots
    assert route.route_source == "deterministic"
    assert route.should_update_policy is True
    assert route.new_topic_candidate == "semiconductor_cycle"
    assert route.suggested_slots == ["cycle_notes"]


def test_rewrite_memory_query_includes_slot_names_not_values():
    route = MemoryRoute(domains=["kr_stock"], slots=["holdings", "average_cost"])

    rewritten = rewrite_memory_query("하이닉스 지금 어때?", route)

    assert "하이닉스 지금 어때?" in rewritten
    assert "slots: holdings, average_cost" in rewritten
    assert "domains: kr_stock" in rewritten
    assert "100" not in rewritten
    assert "50000" not in rewritten


def test_build_route_sets_rewritten_query_from_route_slots():
    route = build_memory_route("하이닉스 지금 어때?")

    assert route.rewritten_query is not None
    assert "slots:" in route.rewritten_query
    assert "holdings" in route.rewritten_query
