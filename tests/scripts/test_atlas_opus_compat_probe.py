from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "atlas_opus_compat_probe.py"
SPEC = importlib.util.spec_from_file_location("atlas_opus_compat_probe", SCRIPT)
assert SPEC and SPEC.loader
PROBE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROBE
SPEC.loader.exec_module(PROBE)


def _decode_segment(value: str) -> dict:
    padded = value + "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_build_minimal_tools_preserves_dotted_names():
    tools = PROBE.build_minimal_tools(["platform.prompt_compile", "media.generate_image"])

    assert [tool["function"]["name"] for tool in tools] == [
        "platform.prompt_compile",
        "media.generate_image",
    ]
    assert tools[0]["function"]["parameters"]["additionalProperties"] is False


def test_parse_models_normalizes_csv():
    assert PROBE.parse_models(" opus-4.6, opus-4.7 ,, ") == ("opus-4.6", "opus-4.7")


def test_parse_sse_lines_reports_end_to_end_first_event(monkeypatch):
    monkeypatch.setattr(PROBE.time, "monotonic", lambda: 12.5)

    first_event, content, finish_reason = PROBE.parse_sse_lines(
        [
            "event: message",
            'data: {"choices":[{"delta":{"content":"MODEL_"}}]}',
            'data: {"choices":[{"delta":{"content":"TEST_OK"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ],
        started_at=10.0,
    )

    assert first_event == 2.5
    assert content == "MODEL_TEST_OK"
    assert finish_reason == "stop"


def test_runor_signer_issues_expected_scoped_claims():
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes_raw()
    signer = PROBE.RunOrSigner(seed)

    token = signer.issue(["run:read", "run:create", "run:read"])
    header, payload, signature = token.split(".")

    assert _decode_segment(header) == {"alg": "EdDSA", "typ": "JWT"}
    claims = _decode_segment(payload)
    assert claims["aud"] == "agent-orchestrator"
    assert claims["scopes"] == ["run:create", "run:read"]
    assert claims["principal"]["project_id"] == "opus-compat-probe"
    assert signature


def test_main_requires_explicit_live_confirmation(capsys):
    assert PROBE.main([]) == 2
    assert "--confirm-live" in capsys.readouterr().err
