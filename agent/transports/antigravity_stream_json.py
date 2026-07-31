"""Structured Antigravity CLI stream-json transport primitives.

The normal ``agy -p`` text mode is not sufficient for Hermes: it collapses
model output, tool execution, checkpoints, and errors into one unverified
string.  This module pins the stream-json event contract before the process
lifecycle/session adapter is added.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class AntigravityTurnResult:
    """Structured result for one Antigravity stream-json turn."""

    final_text: str = ""
    projected_messages: list[dict[str, Any]] = field(default_factory=list)
    tool_iterations: int = 0
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_events: list[dict[str, Any]] = field(default_factory=list)
    conversation_id: str | None = None
    usage: dict[str, Any] | None = None
    completed: bool = False
    error: str | None = None


def build_agy_stream_command(
    prompt: str,
    *,
    conversation_id: str | None = None,
    model: str | None = None,
    print_timeout: str = "600s",
    agy_bin: str = "agy",
) -> list[str]:
    """Build an ``agy`` stream-json command with options before the prompt."""

    command = [
        agy_bin,
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
        "--print-timeout",
        print_timeout,
    ]
    if model:
        command.extend(["--model", model])
    if conversation_id:
        command.extend(["--conversation", conversation_id])
    command.extend(["-p", prompt])
    return command


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _tool_call_id(conversation_id: str | None, step_index: Any) -> str:
    prefix = conversation_id or "turn"
    return f"agy-{prefix}-{step_index}"


def _project_tool_update(
    result: AntigravityTurnResult,
    step_update: dict[str, Any],
) -> None:
    step_type = step_update.get("step_type") or ""
    if step_type == "checkpoint":
        result.checkpoint_events.append(dict(step_update))
        return
    if step_type != "tool":
        return

    state = step_update.get("state") or ""
    if state != "DONE":
        return

    tool_info = step_update.get("tool_info") or {}
    if not isinstance(tool_info, dict):
        tool_info = {}
    tool_name = step_update.get("tool_name") or tool_info.get("name") or "unknown"
    step_index = step_update.get("step_index", len(result.tool_events))
    parameters = tool_info.get("parameters") or {}
    output = tool_info.get("output")
    if output is None:
        output = step_update.get("output", "")
    call_id = _tool_call_id(result.conversation_id, step_index)

    event = dict(step_update)
    event["tool_name"] = tool_name
    event["tool_call_id"] = call_id
    event["parameters"] = parameters
    event["output"] = output
    result.tool_events.append(event)
    result.tool_iterations += 1

    if not isinstance(parameters, dict):
        parameters = {"parameters": parameters}
    result.projected_messages.extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": str(tool_name),
                            "arguments": json.dumps(parameters, ensure_ascii=False),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _as_text(output),
            },
        ]
    )


def parse_stream_events(events: Iterable[str | dict[str, Any]]) -> AntigravityTurnResult:
    """Parse Antigravity stream-json events into Hermes-safe evidence.

    A successful-looking assistant string is never sufficient.  The parser
    requires a terminal ``result`` event with ``status == SUCCESS``.  Missing,
    malformed, or failed terminal events remain incomplete and carry an error.
    """

    result = AntigravityTurnResult()
    text_deltas: list[str] = []
    terminal: dict[str, Any] | None = None
    parse_error: str | None = None

    for raw_event in events:
        if isinstance(raw_event, dict):
            event = raw_event
        else:
            line = str(raw_event).strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_error = f"invalid Antigravity stream JSON: {exc.msg}"
                continue
        if not isinstance(event, dict):
            parse_error = "invalid Antigravity stream event: expected object"
            continue

        event_name = event.get("event")
        if event_name == "init":
            result.conversation_id = event.get("conversation_id") or result.conversation_id
            continue
        if event_name == "step_update":
            step_update = event.get("step_update") or {}
            if isinstance(step_update, dict):
                _project_tool_update(result, step_update)
                if step_update.get("step_type") == "agent_response":
                    delta = step_update.get("text_delta") or ""
                    if delta:
                        text_deltas.append(str(delta))
            continue
        if event_name == "result":
            terminal = event.get("result") or {}
            if isinstance(terminal, dict):
                result.conversation_id = (
                    terminal.get("conversation_id") or result.conversation_id
                )
                if isinstance(terminal.get("usage"), dict):
                    result.usage = terminal["usage"]
                result.final_text = str(terminal.get("response") or "")
            continue
        if event_name == "error":
            parse_error = _as_text(event.get("error") or event)

    if not result.final_text and text_deltas:
        result.final_text = "".join(text_deltas)

    if parse_error:
        result.error = parse_error
        return result
    if terminal is None:
        result.error = "Antigravity stream ended without a terminal result"
        return result

    status = str(terminal.get("status") or "UNKNOWN")
    if status != "SUCCESS":
        result.error = f"Antigravity terminal status={status}"
        return result

    result.completed = True
    return result


class AntigravityStreamJsonSession:
    """Conversation-preserving adapter over ``agy --output-format stream-json``.

    ``agy`` is currently a one-shot process surface, so each turn gets a fresh
    process and resumes the Antigravity conversation with ``--conversation``.
    The conversation id comes from the structured ``init``/``result`` event;
    stdout text is never treated as success without a terminal result event.
    """

    def __init__(
        self,
        *,
        cwd: str | None = None,
        agy_bin: str = "agy",
        model: str | None = None,
        print_timeout: str = "600s",
        process_factory: Any | None = None,
        on_event: Any | None = None,
    ) -> None:
        self._cwd = cwd
        self._agy_bin = agy_bin
        self._model = model
        self._print_timeout = print_timeout
        self._process_factory = process_factory
        self._on_event = on_event
        self._conversation_id: str | None = None
        self._closed = False

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    def close(self) -> None:
        """Retire this adapter; future calls must not silently resume it."""
        self._closed = True
        self._conversation_id = None

    def _first_turn_prompt(self, user_input: str, system_prompt: str | None) -> str:
        if not system_prompt:
            return user_input
        return (
            "[HERMES_SYSTEM_PROMPT]\n"
            f"{system_prompt}\n"
            "[/HERMES_SYSTEM_PROMPT]\n\n"
            "[HERMES_USER_MESSAGE]\n"
            f"{user_input}\n"
            "[/HERMES_USER_MESSAGE]"
        )

    def run_turn(
        self,
        user_input: str,
        *,
        system_prompt: str | None = None,
        turn_timeout: float = 600.0,
    ) -> AntigravityTurnResult:
        """Execute one structured turn and return only verified terminal state."""
        if self._closed:
            return AntigravityTurnResult(error="Antigravity stream session is closed")

        prompt = (
            self._first_turn_prompt(user_input, system_prompt)
            if self._conversation_id is None
            else user_input
        )
        command = build_agy_stream_command(
            prompt,
            conversation_id=self._conversation_id,
            model=self._model,
            print_timeout=self._print_timeout,
            agy_bin=self._agy_bin,
        )

        process: Any = None
        try:
            if self._process_factory is None:
                from tools.environments.local import hermes_subprocess_env

                env = hermes_subprocess_env(inherit_credentials=False)
                process_factory = subprocess.Popen
            else:
                env = None
                process_factory = self._process_factory
            process = process_factory(
                command,
                cwd=self._cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            stdout, stderr = process.communicate(timeout=turn_timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
            return AntigravityTurnResult(
                error=f"Antigravity stream turn timed out after {turn_timeout:.0f}s"
            )
        except Exception as exc:
            return AntigravityTurnResult(error=f"Antigravity stream launch failed: {exc}")

        raw_lines = str(stdout or "").splitlines()
        if self._on_event is not None:
            for raw_line in raw_lines:
                try:
                    event = json.loads(raw_line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    try:
                        self._on_event(event)
                    except Exception:
                        # Display callbacks must never turn a valid provider
                        # result into a failed model turn.
                        pass

        result = parse_stream_events(raw_lines)
        if result.conversation_id:
            self._conversation_id = result.conversation_id

        returncode = getattr(process, "returncode", 0)
        if returncode != 0:
            diagnostic = str(stderr or "").strip()
            result.completed = False
            result.error = (
                f"Antigravity stream exited with code {returncode}"
                + (f": {diagnostic[-2000:]}" if diagnostic else "")
            )
        return result
