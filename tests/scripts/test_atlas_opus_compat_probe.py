from __future__ import annotations

import http.client
import importlib.util
import json
import sys
from email.message import Message
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "atlas_opus_compat_probe.py"
SPEC = importlib.util.spec_from_file_location("atlas_opus_compat_probe", SCRIPT)
assert SPEC and SPEC.loader
PROBE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROBE
SPEC.loader.exec_module(PROBE)


def test_embedded_full_profile_is_complete_and_checksum_verified():
    profile = PROBE.load_embedded_profile()
    payload = PROBE.build_payloads((PROBE.FULL_STAGE,))[0]

    assert PROBE.PROFILE_SHA256 == (
        "dc4db941ab622e7b06fd93aaa8678c5b81cc4fb69225a0149c026e3752044dc3"
    )
    assert payload.system_chars == 28_780
    assert payload.tool_schema_bytes == 6_569
    assert [tool["function"]["name"] for tool in profile["tools"]] == list(
        PROBE.TOOL_NAMES
    )


def test_build_minimal_tools_preserves_dotted_names():
    tools = PROBE.build_minimal_tools(
        ["platform.prompt_compile", "media.generate_image"]
    )

    assert [tool["function"]["name"] for tool in tools] == [
        "platform.prompt_compile",
        "media.generate_image",
    ]
    assert tools[0]["function"]["parameters"]["additionalProperties"] is False


def test_build_payloads_covers_all_stages():
    payloads = PROBE.build_payloads(PROBE.ALL_STAGES)

    assert [payload.stage for payload in payloads] == list(PROBE.ALL_STAGES)
    assert payloads[0].tools is None
    assert payloads[1].tools
    assert payloads[2].system_chars == 28_780


def test_parse_csv_and_resolve_stages():
    assert PROBE.parse_csv(" opus-4.6, opus-4.7 ,, ") == (
        "opus-4.6",
        "opus-4.7",
    )
    assert PROBE.resolve_stages(None, full=True) == (
        "direct_stream",
        "minimal_tools",
        "full_agent_context",
    )
    with pytest.raises(Exception, match="unknown stages"):
        PROBE.resolve_stages(("not-a-stage",), full=False)


def test_parse_sse_lines_reports_ids_and_first_event(monkeypatch):
    monkeypatch.setattr(PROBE.time, "monotonic", lambda: 12.5)

    first_event, event_id, content, finish_reason = PROBE.parse_sse_lines(
        [
            b'data: {"id":"evt_1","choices":[{"delta":{"content":"MODEL_"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"TEST_OK"},"finish_reason":"stop"}]}\n',
            b"data: [DONE]\n",
        ],
        started_at=10.0,
    )

    assert first_event == 2.5
    assert event_id == "evt_1"
    assert content == "MODEL_TEST_OK"
    assert finish_reason == "stop"


def test_parse_env_file_without_dotenv_dependency(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nATLAS_API_KEY='secret-value'\nOTHER=value\n",
        encoding="utf-8",
    )

    assert PROBE.parse_env_file(env_file) == {
        "ATLAS_API_KEY": "secret-value",
        "OTHER": "value",
    }


class _FakeResponse:
    status = 200

    def __init__(self):
        self.headers = Message()
        self.headers["x-request-id"] = "atlas_123"
        self.headers["cf-ray"] = "ray_123"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def __iter__(self):
        return iter(
            [
                b'data: {"id":"evt_123","choices":[{"delta":{"content":"MODEL_TEST_OK"},"finish_reason":"stop"}]}\n',
                b"data: [DONE]\n",
            ]
        )

    def getcode(self):
        return self.status


def test_probe_captures_atlas_and_event_request_ids(monkeypatch):
    def fake_urlopen(request, *, timeout):
        assert timeout == 135
        assert request.full_url.endswith("/chat/completions")
        assert request.headers["X-request-id"].startswith("opus-probe-")
        body = json.loads(request.data)
        assert body["model"] == "anthropic/claude-opus-4.8"
        return _FakeResponse()

    monkeypatch.setattr(PROBE.urllib.request, "urlopen", fake_urlopen)
    payload = PROBE.build_payloads(("direct_stream",))[0]

    result = PROBE.probe(
        api_key="not-a-real-key",
        base_url="https://example.invalid/v1",
        model="anthropic/claude-opus-4.8",
        timeout_seconds=135,
        payload=payload,
    )

    assert result.ok is True
    assert result.atlas_request_id == "atlas_123"
    assert result.client_request_id.startswith("opus-probe-")
    assert result.event_id == "evt_123"
    assert result.content == "MODEL_TEST_OK"


def test_probe_keeps_client_id_when_disconnect_precedes_headers(monkeypatch):
    def broken_urlopen(_request, *, timeout):
        assert timeout == 135
        raise http.client.RemoteDisconnected(
            "Remote end closed connection without response"
        )

    monkeypatch.setattr(PROBE.urllib.request, "urlopen", broken_urlopen)
    payload = PROBE.build_payloads(("direct_stream",))[0]

    result = PROBE.probe(
        api_key="not-a-real-key",
        base_url="https://example.invalid/v1",
        model="anthropic/claude-opus-4.7",
        timeout_seconds=135,
        payload=payload,
    )

    assert result.ok is False
    assert result.atlas_request_id == ""
    assert result.client_request_id.startswith("opus-probe-")
    assert "RemoteDisconnected" in result.error


def test_write_results_serializes_request_ids(tmp_path):
    output = tmp_path / "nested" / "results.json"
    result = PROBE.ProbeResult(
        stage="direct_stream",
        model="opus",
        ok=True,
        started_at="start",
        ended_at="end",
        elapsed_seconds=1,
        client_request_id="client_1",
        atlas_request_id="atlas_1",
    )

    PROBE.write_results(output, [result])

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved[0]["client_request_id"] == "client_1"
    assert saved[0]["atlas_request_id"] == "atlas_1"


def test_main_requires_explicit_live_confirmation(capsys):
    assert PROBE.main([]) == 2
    assert "--confirm-live" in capsys.readouterr().err
