#!/usr/bin/env python3
"""Guarded live verification for Hermes's Claude subscription provider."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from agent.claude_cli_client import ClaudeCLIClient
from agent.claude_cli_process import ClaudeCLIProcessRunner
from hermes_state import SessionDB


_ECHO_TOOL = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Hermes-owned verification tool that returns text unchanged.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}


def _tool_call_dict(call) -> dict:
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.function.name,
            "arguments": call.function.arguments,
        },
    }


def run_verification(*, model: str = "opus", executable: str = "claude") -> dict:
    runner = ClaudeCLIProcessRunner(executable=executable, timeout_seconds=600)
    auth = runner.auth_status()
    version = runner.version()

    with tempfile.TemporaryDirectory(prefix="hermes-claude-cli-live-") as tmp:
        db = SessionDB(Path(tmp) / "state.db")
        hermes_session_id = "claude-cli-live-verification"
        db.create_session(hermes_session_id, "verification", model=model)
        client = ClaudeCLIClient(
            model=model,
            session_db=db,
            session_id=hermes_session_id,
            runner=runner,
        )
        tools = [_ECHO_TOOL]
        messages = [
            {
                "role": "system",
                "content": (
                    "This is a bounded Hermes transport verification. Follow "
                    "the user's requested literal outputs exactly."
                ),
            },
            {
                "role": "user",
                "content": "Reply with exactly HERMES_CLAUDE_CLI_OK",
            },
        ]
        first = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
        )
        exact = first.choices[0].message.content
        if exact != "HERMES_CLAUDE_CLI_OK":
            raise RuntimeError(f"Unexpected exact-response result: {exact!r}")
        initial = db.get_provider_attachment(hermes_session_id, "claude-cli")

        messages.extend(
            [
                {"role": "assistant", "content": exact},
                {
                    "role": "user",
                    "content": (
                        "Request the echo tool once with text HERMES_TOOL_OK. "
                        "Do not return a final answer before its result."
                    ),
                },
            ]
        )
        tool_decision = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
        )
        calls = tool_decision.choices[0].message.tool_calls or []
        if len(calls) != 1 or calls[0].function.name != "echo":
            raise RuntimeError("Claude did not request the Hermes-owned echo tool")
        arguments = json.loads(calls[0].function.arguments)
        if arguments != {"text": "HERMES_TOOL_OK"}:
            raise RuntimeError(f"Unexpected echo arguments: {arguments!r}")

        # Hermes owns execution. The verifier performs the harmless echo
        # locally and sends only the result back into the provider session.
        tool_result = arguments["text"]
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [_tool_call_dict(calls[0])],
                },
                {
                    "role": "tool",
                    "tool_call_id": calls[0].id,
                    "name": "echo",
                    "content": tool_result,
                },
            ]
        )
        final = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
        )
        if tool_result not in (final.choices[0].message.content or ""):
            raise RuntimeError("Claude did not consume the Hermes tool result")

        current = db.get_provider_attachment(hermes_session_id, "claude-cli")
        resumed = bool(
            initial
            and current
            and initial["provider_session_id"] == current["provider_session_id"]
        )
        if not resumed:
            raise RuntimeError("Claude provider session was not resumed")

        client.close()
        return {
            "exact_response": exact,
            "tool_result": tool_result,
            "resumed": resumed,
            "provider": "claude-cli",
            "model_requested": model,
            "model_reported": current.get("model_reported", ""),
            "subscription_type": auth.get("subscriptionType", ""),
            "auth_method": auth.get("authMethod", ""),
            "api_provider": auth.get("apiProvider", ""),
            "version": version,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="opus")
    parser.add_argument("--executable", default="claude")
    args = parser.parse_args()
    result = run_verification(model=args.model, executable=args.executable)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
