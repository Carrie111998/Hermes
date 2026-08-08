from __future__ import annotations

import hashlib
import json

import pytest


def _synthetic_frozen_prompt(length: int = 1544) -> str:
    seed = "Synthetic frozen image prompt for an isolated no-provider regression. "
    return (seed * ((length // len(seed)) + 1))[:length]


def _literal_truncate(prompt: str, length: int = 221) -> str:
    marker = "...[truncated]"
    return prompt[: length - len(marker)] + marker


def _sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _install_fake_provider(monkeypatch, image_tool, payload):
    calls = []

    def fake_dispatch(prompt, aspect_ratio, **kwargs):
        calls.append((prompt, aspect_ratio, kwargs))
        return json.dumps(payload)

    def fail_unexpected_route(*args, **kwargs):
        pytest.fail("image generation escaped the isolated fake provider route")

    monkeypatch.setattr(image_tool, "_dispatch_to_plugin_provider", fake_dispatch)
    monkeypatch.setattr(image_tool, "_maybe_route_managed_krea", fail_unexpected_route)
    monkeypatch.setattr(image_tool, "image_generate_tool", fail_unexpected_route)
    return calls


def test_literal_truncation_is_rejected_before_provider_dispatch(monkeypatch):
    from tools import image_generation_tool as image_tool

    frozen_prompt = _synthetic_frozen_prompt()
    actual_prompt = _literal_truncate(frozen_prompt)
    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/must-not-be-admitted.png"},
    )

    result = json.loads(
        image_tool._handle_image_generate(
            {
                "prompt": actual_prompt,
                "aspect_ratio": "landscape",
                "frozen_prompt_length_chars": len(frozen_prompt),
                "frozen_prompt_sha256": _sha256(frozen_prompt),
            }
        )
    )

    assert len(frozen_prompt) == 1544
    assert len(actual_prompt) == 221
    assert actual_prompt.endswith("...[truncated]")
    assert calls == []
    assert result["success"] is False
    assert result["image"] is None
    assert result["error_type"] == "prompt_integrity"
    assert result["reason_code"] == "PROMPT_ARGUMENT_LITERAL_TRUNCATION"
    assert result["prompt_integrity"] == {
        "verified": False,
        "output_admitted": False,
        "frozen_prompt_length_chars": 1544,
        "actual_prompt_length_chars": 221,
        "frozen_prompt_sha256": _sha256(frozen_prompt),
        "actual_prompt_sha256": _sha256(actual_prompt),
        "utf8_encoding_valid": True,
        "literal_truncation_marker_present": True,
        "length_matches": False,
        "sha256_matches": False,
    }


def test_registry_execution_boundary_rejects_persisted_actual_arguments(monkeypatch):
    from model_tools import handle_function_call
    from tools import image_generation_tool as image_tool

    frozen_prompt = _synthetic_frozen_prompt()
    actual_prompt = _literal_truncate(frozen_prompt)
    persisted_actual_arguments = {
        "prompt": actual_prompt,
        "aspect_ratio": "landscape",
        "frozen_prompt_length_chars": len(frozen_prompt),
        "frozen_prompt_sha256": _sha256(frozen_prompt),
    }
    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/must-not-be-admitted.png"},
    )

    result = json.loads(
        handle_function_call(
            "image_generate",
            persisted_actual_arguments,
            task_id="prompt-integrity-regression",
        )
    )

    assert calls == []
    assert persisted_actual_arguments["prompt"] == actual_prompt
    assert result["reason_code"] == "PROMPT_ARGUMENT_LITERAL_TRUNCATION"
    assert result["prompt_integrity"]["actual_prompt_length_chars"] == 221
    assert result["prompt_integrity"]["actual_prompt_sha256"] == _sha256(actual_prompt)
    assert result["prompt_integrity"]["output_admitted"] is False


def test_handler_verifies_the_same_prompt_snapshot_used_for_dispatch(monkeypatch):
    from tools import image_generation_tool as image_tool

    frozen_prompt = "frozen prompt"

    class MutatingPromptArgs(dict):
        prompt_reads = 0

        def get(self, key, default=None):
            if key == "prompt":
                self.prompt_reads += 1
                return "Frozen prompt" if self.prompt_reads == 1 else frozen_prompt
            return super().get(key, default)

    args = MutatingPromptArgs(
        frozen_prompt_length_chars=len(frozen_prompt),
        frozen_prompt_sha256=_sha256(frozen_prompt),
    )
    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/must-not-be-admitted.png"},
    )

    result = json.loads(image_tool._handle_image_generate(args))

    assert args.prompt_reads == 1
    assert calls == []
    assert result["reason_code"] == "PROMPT_ARGUMENT_SHA256_MISMATCH"
    assert result["prompt_integrity"]["actual_prompt_sha256"] == _sha256(
        "Frozen prompt"
    )
    assert result["prompt_integrity"]["output_admitted"] is False


