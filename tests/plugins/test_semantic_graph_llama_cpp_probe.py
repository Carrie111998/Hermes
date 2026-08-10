"""Unit tests for the llama.cpp embedding capability probe."""

from __future__ import annotations

import json

import pytest

from scripts.semantic_graph_llama_embedding_probe import (
    ProbeConfig,
    ProbeResult,
    check_batch_stability,
    determine_verdict,
    required_check_names,
    check_finite_norm,
    check_repeat_stability,
    check_semantic_ordering,
    check_shape,
    cosine,
    make_report,
    serialize_query,
    stability_thresholds,
)


BASE_CHECKS = {
    "health": True,
    "v1_models": True,
    "v1_embeddings": True,
    "one_vector_per_input": True,
    "all_finite": True,
    "all_nonzero": True,
    "unit_norm": True,
    "repeat_stability": True,
    "batch_stability": True,
    "semantic_ordering": True,
}


def _response(vectors: list[list[float]], indices: list[int] | None = None) -> dict[str, object]:
    return {
        "data": [
            {"index": index, "embedding": vector}
            for index, vector in zip(indices or list(range(len(vectors))), vectors)
        ]
    }


def test_probe_config_defaults_are_candidate_specific() -> None:
    config = ProbeConfig()
    assert config.expected_dimensions == 2048
    assert config.profile == "qwen3_vl_candidate"
    assert config.soak_requests == 0
    assert config.soak_duration_seconds == 0.0


def test_control_config_is_supported() -> None:
    config = ProbeConfig(
        base_url="http://127.0.0.1:8083",
        model="qwen3-embedding-0.6b-q8_0",
        repo="Qwen/Qwen3-Embedding-0.6B-GGUF",
        expected_dimensions=1024,
        profile="qwen3_text_control",
    )
    assert config.expected_dimensions == 1024
    assert config.profile == "qwen3_text_control"


def test_bge_m3_control_uses_raw_serializer_and_control_thresholds() -> None:
    assert serialize_query("query", profile="bge_m3_control") == "query"
    assert stability_thresholds("bge_m3_control") == (0.9999, 0.999)
    assert serialize_query(
        "query", profile="qwen3_text", instruction="Retrieve memories."
    ) == "Instruct: Retrieve memories.\nQuery:query"
    with pytest.raises(ValueError, match="Qwen3 profiles require an instruction"):
        serialize_query("query", profile="qwen3_text")


def test_shape_accepts_flat_vectors_and_reorders_by_index() -> None:
    ok, message, vectors = check_shape(
        _response([[0.0, 1.0], [1.0, 0.0]], indices=[1, 0]), 2, 2
    )
    assert ok is True
    assert message == ""
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"data": [{"index": 0, "embedding": [[1.0, 0.0]]}]}, "flat"),
        ({"data": [{"index": 0, "embedding": [None, 0.0]}]}, "scalar"),
        ({"data": [{"index": 0, "embedding": []}]}, "empty"),
        ({"data": [{"index": 0, "embedding": [1.0, 0.0]}]}, "dimension"),
        (
            {
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
            "unique",
        ),
    ],
)
def test_shape_rejects_unsafe_or_incompatible_payloads(
    payload: dict[str, object], expected: str
) -> None:
    ok, message, _ = check_shape(
        payload, 2 if expected == "unique" else 1, 3 if expected == "dimension" else 2
    )
    assert ok is False
    assert message


def test_shape_rejects_missing_index_coverage() -> None:
    ok, message, _ = check_shape(
        _response([[1.0, 0.0], [0.0, 1.0]], indices=[0, 2]), 2, 2
    )
    assert ok is False
    assert "indices" in message


def test_finite_norm_rejects_nan_inf_and_zero() -> None:
    assert check_finite_norm([[0.6, 0.8]])[0] is True
    assert check_finite_norm([[None, 0.0]])[0] is False
    assert check_finite_norm([[float("nan"), 0.0]])[0] is False
    assert check_finite_norm([[float("inf"), 0.0]])[0] is False
    assert check_finite_norm([[0.0, 0.0]])[0] is False
    assert check_finite_norm([[2.0, 0.0]])[0] is False


