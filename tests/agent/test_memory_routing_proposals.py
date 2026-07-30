import json

from agent.memory_routing import MemoryRoute
from agent.memory_routing_proposals import record_policy_proposal


def test_record_policy_proposal_noops_without_new_topic_candidate(tmp_path):
    route = MemoryRoute(suggested_slots=["anything"])

    result = record_policy_proposal(tmp_path, route, "remember this")

    assert result is None
    assert not (tmp_path / "memory-routing-proposals.json").exists()


def test_record_policy_proposal_creates_json_without_mutating_policy(tmp_path):
    policy_path = tmp_path / "memory-routing-policy.yaml"
    policy_text = "domains: {}\n"
    policy_path.write_text(policy_text, encoding="utf-8")
    route = MemoryRoute(
        new_topic_candidate="gardening",
        suggested_slots=["soil_ph", "sunlight"],
    )

    proposal_path = record_policy_proposal(tmp_path, route, "How is my basil doing?")

    assert proposal_path == tmp_path / "memory-routing-proposals.json"
    assert policy_path.read_text(encoding="utf-8") == policy_text
    data = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert data["gardening"]["count"] == 1
    assert data["gardening"]["examples"] == ["How is my basil doing?"]
    assert data["gardening"]["suggested_slots"] == ["soil_ph", "sunlight"]


def test_record_policy_proposal_updates_existing_entry(tmp_path):
    first_route = MemoryRoute(
        new_topic_candidate="gardening",
        suggested_slots=["soil_ph", "sunlight"],
    )
    second_route = MemoryRoute(
        new_topic_candidate="gardening",
        suggested_slots=["sunlight", "watering_schedule"],
    )
    first = record_policy_proposal(tmp_path, first_route, "How is my basil doing?")
    second = record_policy_proposal(tmp_path, second_route, "Should I water the basil?")

    assert second == first
    data = json.loads(first.read_text(encoding="utf-8"))
    assert data["gardening"]["count"] == 2
    assert data["gardening"]["examples"] == [
        "How is my basil doing?",
        "Should I water the basil?",
    ]
    assert data["gardening"]["suggested_slots"] == [
        "soil_ph",
        "sunlight",
        "watering_schedule",
    ]