def test_literal_truncation_is_fail_closed_without_frozen_contract(monkeypatch):
    from tools import image_generation_tool as image_tool

    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/must-not-be-admitted.png"},
    )

    result = json.loads(
        image_tool._handle_image_generate(
            {"prompt": "ordinary prompt...[truncated]", "aspect_ratio": "square"}
        )
    )

    assert calls == []
    assert result["reason_code"] == "PROMPT_ARGUMENT_LITERAL_TRUNCATION"
    assert result["prompt_integrity"]["output_admitted"] is False


@pytest.mark.parametrize(
    ("actual_prompt", "expected_reason"),
    [
        ("frozen prompt!", "PROMPT_ARGUMENT_LENGTH_MISMATCH"),
        ("Frozen prompt", "PROMPT_ARGUMENT_SHA256_MISMATCH"),
    ],
)
def test_length_and_sha256_mismatches_are_rejected_before_dispatch(
    monkeypatch, actual_prompt, expected_reason
):
    from tools import image_generation_tool as image_tool

    frozen_prompt = "frozen prompt"
    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/must-not-be-admitted.png"},
    )

    result = json.loads(
        image_tool._handle_image_generate(
            {
                "prompt": actual_prompt,
                "frozen_prompt_length_chars": len(frozen_prompt),
                "frozen_prompt_sha256": _sha256(frozen_prompt),
            }
        )
    )

    assert calls == []
    assert result["success"] is False
    assert result["reason_code"] == expected_reason
    assert result["prompt_integrity"]["output_admitted"] is False


@pytest.mark.parametrize(
    "contract",
    [
        {"frozen_prompt_length_chars": None},
        {"frozen_prompt_sha256": None},
        {
            "frozen_prompt_length_chars": None,
            "frozen_prompt_sha256": None,
        },
        {"frozen_prompt_length_chars": 13},
        {"frozen_prompt_sha256": "0" * 64},
        {
            "frozen_prompt_length_chars": True,
            "frozen_prompt_sha256": "0" * 64,
        },
        {
            "frozen_prompt_length_chars": 13,
            "frozen_prompt_sha256": "A" * 64,
        },
        {
            "frozen_prompt_length_chars": 13,
            "frozen_prompt_sha256": "é" * 64,
        },
    ],
)
def test_incomplete_or_malformed_contract_is_fail_closed(monkeypatch, contract):
    from tools import image_generation_tool as image_tool

    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/must-not-be-admitted.png"},
    )

    result = json.loads(
        image_tool._handle_image_generate({"prompt": "frozen prompt", **contract})
    )

    assert calls == []
    assert result["reason_code"] == "PROMPT_INTEGRITY_CONTRACT_INVALID"
    assert result["prompt_integrity"]["output_admitted"] is False


def test_empty_actual_prompt_with_contract_returns_integrity_evidence(monkeypatch):
    from tools import image_generation_tool as image_tool

    frozen_prompt = "frozen prompt"
    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/must-not-be-admitted.png"},
    )

    result = json.loads(
        image_tool._handle_image_generate(
            {
                "prompt": "",
                "frozen_prompt_length_chars": len(frozen_prompt),
                "frozen_prompt_sha256": _sha256(frozen_prompt),
            }
        )
    )

    assert calls == []
    assert result["reason_code"] == "PROMPT_ARGUMENT_LENGTH_MISMATCH"
    assert result["prompt_integrity"]["actual_prompt_length_chars"] == 0
    assert result["prompt_integrity"]["output_admitted"] is False


def test_unpaired_surrogate_with_contract_is_rejected_before_dispatch(monkeypatch):
    from tools import image_generation_tool as image_tool

    frozen_prompt = "frozen prompt"
    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/must-not-be-admitted.png"},
    )

    result = json.loads(
        image_tool._handle_image_generate(
            {
                "prompt": "\ud800",
                "frozen_prompt_length_chars": len(frozen_prompt),
                "frozen_prompt_sha256": _sha256(frozen_prompt),
            }
        )
    )

    assert calls == []
    assert result["reason_code"] == "PROMPT_ARGUMENT_UTF8_ENCODING_INVALID"
    assert result["prompt_integrity"]["actual_prompt_sha256"] is None
    assert result["prompt_integrity"]["utf8_encoding_valid"] is False
    assert result["prompt_integrity"]["output_admitted"] is False


