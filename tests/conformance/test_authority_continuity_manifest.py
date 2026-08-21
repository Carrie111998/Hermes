"""Conformance tests for the proof-carrying authority interlock manifest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "docs" / "architecture" / "authority-continuity.json"

EXPECTED_PIPELINE = ["admit", "carry", "effect", "settle", "publish"]
EXPECTED_INTERLOCKS = {
    "compression_projection",
    "discord_voice_v6",
    "godfile_campaign_topology",
    "hermes_tag",
    "mcp_oauth",
    "publication_contracts",
    "secret_provider_egress",
    "webhook",
    "webhook_docs_assembly",
    "windows_update_bootstrap",
}
EXPECTED_WEBHOOK_ORDER = [
    85002,
    90995,
    90236,
    85318,
    90304,
    85644,
    85638,
    85640,
]


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _interlocks_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    interlocks = data["interlocks"]
    assert isinstance(interlocks, list)
    indexed = {item["id"]: item for item in interlocks}
    assert len(indexed) == len(interlocks), "interlock ids must be unique"
    return indexed


def _assert_node(node: dict[str, Any]) -> None:
    assert node["kind"] in {"issue", "pull_request"}
    assert isinstance(node["number"], int)
    assert node["number"] > 0


def test_manifest_has_one_exact_evidence_pipeline() -> None:
    data = _manifest()

    assert data["schema_version"] == 1
    assert data["repository"] == "NousResearch/hermes-agent"
    assert data["architecture_owner_issue"] == 90866
    assert data["completion_owner_issue"] == 91230
    assert data["evidence_policy"] == "exact_object_only"

    pipeline = data["pipeline"]
    assert [stage["id"] for stage in pipeline] == EXPECTED_PIPELINE
    assert all(stage["requires"] for stage in pipeline)


def test_manifest_covers_the_current_interlock_domains() -> None:
    interlocks = _interlocks_by_id(_manifest())

    assert set(interlocks) == EXPECTED_INTERLOCKS
    for interlock in interlocks.values():
        _assert_node(interlock["architecture_owner"])
        owner = interlock.get("active_delivery_owner")
        if owner is not None:
            _assert_node(owner)
        assert interlock["constraints"]


def test_webhook_chain_has_one_spine_and_settlement_before_docs() -> None:
    webhook = _interlocks_by_id(_manifest())["webhook"]

    assert webhook["active_delivery_owner"] == {
        "kind": "pull_request",
        "number": 90995,
    }
    assert [stage["number"] for stage in webhook["stages"]] == EXPECTED_WEBHOOK_ORDER
    assert [stage["role"] for stage in webhook["stages"]] == [
        "effective_config",
        "domain_spine",
        "http_mechanics",
        "signature_verifier",
        "session_admission",
        "effect_settlement",
        "docs_projection",
        "terminal_assembler",
    ]


def test_mcp_parent_cannot_retire_while_transport_child_is_missing() -> None:
    mcp = _interlocks_by_id(_manifest())["mcp_oauth"]
    roles = {item["role"]: item["publication"] for item in mcp["required_child_roles"]}

    assert mcp["architecture_owner"]["number"] == 84963
    assert mcp["parent_retirement_gate"] == "semantic_exhaustiveness"
    assert mcp["parent_may_retire"] is False
    assert roles["oauth_lifecycle"] == {
        "kind": "pull_request",
        "number": 90888,
    }
    assert roles["mcp2_transport_control_plane"] is None


def test_compression_is_one_delivery_lane_with_two_proof_dimensions() -> None:
    compression = _interlocks_by_id(_manifest())["compression_projection"]

    assert compression["active_delivery_owner"]["number"] == 88551
    assert compression["delivery_lanes"] == 1
    assert {node["number"] for node in compression["specification_nodes"]} == {
        88740,
        88758,
    }
    assert compression["proof_dimensions"] == ["_row_id", "_row_id_watermark"]


def test_publication_contract_precedes_channel_ledgers() -> None:
    publication = _interlocks_by_id(_manifest())["publication_contracts"]

    assert publication["active_delivery_owner"]["number"] == 90307
    assert {(item["channel"], item["number"]) for item in publication["consumers"]} == {
        ("discord", 90321),
        ("slack", 91036),
    }
    assert any(
        "Narrative counts" in constraint
        for constraint in publication["constraints"]
    )


def test_fossils_and_historical_candidates_cannot_claim_current_main_closure() -> None:
    interlocks = _interlocks_by_id(_manifest())

    updater = interlocks["windows_update_bootstrap"]
    assert updater["historical_fossil"] == {
        "kind": "pull_request",
        "number": 60233,
        "disposition": "invariant_only",
    }
    assert updater["admission_owner"]["number"] == 91316
    assert updater["active_delivery_owner"]["number"] == 91895

    secret_egress = interlocks["secret_provider_egress"]
    assert secret_egress["architecture_owner"]["number"] == 77162
    assert secret_egress["active_delivery_owner"]["number"] == 77198
    assert secret_egress["closure_state"] == "open_current_main_unverified"


def test_godfile_campaign_selects_semantic_slices_not_pr_population() -> None:
    campaign = _interlocks_by_id(_manifest())["godfile_campaign_topology"]

    assert campaign["architecture_owner"]["number"] == 78647
    assert campaign["active_delivery_owner"] is None
    assert campaign["selection_authority"] == "kill_lock_plus_current_main"
    assert campaign["survivor_unit"] == "semantic_slice"
