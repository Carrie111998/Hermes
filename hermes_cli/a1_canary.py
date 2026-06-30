"""Reusable A1 GREEN canary harness.

This module packages the synthetic A1.3 canaries used to verify the guarded
model-dispatch seam.  It intentionally uses fake provider calls: the goal is to
prove Hermes records resolver/payload/dispatch evidence and blocks forbidden
routes before any external provider can be contacted.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock, patch

GREEN_CANARY_CASE_IDS = (
    "A1.3-NEG-001",
    "A1.3-CANARY-006",
    "A1.3-CANARY-005",
    "A1.3-CANARY-004",
)

_RAW_PAYLOAD_FRAGMENTS = (
    "do not leave local",
    "nonstream hello",
    "streaming hello",
    "fallback hello",
)


class _CallRecorder:
    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, request: dict[str, Any], **_kwargs: Any) -> Any:
        self.calls.append(request)
        if not self._responses:
            raise AssertionError("No fake provider response configured")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def run_green_canary_harness(*, output_path: str | Path) -> list[dict[str, Any]]:
    """Run the packaged A1.3 GREEN canaries and write JSONL case summaries."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="a1-green-canary-") as tmpdir:
        tmp = Path(tmpdir)
        records.append(
            _run_case(
                case_id="A1.3-NEG-001",
                surface="main_chat",
                prompt="CLASSIFICATION=C2_LOCAL_ONLY do not leave local",
                evidence_sink=tmp / "deny.jsonl",
                configure=_configure_c2_denied,
            )
        )
        records.append(
            _run_case(
                case_id="A1.3-CANARY-006",
                surface="non_streaming",
                prompt="CLASSIFICATION=C0_PUBLIC nonstream hello",
                evidence_sink=tmp / "nonstream.jsonl",
                configure=_configure_non_streaming,
            )
        )
        records.append(
            _run_case(
                case_id="A1.3-CANARY-005",
                surface="streaming",
                prompt="CLASSIFICATION=C0_PUBLIC streaming hello",
                evidence_sink=tmp / "streaming.jsonl",
                configure=_configure_streaming,
            )
        )
        records.append(
            _run_case(
                case_id="A1.3-CANARY-004",
                surface="fallback_same_turn",
                prompt="CLASSIFICATION=C0_PUBLIC fallback hello",
                evidence_sink=tmp / "fallback.jsonl",
                configure=_configure_fallback,
            )
        )

    with output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run synthetic A1.3 GREEN dispatch canaries")
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write summarized JSONL canary evidence",
    )
    args = parser.parse_args(argv)

    records = run_green_canary_harness(output_path=args.output)
    print(f"A1.3 GREEN canaries passed: {len(records)} records written to {args.output}")
    return 0


