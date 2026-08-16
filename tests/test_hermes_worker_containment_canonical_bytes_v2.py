"""Behavior tests for the v2 canonical-bytes containment boundary."""

from __future__ import annotations

import json
import unittest

from hermes_worker_containment_canonical_bytes_v2 import (
    assess_worker_containment_canonical_bytes_v2,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


VALID_CANDIDATE = _canonical(
    {
        "attempts": 1,
        "contract_version": "hermes.worker_containment.canonical_bytes.v2",
        "credential_mode": "external_owner_handoff_required",
        "fanout": 0,
        "immutable_revision_claimed": False,
        "jobs": 1,
        "max_cost_usd_microdollars": 250_000,
        "max_input_tokens": 32_768,
        "max_output_bytes": 524_288,
        "max_output_tokens": 8_192,
        "max_total_tokens": 40_960,
        "model_call_limit": 1,
        "model_id": "glm-5.2",
        "provider_id": "zai",
        "provider_internal_revision": "unknown",
        "provider_internal_revision_owner_accepted": True,
        "provider_request_limit": 1,
        "repository_mount": False,
        "retry_count": 0,
        "tool_allowlist": [],
        "wall_clock_seconds": 900,
    }
)
VALID_ENVIRONMENT = _canonical(
    {
        "contract_version": "hermes.clean_environment.canonical_bytes.v2",
        "environment": {
            "HOME": "/private/run/home",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
        },
    }
)


def _candidate(**updates: object) -> bytes:
    value = json.loads(VALID_CANDIDATE)
    value.update(updates)
    return _canonical(value)


def _environment(environment: dict[str, str]) -> bytes:
    return _canonical(
        {
            "contract_version": "hermes.clean_environment.canonical_bytes.v2",
            "environment": environment,
        }
    )


def _receipt(candidate: object = VALID_CANDIDATE, environment: object = VALID_ENVIRONMENT):
    return json.loads(
        assess_worker_containment_canonical_bytes_v2(candidate, environment)
    )


class HermesWorkerContainmentCanonicalBytesV2Tests(unittest.TestCase):
    def assert_hold(self, candidate: object, environment: object, code: str):
        receipt = _receipt(candidate, environment)
        self.assertEqual(receipt["status"], "hold_missing_or_invalid")
        self.assertIn(code, receipt["blocking_codes"])
        self.assertFalse(receipt["execution_authorized"])
        self.assertFalse(receipt["safe_to_dispatch"])
        return receipt

    def test_exact_builtin_documents_are_canonical_and_non_authorizing(self):
        raw = assess_worker_containment_canonical_bytes_v2(
            VALID_CANDIDATE, VALID_ENVIRONMENT
        )
        receipt = json.loads(raw)
        self.assertEqual(raw, _canonical(receipt))
        self.assertEqual(
            receipt["status"], "canonical_containment_inputs_verified_contract_only"
        )
        self.assertEqual(receipt["requested_model_identity"]["provider_id"], "zai")
        self.assertEqual(receipt["requested_model_identity"]["model_id"], "glm-5.2")
        self.assertEqual(
            receipt["requested_model_identity"]["provider_internal_revision"],
            "unknown",
        )
        self.assertTrue(
            receipt["requested_model_identity"][
                "provider_internal_revision_owner_accepted"
            ]
        )
        self.assertFalse(receipt["requested_model_identity"]["immutable_revision_claimed"])
        self.assertEqual(receipt["requested_limits"]["max_input_tokens"], 32_768)
        self.assertEqual(receipt["requested_limits"]["max_output_tokens"], 8_192)
        self.assertEqual(receipt["requested_limits"]["max_total_tokens"], 40_960)
        self.assertEqual(receipt["requested_limits"]["max_output_bytes"], 524_288)
        self.assertEqual(
            receipt["requested_limits"]["max_cost_usd_microdollars"], 250_000
        )
        self.assertEqual(receipt["requested_limits"]["wall_clock_seconds"], 900)
        for key in (
            "credential_scope_verified",
            "execution_authorized",
            "host_containment_verified",
            "model_identity_effective_verified",
            "owner_approval_verified",
            "safe_to_dispatch",
            "token_limits_effective_verified",
            "tool_allowlist_effective_verified",
            "worker_runtime_verified",
        ):
            self.assertIs(receipt[key], False, key)

    def test_only_builtin_bytes_are_accepted(self):
        class HostileBytes(bytes):
            def __len__(self):
                raise AssertionError("length must not be dispatched on a subtype")

        self.assert_hold(HostileBytes(VALID_CANDIDATE), VALID_ENVIRONMENT, "candidate_document_type_invalid")
        self.assert_hold(bytearray(VALID_CANDIDATE), VALID_ENVIRONMENT, "candidate_document_type_invalid")
        self.assert_hold(VALID_CANDIDATE, "not-bytes", "environment_document_type_invalid")

    def test_noncanonical_duplicate_and_numeric_forms_fail_closed(self):
        self.assert_hold(VALID_CANDIDATE + b"\n", VALID_ENVIRONMENT, "candidate_document_noncanonical")
        duplicate = VALID_CANDIDATE[:-1] + b',"jobs":1}'
        self.assert_hold(duplicate, VALID_ENVIRONMENT, "candidate_document_duplicate_key")
        self.assert_hold(
            _candidate(max_input_tokens=32_768.0),
            VALID_ENVIRONMENT,
            "candidate_document_float_forbidden",
        )
        self.assert_hold(
            VALID_CANDIDATE.replace(b'"fanout":0', b'"fanout":-0'),
            VALID_ENVIRONMENT,
            "candidate_document_negative_zero_forbidden",
        )

    def test_representative_limits_and_revision_policy_are_exact(self):
        cases = (
            (_candidate(max_input_tokens=32_767), "candidate_max_input_tokens_invalid"),
            (_candidate(max_output_tokens=8_191), "candidate_max_output_tokens_invalid"),
            (_candidate(max_total_tokens=40_959), "candidate_max_total_tokens_invalid"),
            (_candidate(max_output_bytes=524_287), "candidate_max_output_bytes_invalid"),
            (_candidate(max_cost_usd_microdollars=250_001), "candidate_max_cost_usd_microdollars_invalid"),
            (_candidate(wall_clock_seconds=901), "candidate_wall_clock_seconds_invalid"),
            (_candidate(repository_mount=True), "candidate_repository_mount_invalid"),
            (_candidate(tool_allowlist=["terminal"]), "candidate_tool_allowlist_invalid"),
            (_candidate(provider_internal_revision="sha256:" + "a" * 64), "candidate_provider_internal_revision_invalid"),
            (_candidate(provider_internal_revision_owner_accepted=False), "candidate_provider_internal_revision_owner_accepted_invalid"),
            (_candidate(immutable_revision_claimed=True), "candidate_immutable_revision_claimed_invalid"),
            (_candidate(model_id="zai/latest"), "candidate_model_id_invalid"),
            (_candidate(provider_id="default"), "candidate_provider_id_invalid"),
        )
        for raw, code in cases:
            with self.subTest(code=code):
                self.assert_hold(raw, VALID_ENVIRONMENT, code)

    def test_shape_and_environment_allowlist_are_strict(self):
        value = json.loads(VALID_CANDIDATE)
        del value["jobs"]
        self.assert_hold(_canonical(value), VALID_ENVIRONMENT, "candidate_shape_invalid")
        value["jobs"] = 1
        value["extra"] = False
        self.assert_hold(_canonical(value), VALID_ENVIRONMENT, "candidate_shape_invalid")
        self.assert_hold(
            VALID_CANDIDATE,
            _environment({"PATH": "/usr/bin"}),
            "environment_key_invalid",
        )
        self.assert_hold(
            VALID_CANDIDATE,
            _environment({"HOME": "line\nbreak"}),
            "environment_value_invalid",
        )
        self.assert_hold(
            VALID_CANDIDATE,
            _environment({"HOME": ""}),
            "environment_value_invalid",
        )


if __name__ == "__main__":
    unittest.main()