def test_exact_unicode_prompt_is_dispatched_and_admission_is_bound(monkeypatch):
    from tools import image_generation_tool as image_tool

    frozen_prompt = "ภาพสินค้า 🪽 exact\nsecond line"
    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/admitted.png"},
    )

    result = json.loads(
        image_tool._handle_image_generate(
            {
                "prompt": frozen_prompt,
                "aspect_ratio": "portrait",
                "frozen_prompt_length_chars": len(frozen_prompt),
                "frozen_prompt_sha256": _sha256(frozen_prompt),
            }
        )
    )

    assert calls == [
        (
            frozen_prompt,
            "portrait",
            {"image_url": None, "reference_image_urls": None},
        )
    ]
    assert result["success"] is True
    assert result["image"] == "/tmp/admitted.png"
    assert result["prompt_integrity"] == {
        "verified": True,
        "output_admitted": True,
        "frozen_prompt_length_chars": len(frozen_prompt),
        "actual_prompt_length_chars": len(frozen_prompt),
        "frozen_prompt_sha256": _sha256(frozen_prompt),
        "actual_prompt_sha256": _sha256(frozen_prompt),
        "utf8_encoding_valid": True,
        "literal_truncation_marker_present": False,
        "length_matches": True,
        "sha256_matches": True,
    }


def test_verified_prompt_does_not_admit_a_provider_failure(monkeypatch):
    from tools import image_generation_tool as image_tool

    frozen_prompt = "exact prompt"
    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": False, "image": None, "error": "synthetic provider failure"},
    )

    result = json.loads(
        image_tool._handle_image_generate(
            {
                "prompt": frozen_prompt,
                "frozen_prompt_length_chars": len(frozen_prompt),
                "frozen_prompt_sha256": _sha256(frozen_prompt),
            }
        )
    )

    assert len(calls) == 1
    assert result["success"] is False
    assert result["prompt_integrity"]["verified"] is True
    assert result["prompt_integrity"]["output_admitted"] is False


def test_non_boolean_provider_success_is_not_marked_admitted(monkeypatch):
    from tools import image_generation_tool as image_tool

    frozen_prompt = "exact prompt"
    _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": "false", "image": "/tmp/malformed-provider-result.png"},
    )

    result = json.loads(
        image_tool._handle_image_generate(
            {
                "prompt": frozen_prompt,
                "frozen_prompt_length_chars": len(frozen_prompt),
                "frozen_prompt_sha256": _sha256(frozen_prompt),
            }
        )
    )

    assert result["prompt_integrity"]["verified"] is True
    assert result["prompt_integrity"]["output_admitted"] is False


def test_call_without_integrity_contract_remains_backward_compatible(monkeypatch):
    from tools import image_generation_tool as image_tool

    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/legacy.png"},
    )

    result = json.loads(
        image_tool._handle_image_generate(
            {"prompt": "ordinary complete prompt", "aspect_ratio": "landscape"}
        )
    )

    assert len(calls) == 1
    assert result == {"success": True, "image": "/tmp/legacy.png"}


def test_legacy_unpaired_surrogate_is_not_prehashed(monkeypatch):
    from tools import image_generation_tool as image_tool

    prompt = "legacy \ud800 prompt"
    calls = _install_fake_provider(
        monkeypatch,
        image_tool,
        {"success": True, "image": "/tmp/legacy.png"},
    )

    result = json.loads(image_tool._handle_image_generate({"prompt": prompt}))

    assert calls[0][0] == prompt
    assert result == {"success": True, "image": "/tmp/legacy.png"}


def test_schema_exposes_the_frozen_prompt_contract():
    from tools.image_generation_tool import IMAGE_GENERATE_SCHEMA

    properties = IMAGE_GENERATE_SCHEMA["parameters"]["properties"]

    assert properties["frozen_prompt_length_chars"]["type"] == "integer"
    assert properties["frozen_prompt_length_chars"]["minimum"] == 1
    assert properties["frozen_prompt_sha256"]["type"] == "string"
    assert properties["frozen_prompt_sha256"]["pattern"] == "^[0-9a-f]{64}$"