def test_repeat_and_batch_stability_thresholds() -> None:
    stable = [[0.6, 0.8] for _ in range(20)]
    assert check_repeat_stability(stable)[0] is True
    assert check_batch_stability([[0.6, 0.8]], [[0.6, 0.8]])[0] is True
    assert check_repeat_stability([[1.0, 0.0], [0.0, 1.0]])[0] is False


def test_semantic_ordering_and_cosine() -> None:
    query = [1.0, 0.0, 0.0]
    ok, _, metrics = check_semantic_ordering(
        query,
        {"A": [0.9, 0.1, 0.0], "B": [0.1, 0.9, 0.0], "C": [0.0, 0.0, 1.0]},
    )
    assert ok is True
    assert metrics["semantic_margin"] > 0.03
    assert cosine(query, query) == pytest.approx(1.0)


def test_report_is_summary_only_and_redacts_paths() -> None:
    report = make_report(
        ProbeConfig(
            base_url="http://127.0.0.1:8082",
            repo="Etherll/Qwen3-VL-Embedding-2B-Q8_0-GGUF",
            gguf_path="C:/Users/downl/private/model.gguf",
        ),
        checks={"health": True},
        metrics={"norm_min": 1.0},
        actual_dimensions=2048,
    )
    encoded = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "raw_vectors" not in encoded
    assert "C:/Users/downl/private" not in encoded
    assert "private/model.gguf" not in encoded
    assert "token" not in encoded.lower()


def test_report_verdict_requires_all_compatibility_checks() -> None:
    config = ProbeConfig(expected_dimensions=2048)
    result = make_report(
        config,
        checks={"health": True, "v1_models": True, "v1_embeddings": True},
        metrics={},
        actual_dimensions=2048,
    )
    assert result.verdict == "fail_model_compatibility"
    passing = make_report(
        config,
        checks={**BASE_CHECKS, "soak": None},
        metrics={},
        actual_dimensions=2048,
    )
    assert passing.verdict == "pass_text_only"


def test_zero_soak_requests_does_not_fail_otherwise_passing_probe() -> None:
    assert determine_verdict({**BASE_CHECKS, "soak": None}, soak_requests=0) == "pass_text_only"


def test_zero_soak_requests_records_not_run() -> None:
    result = make_report(
        ProbeConfig(soak_requests=0),
        checks={**BASE_CHECKS, "soak": None},
        metrics={},
        actual_dimensions=2048,
    )
    data = result.to_dict()
    assert data["checks"]["soak"] is None
    assert data["soak"] == {
        "requested": False,
        "request_count": 0,
        "passed": None,
    }


def test_observations_are_not_required_verdict_checks() -> None:
    result = make_report(
        ProbeConfig(soak_requests=0),
        checks={**BASE_CHECKS, "soak": None},
        metrics={},
        actual_dimensions=2048,
        observations={
            "japanese_cross_lingual": {"passed": False},
            "instruction_profile_comparison": {"passed": False},
        },
    )
    data = result.to_dict()
    assert result.verdict == "pass_text_only"
    assert data["observations"]["japanese_cross_lingual"]["passed"] is False


def test_requested_successful_soak_is_required_and_passes() -> None:
    assert "soak" in required_check_names(soak_requests=500)
    assert determine_verdict({**BASE_CHECKS, "soak": True}, soak_requests=500) == "pass_text_only"


def test_requested_failed_soak_fails_probe() -> None:
    assert determine_verdict({**BASE_CHECKS, "soak": False}, soak_requests=500) == "fail_model_compatibility"


def test_negative_soak_requests_are_rejected() -> None:
    with pytest.raises(ValueError, match="soak_requests must be non-negative"):
        required_check_names(soak_requests=-1)


def test_probe_result_json_has_no_raw_vectors() -> None:
    rendered = ProbeResult(verdict="pass_text_only").to_json()
    data = json.loads(rendered)
    assert data["schema_version"] == 1
    assert "raw_vectors" not in data
    assert "vector" not in rendered.lower()


def test_probe_result_serializes_optional_error_summary() -> None:
    result = ProbeResult(verdict="fail_model_compatibility", errors=["dimension mismatch"])
    data = json.loads(result.to_json())
    assert data["errors"] == ["dimension mismatch"]


def test_probe_config_normalizes_loopback_url() -> None:
    assert ProbeConfig(base_url="http://127.0.0.1:8082/").normalized_base_url.endswith(":8082")
