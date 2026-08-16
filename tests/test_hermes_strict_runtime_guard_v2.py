"""Behavior tests for the pure v2 runtime-guard state machine."""

from __future__ import annotations

import base64
import hashlib
import json
import unittest

from hermes_strict_runtime_guard_v2 import (
    EVENT_VERSION,
    new_strict_runtime_guard_v2,
    prove_strict_runtime_guard_v2,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _capsule(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "api_mode": "chat_completions",
        "attempt_limit": 1,
        "contract_version": "hermes.strict_no_send.request_capsule.v2",
        "credential_handoff": "external_owner_handoff_required",
        "fallback_model_ids": [],
        "fallback_provider_ids": [],
        "fanout": 0,
        "immutable_revision_claimed": False,
        "job_limit": 1,
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
        "provider_profile_api_mode": "chat_completions",
        "provider_profile_declared_base_url": "https://api.z.ai/api/paas/v4",
        "provider_request_limit": 1,
        "repository_mount": False,
        "retry_count": 0,
        "tool_names": [],
        "wall_clock_seconds": 900,
    }
    value.update(overrides)
    return value


H5_BLOCKING_CODES = [
    "credential_handoff_required",
    "credential_scope_effective_verification_required",
    "effective_provider_endpoint_verification_required",
    "external_dependency_graph_verification_required",
    "host_containment_proof_required",
    "interpreter_bootstrap_filesystem_side_effect_verification_required",
    "owner_approval_required",
    "runtime_token_enforcement_required",
    "strict_worker_runner_required",
    "trusted_implementation_graph_anchor_required",
]


def _h5_receipt(*, capsule: dict[str, object] | None = None, **overrides: object) -> bytes:
    request_capsule = capsule or _capsule()
    value: dict[str, object] = {
        "activation_state": "hold_no_send",
        "actual_cost_usd_microdollars": 0,
        "actual_output_bytes": 0,
        "blocking_codes": H5_BLOCKING_CODES,
        "candidate_document_sha256": "1" * 64,
        "clean_environment_document_sha256": "2" * 64,
        "contract_version": "hermes.strict_no_send_preflight.receipt.v2",
        "credential_environment_boundary_preflight_verified": True,
        "credential_environment_names": [
            "GLM_API_KEY",
            "ZAI_API_KEY",
            "Z_AI_API_KEY",
        ],
        "credential_scope_effective_verified": False,
        "execution_authorized": False,
        "external_dependency_graph_verified": False,
        "external_send": False,
        "filesystem_mutation_effective_verified": False,
        "h4_candidate_input_verified": True,
        "h4_environment_input_verified": True,
        "host_containment_verified": False,
        "implementation_graph_digest_semantics": "local_python_source_canonical_lf_v2",
        "implementation_graph_file_count": 49,
        "implementation_graph_sha256": "sha256:" + "a" * 64,
        "immutable_revision_claimed": False,
        "job_count": 0,
        "local_implementation_graph_expected_match": True,
        "local_implementation_graph_trusted_anchor_verified": False,
        "model_call_count": 0,
        "model_identity_preflight_verified": True,
        "model_revision_immutable_verified": False,
        "network_access": False,
        "no_send_audit_hook_installed": True,
        "ordinary_runtime_imported": False,
        "owner_approval_verified": False,
        "pilot_ready": False,
        "provider_endpoint_effective_verified": False,
        "provider_internal_revision": "unknown",
        "provider_internal_revision_owner_accepted": True,
        "provider_profile_preflight_verified": True,
        "provider_request_count": 0,
        "request_capsule": request_capsule,
        "request_capsule_sha256": hashlib.sha256(
            _canonical(request_capsule)
        ).hexdigest(),
        "safe_to_dispatch": False,
        "status": "hermes_strict_no_send_preflight_verified_contract_only",
        "token_limits_effective_verified": False,
        "token_limits_preflight_bound": True,
        "tool_allowlist_preflight_verified": True,
        "tool_call_count": 0,
        "worker_runtime_verified": False,
    }
    value.update(overrides)
    return _canonical(value) + b"\n"


def _proof_input(h5_receipt: bytes) -> bytes:
    return _canonical(
        {
            "contract_version": "hermes.strict_runtime_guard.proof.input.v2",
            "expected_h5_receipt_sha256": hashlib.sha256(h5_receipt).hexdigest(),
            "h5_receipt_b64": base64.b64encode(h5_receipt).decode("ascii"),
        }
    )


def _event(name: str, **values: object) -> bytes:
    return _canonical({"contract_version": EVENT_VERSION, "event": name, **values})


def _binding(**overrides: object) -> bytes:
    capsule = _capsule()
    value: dict[str, object] = {
        "credential_material_present": False,
        "fanout": 0,
        "immutable_revision_claimed": False,
        "model_id": capsule["model_id"],
        "provider_id": capsule["provider_id"],
        "provider_internal_revision": capsule["provider_internal_revision"],
        "provider_internal_revision_owner_accepted": True,
        "repository_mount": False,
        "retry_count": 0,
        "tool_names": [],
    }
    value.update(overrides)
    return _event("bind_request", **value)


def _decision(guard: object, event: bytes) -> dict[str, object]:
    raw = guard(event)
    value = json.loads(raw)
    assert raw == _canonical(value)
    return value


class HermesStrictRuntimeGuardV2Tests(unittest.TestCase):
    def assert_hold(self, raw: bytes, code: str):
        receipt_raw = prove_strict_runtime_guard_v2(raw)
        receipt = json.loads(receipt_raw)
        self.assertEqual(receipt_raw, _canonical(receipt))
        self.assertEqual(receipt["status"], "hold_missing_or_invalid")
        self.assertIn(code, receipt["blocking_codes"])
        self.assertFalse(receipt["execution_authorized"])
        self.assertFalse(receipt["safe_to_dispatch"])
        self.assertEqual(receipt["job_count"], 0)
        self.assertEqual(receipt["model_call_count"], 0)
        self.assertEqual(receipt["provider_request_count"], 0)
        return receipt

    def test_fixed_proof_covers_all_representative_limits_and_stays_no_send(self):
        raw = prove_strict_runtime_guard_v2(_proof_input(_h5_receipt()))
        receipt = json.loads(raw)
        self.assertEqual(raw, _canonical(receipt))
        self.assertEqual(
            receipt["status"], "hermes_strict_runtime_guard_mechanics_verified_no_send"
        )
        self.assertTrue(receipt["guard_event_api_verified"])
        self.assertTrue(receipt["h5_preflight_receipt_verified"])
        self.assertTrue(receipt["runtime_guard_decision_mechanics_verified_no_send"])
        self.assertGreaterEqual(receipt["guard_probe_count"], 20)
        for expected in (
            "job_limit_exceeded",
            "attempt_limit_exceeded",
            "model_call_limit_exceeded",
            "provider_request_limit_exceeded",
            "input_token_limit_exceeded",
            "output_token_limit_exceeded",
            "output_bytes_limit_exceeded",
            "cost_limit_exceeded",
            "wall_clock_limit_exceeded",
            "retry_forbidden",
            "tool_call_forbidden",
            "repository_mount_forbidden",
            "credential_material_forbidden_in_no_send_proof",
        ):
            self.assertIn(expected, receipt["guard_probe_codes"])
        state = receipt["proof_only_guard_state"]
        self.assertEqual(state["job_reservations"], 1)
        self.assertEqual(state["attempt_reservations"], 1)
        self.assertEqual(state["model_call_reservations"], 1)
        self.assertEqual(state["provider_request_reservations"], 1)
        self.assertEqual(state["input_tokens_recorded"], 32_768)
        self.assertEqual(state["output_tokens_recorded"], 8_192)
        self.assertEqual(state["output_bytes_recorded"], 524_288)
        self.assertEqual(state["cost_usd_microdollars_recorded"], 250_000)
        self.assertEqual(state["wall_clock_seconds_observed"], 900)
        for key in (
            "actual_worker_runtime_verified",
            "credential_scope_effective_verified",
            "execution_authorized",
            "external_send",
            "h5_receipt_trusted_anchor_verified",
            "immutable_model_revision_effective_verified",
            "network_access",
            "pilot_ready",
            "provider_transport_effective_verified",
            "runtime_guard_integrated_with_worker",
            "safe_to_dispatch",
            "token_limits_effective_verified",
        ):
            self.assertIs(receipt[key], False, key)
        self.assertEqual(receipt["actual_output_bytes"], 0)
        self.assertEqual(receipt["actual_cost_usd_microdollars"], 0)

    def test_guard_limits_are_cumulative_and_terminal_hold_is_sticky(self):
        guard = new_strict_runtime_guard_v2(_h5_receipt())
        self.assertTrue(_decision(guard, _binding())["guard_event_applied"])
        self.assertTrue(
            _decision(guard, _event("record_output_bytes", count=524_288))[
                "guard_event_applied"
            ]
        )
        over = _decision(guard, _event("record_output_bytes", count=1))
        self.assertIn("output_bytes_limit_exceeded", over["blocking_codes"])
        self.assertEqual(over["guard_state"]["output_bytes_recorded"], 524_288)
        terminal = _decision(guard, _event("reserve_job"))
        self.assertIn("guard_terminal_hold", terminal["blocking_codes"])
        self.assertEqual(terminal["guard_state"]["job_reservations"], 0)

        clock_guard = new_strict_runtime_guard_v2(_h5_receipt())
        _decision(clock_guard, _binding())
        _decision(clock_guard, _event("observe_wall_clock", count=10))
        backwards = _decision(clock_guard, _event("observe_wall_clock", count=9))
        self.assertIn("wall_clock_not_monotonic", backwards["blocking_codes"])
        self.assertEqual(backwards["guard_state"]["wall_clock_seconds_observed"], 10)

    def test_binding_and_operation_negatives_fail_closed(self):
        cases = (
            ({"provider_id": "other"}, "requested_provider_id_mismatch"),
            ({"model_id": "other"}, "requested_model_id_mismatch"),
            ({"provider_internal_revision": "claimed"}, "requested_provider_internal_revision_mismatch"),
            ({"provider_internal_revision_owner_accepted": False}, "requested_provider_internal_revision_owner_accepted_mismatch"),
            ({"immutable_revision_claimed": True}, "requested_immutable_revision_claimed_mismatch"),
            ({"retry_count": 1}, "retry_forbidden"),
            ({"fanout": 1}, "fanout_forbidden"),
            ({"tool_names": ["terminal"]}, "tool_allowlist_violation"),
            ({"repository_mount": True}, "repository_mount_forbidden"),
            ({"credential_material_present": True}, "credential_material_forbidden_in_no_send_proof"),
        )
        for changes, code in cases:
            with self.subTest(code=code):
                guard = new_strict_runtime_guard_v2(_h5_receipt())
                decision = _decision(guard, _binding(**changes))
                self.assertEqual(decision["status"], "hold_missing_or_invalid")
                self.assertIn(code, decision["blocking_codes"])
                self.assertFalse(decision["guard_event_applied"])

        for event, code in (
            (_event("record_tool_call", tool_name="terminal"), "tool_call_forbidden"),
            (_event("mount_repository", mounted=True), "repository_mount_forbidden"),
            (
                _event(
                    "supply_credential_material",
                    credential_material_present=True,
                ),
                "credential_material_forbidden_in_no_send_proof",
            ),
            (_event("retry"), "retry_forbidden"),
        ):
            guard = new_strict_runtime_guard_v2(_h5_receipt())
            _decision(guard, _binding())
            decision = _decision(guard, event)
            self.assertIn(code, decision["blocking_codes"])

    def test_h5_receipt_transport_and_authority_are_bound(self):
        self.assert_hold(_proof_input(_h5_receipt()[:-1]), "h5_receipt_transport_invalid")
        self.assert_hold(
            _proof_input(_h5_receipt(execution_authorized=True)),
            "h5_execution_authorized_invalid",
        )
        self.assert_hold(
            _proof_input(_h5_receipt(request_capsule_sha256="4" * 64)),
            "h5_request_capsule_digest_mismatch",
        )

    def test_proof_input_is_canonical_and_bounded(self):
        valid = _proof_input(_h5_receipt())
        self.assert_hold(valid + b"\n", "proof_input_noncanonical")
        duplicate = valid[:-1] + b',"contract_version":"duplicate"}'
        self.assert_hold(duplicate, "json_duplicate_key")
        self.assert_hold(b"x" * (96 * 1024 + 1), "proof_input_size_invalid")


if __name__ == "__main__":
    unittest.main()
