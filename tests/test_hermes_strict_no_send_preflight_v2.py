"""Subprocess behavior tests for the v2 strict no-send preflight."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Callable

from hermes_strict_no_send_preflight_v2 import _implementation_graph_sha256


ENTRYPOINT = Path(__file__).resolve().parents[1] / "hermes_strict_no_send_preflight_v2.py"


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


def _environment_document(home: Path) -> bytes:
    return _canonical(
        {
            "contract_version": "hermes.clean_environment.canonical_bytes.v2",
            "environment": {
                "HOME": str(home),
                "LANG": "C.UTF-8",
                "TZ": "UTC",
            },
        }
    )


def _envelope(home: Path, graph_sha256: str | None = None) -> bytes:
    return _canonical(
        {
            "candidate_document_b64": base64.b64encode(VALID_CANDIDATE).decode("ascii"),
            "contract_version": "hermes.strict_no_send_preflight.input.v2",
            "environment_document_b64": base64.b64encode(
                _environment_document(home)
            ).decode("ascii"),
            "expected_implementation_graph_sha256": graph_sha256
            or _implementation_graph_sha256()[0],
        }
    )


def _invoke(
    raw: bytes | Callable[[Path], bytes],
    *,
    extra_environment: dict[str, str] | None = None,
    arguments: tuple[str, ...] = (),
    python_code: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryDirectory(prefix="hermes-h5-v2-unit-") as temporary:
        root = Path(os.path.realpath(temporary))
        home = root / "home"
        home.mkdir()
        (home / ".hermes").mkdir()
        outside = root / "outside"
        outside.mkdir()
        environment = {
            "HOME": str(home),
            "HERMES_HOME": str(home / ".hermes"),
            "LANG": "C.UTF-8",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
        }
        if extra_environment:
            environment.update(extra_environment)
        input_bytes = raw(home) if callable(raw) else raw
        command = (
            [sys.executable, "-c", python_code]
            if python_code is not None
            else [sys.executable, str(ENTRYPOINT), *arguments]
        )
        return subprocess.run(
            command,
            cwd=outside,
            env=environment,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=30,
        )


class HermesStrictNoSendPreflightV2Tests(unittest.TestCase):
    def assert_hold(self, raw: bytes, code: str, **kwargs: object):
        result = _invoke(raw, **kwargs)
        self.assertEqual(result.returncode, 64, result.stderr)
        self.assertEqual(result.stderr, b"")
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "hold_missing_or_invalid")
        self.assertIn(code, receipt["blocking_codes"])
        self.assertFalse(receipt["execution_authorized"])
        self.assertFalse(receipt["external_send"])
        self.assertFalse(receipt["network_access"])
        self.assertFalse(receipt["safe_to_dispatch"])
        self.assertEqual(receipt["job_count"], 0)
        self.assertEqual(receipt["model_call_count"], 0)
        self.assertEqual(receipt["provider_request_count"], 0)
        self.assertEqual(receipt["tool_call_count"], 0)
        self.assertEqual(receipt["actual_cost_usd_microdollars"], 0)
        self.assertEqual(receipt["actual_output_bytes"], 0)
        return receipt

    def test_installed_resolver_surfaces_produce_contract_only_receipt(self):
        with tempfile.TemporaryDirectory(prefix="hermes-h5-v2-valid-") as temporary:
            root = Path(os.path.realpath(temporary))
            home = root / "home"
            home.mkdir()
            (home / ".hermes").mkdir()
            result = _invoke(lambda invocation_home: _envelope(invocation_home))
        self.assertEqual(
            result.returncode,
            0,
            result.stderr + b" stdout=" + result.stdout,
        )
        self.assertEqual(result.stderr, b"")
        self.assertTrue(result.stdout.endswith(b"\n"))
        receipt = json.loads(result.stdout)
        self.assertEqual(result.stdout, _canonical(receipt) + b"\n")
        self.assertEqual(
            receipt["status"], "hermes_strict_no_send_preflight_verified_contract_only"
        )
        self.assertEqual(receipt["implementation_graph_sha256"], _implementation_graph_sha256()[0])
        self.assertGreaterEqual(receipt["implementation_graph_file_count"], 1)
        self.assertEqual(
            receipt["credential_environment_names"],
            ["GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"],
        )
        capsule = receipt["request_capsule"]
        self.assertEqual(capsule["provider_id"], "zai")
        self.assertEqual(capsule["model_id"], "glm-5.2")
        self.assertEqual(capsule["provider_internal_revision"], "unknown")
        self.assertTrue(capsule["provider_internal_revision_owner_accepted"])
        self.assertFalse(capsule["immutable_revision_claimed"])
        self.assertEqual(capsule["max_input_tokens"], 32_768)
        self.assertEqual(capsule["max_output_tokens"], 8_192)
        self.assertEqual(capsule["max_total_tokens"], 40_960)
        self.assertEqual(capsule["max_output_bytes"], 524_288)
        self.assertEqual(capsule["max_cost_usd_microdollars"], 250_000)
        self.assertEqual(capsule["wall_clock_seconds"], 900)
        self.assertEqual(capsule["tool_names"], [])
        self.assertFalse(capsule["repository_mount"])
        self.assertEqual(capsule["retry_count"], 0)
        self.assertTrue(receipt["h4_candidate_input_verified"])
        self.assertTrue(receipt["h4_environment_input_verified"])
        for key in (
            "execution_authorized",
            "external_send",
            "external_dependency_graph_verified",
            "filesystem_mutation_effective_verified",
            "host_containment_verified",
            "local_implementation_graph_trusted_anchor_verified",
            "model_revision_immutable_verified",
            "network_access",
            "owner_approval_verified",
            "pilot_ready",
            "provider_endpoint_effective_verified",
            "safe_to_dispatch",
            "token_limits_effective_verified",
            "worker_runtime_verified",
        ):
            self.assertIs(receipt[key], False, key)

    def test_credential_names_are_rejected_without_value_access(self):
        with tempfile.TemporaryDirectory(prefix="hermes-h5-v2-credential-") as temporary:
            root = Path(os.path.realpath(temporary))
            home = root / "home"
            home.mkdir()
            (home / ".hermes").mkdir()
            receipt = self.assert_hold(
                _envelope(home),
                "process_environment_key_forbidden",
                extra_environment={"ZAI_API_KEY": "must-not-be-read"},
            )
        self.assertEqual(receipt["credential_environment_names"], [])
        self.assertIsNone(receipt["request_capsule"])

    def test_graph_mismatch_and_environment_determinism_are_typed_holds(self):
        with tempfile.TemporaryDirectory(prefix="hermes-h5-v2-holds-") as temporary:
            root = Path(os.path.realpath(temporary))
            home = root / "home"
            home.mkdir()
            (home / ".hermes").mkdir()
            self.assert_hold(_envelope(home, "sha256:" + "0" * 64), "implementation_graph_mismatch")
            for key, value in (
                ("PYTHONDONTWRITEBYTECODE", "0"),
                ("PYTHONHASHSEED", "random"),
                ("TZ", "Asia/Tokyo"),
            ):
                with self.subTest(key=key):
                    self.assert_hold(
                        _envelope(home),
                        "process_environment_value_invalid",
                        extra_environment={key: value},
                    )

    def test_input_and_arguments_are_rejected_before_resolution(self):
        with tempfile.TemporaryDirectory(prefix="hermes-h5-v2-input-") as temporary:
            root = Path(os.path.realpath(temporary))
            home = root / "home"
            home.mkdir()
            (home / ".hermes").mkdir()
            self.assert_hold(_envelope(home) + b"\n", "input_noncanonical")
        result = _invoke(b"", arguments=("--dry-run",))
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(
            result.stderr,
            b"usage: hermes-strict-no-send-preflight-v2 < canonical-envelope.json\n",
        )

    def test_preloaded_ordinary_runtime_is_a_typed_and_disclosed_hold(self):
        code = (
            "import sys,types\n"
            f"sys.path.insert(0,{str(ENTRYPOINT.parent)!r})\n"
            "sys.modules['hermes_cli.config']=types.ModuleType('hermes_cli.config')\n"
            "from hermes_strict_no_send_preflight_v2 import main\n"
            "raise SystemExit(main())\n"
        )
        receipt = self.assert_hold(
            lambda home: _envelope(home),
            "ordinary_runtime_imported",
            python_code=code,
        )
        self.assertTrue(receipt["ordinary_runtime_imported"])


if __name__ == "__main__":
    unittest.main()