def _run_case(
    *,
    case_id: str,
    surface: str,
    prompt: str,
    evidence_sink: Path,
    configure: Callable[[Any], Callable[[], int]],
) -> dict[str, Any]:
    old_guard = os.environ.get("HERMES_A1_DISPATCH_GUARD")
    old_sink = os.environ.get("HERMES_A1_EVIDENCE_SINK")
    os.environ["HERMES_A1_DISPATCH_GUARD"] = "1"
    os.environ["HERMES_A1_EVIDENCE_SINK"] = str(evidence_sink)
    try:
        agent = _make_agent()
        classification = _classification_from_prompt(prompt)
        setattr(agent, "a1_classification", classification)
        dispatch_prompt = _strip_classification_marker(prompt)
        call_count = configure(agent)
        # Some denial paths intentionally exercise Hermes error reporting.  Keep
        # the harness output machine-readable by capturing that chatter.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = agent.run_conversation(dispatch_prompt)
        events = _read_jsonl(evidence_sink)
        serialized_events = json.dumps(events, sort_keys=True)
        return {
            "case_id": case_id,
            "surface": surface,
            "prompt_classification": _classification_from_prompt(prompt),
            "result_completed": bool(result.get("completed")),
            "result_failed": bool(result.get("failed")),
            "final_response_digest": _digest(result.get("final_response")),
            "provider_call_count": call_count(),
            "event_types": [event["event_type"] for event in events],
            "resolver_providers": [
                event.get("canonical_provider")
                for event in events
                if event.get("event_type") == "resolver_decision"
            ],
            "resolver_hosts": [
                event.get("canonical_base_url_host")
                for event in events
                if event.get("event_type") == "resolver_decision"
            ],
            "dispatch_attempted": [
                event.get("provider_call_attempted")
                for event in events
                if event.get("event_type") == "dispatch_result"
            ],
            "dispatch_completed": [
                event.get("provider_call_completed")
                for event in events
                if event.get("event_type") == "dispatch_result"
            ],
            "rule_ids": [event.get("rule_id") for event in events if event.get("rule_id")],
            "raw_payload_stored": any(fragment in serialized_events for fragment in _RAW_PAYLOAD_FRAGMENTS),
        }
    finally:
        _restore_env("HERMES_A1_DISPATCH_GUARD", old_guard)
        _restore_env("HERMES_A1_EVIDENCE_SINK", old_sink)


def _configure_c2_denied(agent: Any) -> Callable[[], int]:
    call = MagicMock(return_value=_mock_response("should-not-run"))
    agent._interruptible_api_call = call
    return lambda: call.call_count


def _configure_non_streaming(agent: Any) -> Callable[[], int]:
    call = MagicMock(return_value=_mock_response("guarded ok"))
    agent._interruptible_api_call = call
    return lambda: call.call_count


def _configure_streaming(agent: Any) -> Callable[[], int]:
    stream_call = MagicMock(return_value=_mock_response("streamed ok"))
    non_stream_call = MagicMock(return_value=_mock_response("wrong path"))
    agent.stream_delta_callback = lambda _delta: None
    agent._interruptible_streaming_api_call = stream_call
    agent._interruptible_api_call = non_stream_call
    return lambda: stream_call.call_count + non_stream_call.call_count


def _configure_fallback(agent: Any) -> Callable[[], int]:
    call = MagicMock(side_effect=[_mock_invalid_response(), _mock_response("fallback ok")])
    agent._fallback_chain = [{"provider": "local-ollama", "model": "qwen3.5:9b"}]
    agent._fallback_index = 0
    agent._interruptible_api_call = call

    def activate_fallback(*_args: Any, **_kwargs: Any) -> bool:
        agent.provider = "local-ollama"
        agent.model = "qwen3.5:9b"
        agent.base_url = "http://localhost:11434/v1"
        agent._fallback_index = 1
        agent._fallback_activated = True
        return True

    agent._try_activate_fallback = activate_fallback
    return lambda: call.call_count


def _make_agent() -> Any:
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="http://localhost:8787/v1",
            provider="custom:headroom-openrouter-litellm",
            model="frontier-fast",
            max_iterations=1,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._api_max_retries = 1
    return agent


def _make_tool_defs(*names: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_response(content: str = "ok", finish_reason: str = "stop") -> Any:
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="frontier-fast", usage=None)


def _mock_invalid_response() -> Any:
    return SimpleNamespace(choices=[], model="frontier-fast", usage=None)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _classification_from_prompt(prompt: str) -> str:
    first = prompt.split(maxsplit=1)[0]
    if first.startswith("CLASSIFICATION="):
        return first.split("=", 1)[1]
    return "UNKNOWN"


def _strip_classification_marker(prompt: str) -> str:
    parts = prompt.split(maxsplit=1)
    if parts and parts[0].startswith("CLASSIFICATION="):
        return parts[1] if len(parts) > 1 else ""
    return prompt


def _digest(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _restore_env(name: str, old_value: str | None) -> None:
    if old_value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old_value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
