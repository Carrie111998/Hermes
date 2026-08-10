"""Persistent, text-only transport for the official Claude CLI.

This module deliberately delegates authentication, subscription billing, session
state, hooks, and managed policy to the installed ``claude`` executable. Hermes
never reads Claude credentials and never speaks to an Anthropic HTTP endpoint on
this path.

Stage 0 is intentionally narrow: one exact model, no tools, no vision, no MCP,
no reasoning projection, and no native cancellation/steering protocol.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse, Usage
from tools.environments.local import hermes_subprocess_env


CLAUDE_CLI_MODEL = "claude-opus-5"
CLAUDE_CLI_MIN_VERSION = (2, 1, 221)
CLAUDE_CLI_EXECUTABLE = "claude"
CLAUDE_CLI_REQUIRED_FLAGS = frozenset(
    {
        "--input-format",
        "--output-format",
        "--include-partial-messages",
        "--replay-user-messages",
        "--model",
        "--tools",
        "--verbose",
        "--strict-mcp-config",
    }
)
CLAUDE_CLI_FORBIDDEN_FLAGS = frozenset(
    {"--bare", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}
)
CLAUDE_CLI_ENV_BLOCKLIST = frozenset(
    {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"}
)
_DEFAULT_POPEN = subprocess.Popen


def build_claude_cli_argv(model: str = CLAUDE_CLI_MODEL) -> list[str]:
    """Return the only Stage-0 command line Hermes is allowed to spawn."""
    if model != CLAUDE_CLI_MODEL:
        raise ValueError(
            f"claude-cli Stage 0 supports only model {CLAUDE_CLI_MODEL!r}"
        )
    argv = [
        CLAUDE_CLI_EXECUTABLE,
        "-p",
        "--verbose",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--replay-user-messages",
        "--model",
        CLAUDE_CLI_MODEL,
        "--tools",
        "",
        "--strict-mcp-config",
    ]
    if CLAUDE_CLI_FORBIDDEN_FLAGS.intersection(argv):  # pragma: no cover
        raise AssertionError("forbidden Claude CLI flag in fixed argv")
    return argv


def build_claude_cli_env(
    source_env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Build the official CLI child environment at its managed-policy boundary.

    All inherited ``ANTHROPIC_*`` request shaping and the explicitly blocked
    Claude CLI backend selectors are removed. ``HOME``, ``USERPROFILE``, and
    ``CLAUDE_CODE_OAUTH_TOKEN`` are deliberately preserved so the official CLI
    can use its own login/keychain and managed policy. Values are never
    inspected or logged.
    """
    if source_env is None:
        env = hermes_subprocess_env(inherit_credentials=False)
    else:
        env = dict(source_env)
    for name in tuple(env):
        normalized = name.upper()
        if normalized.startswith("ANTHROPIC_") or normalized in CLAUDE_CLI_ENV_BLOCKLIST:
            env.pop(name, None)
    return env


def parse_claude_cli_version(output: str) -> Optional[tuple[int, int, int]]:
    match = re.search(r"(?:^|\s)(\d+)\.(\d+)\.(\d+)(?:\s|$)", output or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


@dataclass(frozen=True)
class ClaudeCLICapabilities:
    stream_json_input: bool
    stream_json_output: bool
    partial_messages: bool
    replay_user_messages: bool
    model_selection: bool
    tools_selection: bool
    verbose: bool
    strict_mcp_config: bool
    persistent_session: bool = True
    zero_tools: bool = True
    cancellation: bool = False
    native_steer: bool = False
    vision: bool = False
    reasoning: bool = False

    @property
    def stage0_ready(self) -> bool:
        return all(
            (
                self.stream_json_input,
                self.stream_json_output,
                self.partial_messages,
                self.replay_user_messages,
                self.model_selection,
                self.tools_selection,
                self.verbose,
                self.strict_mcp_config,
                self.persistent_session,
                self.zero_tools,
            )
        )


def parse_claude_cli_help(output: str) -> ClaudeCLICapabilities:
    text = output or ""
    return ClaudeCLICapabilities(
        stream_json_input="--input-format" in text and "stream-json" in text,
        stream_json_output="--output-format" in text and "stream-json" in text,
        partial_messages="--include-partial-messages" in text,
        replay_user_messages="--replay-user-messages" in text,
        model_selection="--model" in text,
        tools_selection="--tools" in text,
        verbose="--verbose" in text,
        strict_mcp_config="--strict-mcp-config" in text,
    )


def check_claude_cli_available(timeout: float = 10.0) -> tuple[bool, str]:
    """Check the installed binary without accessing credentials or the network."""
    env = build_claude_cli_env()
    try:
        version_proc = subprocess.run(
            [CLAUDE_CLI_EXECUTABLE, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            shell=False,
        )
        help_proc = subprocess.run(
            [CLAUDE_CLI_EXECUTABLE, "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            shell=False,
        )
    except FileNotFoundError:
        return False, "official Claude CLI executable not found"
    except subprocess.TimeoutExpired:
        return False, "Claude CLI capability check timed out"
    if version_proc.returncode != 0 or help_proc.returncode != 0:
        return False, "Claude CLI capability check failed"
    version = parse_claude_cli_version(version_proc.stdout)
    if version is None or version < CLAUDE_CLI_MIN_VERSION:
        return False, "Claude CLI version is below the Stage-0 minimum"
    capabilities = parse_claude_cli_help(help_proc.stdout)
    if not capabilities.stage0_ready:
        return False, "Claude CLI is missing required Stage-0 capabilities"
    return True, ".".join(str(part) for part in version)


class ClaudeCLIError(RuntimeError):
    """Sanitized provider error safe for user-visible reporting."""

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        visible_output: bool = False,
        category: str = "protocol",
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.visible_output = visible_output
        self.category = category


class ClaudeCLIBusyError(ClaudeCLIError):
    pass


@dataclass
class ClaudeCLITurnResult:
    final_text: str
    usage: dict[str, int] = field(default_factory=dict)
    session_id: Optional[str] = None
    event_counts: dict[str, int] = field(default_factory=dict)
    generation: int = 0
    interrupted: bool = False


@dataclass(frozen=True)
class _WireItem:
    generation: int
    kind: str
    event: Optional[dict[str, Any]] = None


class ClaudeCLIEventProjector:
    """Project Claude stream-json frames without exposing raw payloads."""

    @staticmethod
    def text_delta(event: Mapping[str, Any]) -> str:
        if event.get("type") != "stream_event":
            return ""
        inner = event.get("event")
        if not isinstance(inner, Mapping) or inner.get("type") != "content_block_delta":
            return ""
        delta = inner.get("delta")
        if not isinstance(delta, Mapping) or delta.get("type") != "text_delta":
            return ""
        text = delta.get("text")
        return text if isinstance(text, str) else ""

    @staticmethod
    def assistant_text(event: Mapping[str, Any]) -> str:
        if event.get("type") != "assistant":
            return ""
        message = event.get("message")
        if not isinstance(message, Mapping):
            return ""
        content = message.get("content")
        if not isinstance(content, list):
            return ""
        chunks: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    chunks.append(value)
        return "".join(chunks)

    @staticmethod
    def result_text(event: Mapping[str, Any]) -> str:
        value = event.get("result")
        return value if isinstance(value, str) else ""

    @staticmethod
    def usage(event: Mapping[str, Any]) -> dict[str, int]:
        raw = event.get("usage")
        if not isinstance(raw, Mapping):
            return {}
        allowed = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        return {
            key: max(int(raw.get(key) or 0), 0)
            for key in allowed
            if isinstance(raw.get(key), (int, float))
        }


class ClaudeCLIClient:
    """One persistent shell-free Claude CLI process with one active turn."""

    def __init__(
        self,
        *,
        model: str = CLAUDE_CLI_MODEL,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        popen_factory: Callable[..., Any] = _DEFAULT_POPEN,
        queue_size: int = 512,
        generation_start: int = 0,
    ) -> None:
        self.model = model
        self.cwd = cwd
        self._env = build_claude_cli_env(env)
        self._popen_factory = popen_factory
        self._queue_size = queue_size
        self._proc: Any = None
        self._generation = generation_start
        self._events: queue.Queue[_WireItem] = queue.Queue(maxsize=queue_size)
        self._reader_error: Optional[ClaudeCLIError] = None
        self._reader_error_lock = threading.Lock()
        self._stdout_reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._stderr_line_count = 0
        self._stderr_lock = threading.Lock()
        self._session_id: Optional[str] = None
        self._initialized_generation: Optional[int] = None
        self._turn_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._cancelled = False
        self._completed_turns = 0

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._proc, "pid", None)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def completed_turns(self) -> int:
        return self._completed_turns

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _set_reader_error(self, error: ClaudeCLIError) -> None:
        with self._reader_error_lock:
            if self._reader_error is None:
                self._reader_error = error

    def start(self) -> None:
        with self._lifecycle_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        if self._closed:
            raise ClaudeCLIError("claude-cli session is closed", category="closed")
        if self.is_alive():
            return
        if self._completed_turns:
            raise ClaudeCLIError(
                "Claude CLI process exited after a completed turn; refusing to respawn "
                "without the previous session context",
                category="lifecycle",
            )
        if self._popen_factory is _DEFAULT_POPEN:
            available, reason = check_claude_cli_available()
            if not available:
                raise ClaudeCLIError(reason, category="unavailable")
        self._generation += 1
        self._events = queue.Queue(maxsize=self._queue_size)
        self._reader_error = None
        self._session_id = None
        self._initialized_generation = None
        self._cancelled = False
        try:
            from hermes_cli._subprocess_compat import windows_hide_flags

            self._proc = self._popen_factory(
                build_claude_cli_argv(self.model),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                cwd=self.cwd,
                env=dict(self._env),
                shell=False,
                creationflags=windows_hide_flags(),
            )
        except FileNotFoundError as exc:
            raise ClaudeCLIError(
                "official Claude CLI executable not found", category="unavailable"
            ) from exc
        except OSError as exc:
            raise ClaudeCLIError(
                "Claude CLI subprocess could not be started",
                transient=True,
                category="spawn",
            ) from exc
        generation = self._generation
        self._stdout_reader = threading.Thread(
            target=self._read_stdout,
            args=(generation,),
            name=f"claude-cli-stdout-{generation}",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            args=(generation,),
            name=f"claude-cli-stderr-{generation}",
            daemon=True,
        )
        self._stdout_reader.start()
        self._stderr_reader.start()

    def _enqueue(self, item: _WireItem) -> None:
        try:
            self._events.put(item, timeout=0.25)
        except queue.Full:
            self._set_reader_error(
                ClaudeCLIError("Claude CLI event buffer overflow", category="protocol")
            )

    def _read_stdout(self, generation: int) -> None:
        stream = getattr(self._proc, "stdout", None)
        if stream is None:
            self._enqueue(_WireItem(generation, "eof"))
            return
        try:
            for raw in iter(stream.readline, b""):
                if not raw:
                    break
                try:
                    event = json.loads(raw.decode("utf-8", "strict"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._set_reader_error(
                        ClaudeCLIError(
                            "Claude CLI emitted malformed stream-json",
                            category="protocol",
                        )
                    )
                    break
                if not isinstance(event, dict):
                    self._set_reader_error(
                        ClaudeCLIError(
                            "Claude CLI emitted a non-object stream event",
                            category="protocol",
                        )
                    )
                    break
                self._enqueue(_WireItem(generation, "event", event))
        except Exception:
            self._set_reader_error(
                ClaudeCLIError(
                    "Claude CLI stdout reader failed",
                    transient=True,
                    category="pipe",
                )
            )
        finally:
            self._enqueue(_WireItem(generation, "eof"))

    def _read_stderr(self, generation: int) -> None:
        del generation
        stream = getattr(self._proc, "stderr", None)
        if stream is None:
            return
        try:
            for _raw in iter(stream.readline, b""):
                with self._stderr_lock:
                    self._stderr_line_count = min(self._stderr_line_count + 1, 10_000)
        except Exception:
            return

    def stderr_summary(self) -> str:
        with self._stderr_lock:
            count = self._stderr_line_count
        return f"stderr_lines={count}" if count else "stderr_lines=0"

    def _write_user_frame(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("claude-cli input must be text")
        frame = {"type": "user", "message": {"role": "user", "content": text}}
        payload = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        stream = getattr(self._proc, "stdin", None)
        if stream is None:
            raise ClaudeCLIError(
                "Claude CLI stdin is unavailable", transient=True, category="pipe"
            )
        try:
            with self._write_lock:
                stream.write(payload)
                stream.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ClaudeCLIError(
                "Claude CLI stdin closed unexpectedly",
                transient=True,
                category="pipe",
            ) from exc

    def run_turn(
        self,
        text: str,
        *,
        on_delta: Optional[Callable[[str], None]] = None,
        first_event_timeout: float = 30.0,
        idle_timeout: float = 60.0,
        total_timeout: float = 300.0,
    ) -> ClaudeCLITurnResult:
        if not self._turn_lock.acquire(blocking=False):
            raise ClaudeCLIBusyError("claude-cli already has an active turn")
        visible_output = False
        try:
            self.start()
            generation = self._generation
            self._write_user_frame(text)
            started = time.monotonic()
            deadline = started + total_timeout
            event_deadline = started + first_event_timeout
            ack_seen = False
            init_seen = self._initialized_generation == generation
            assistant_text = ""
            delta_text: list[str] = []
            event_counts: dict[str, int] = {}

            def interrupted_result() -> ClaudeCLITurnResult:
                return ClaudeCLITurnResult(
                    final_text=assistant_text or "".join(delta_text),
                    session_id=self._session_id,
                    event_counts=event_counts,
                    generation=generation,
                    interrupted=True,
                )

            while True:
                if self._cancelled:
                    return interrupted_result()
                with self._reader_error_lock:
                    reader_error = self._reader_error
                if reader_error is not None:
                    reader_error.visible_output = visible_output
                    raise reader_error
                now = time.monotonic()
                if now >= deadline:
                    raise ClaudeCLIError(
                        "Claude CLI turn timed out",
                        visible_output=visible_output,
                        category="timeout",
                    )
                wait = max(min(event_deadline, deadline) - now, 0.01)
                try:
                    item = self._events.get(timeout=wait)
                except queue.Empty:
                    if self._cancelled:
                        return interrupted_result()
                    raise ClaudeCLIError(
                        "Claude CLI stream became idle",
                        visible_output=visible_output,
                        category="timeout",
                    )
                if item.generation != generation:
                    continue
                if item.kind == "eof":
                    if self._cancelled:
                        return interrupted_result()
                    raise ClaudeCLIError(
                        "Claude CLI exited before the turn result",
                        transient=not visible_output,
                        visible_output=visible_output,
                        category="eof",
                    )
                event = item.event or {}
                event_deadline = time.monotonic() + idle_timeout
                event_type = str(event.get("type") or "unknown")
                event_counts[event_type] = event_counts.get(event_type, 0) + 1
                event_session_id = event.get("session_id")
                if event_session_id is not None:
                    if not isinstance(event_session_id, str):
                        raise ClaudeCLIError(
                            "Claude CLI emitted an invalid session identifier",
                            visible_output=visible_output,
                            category="correlation",
                        )
                    if self._session_id is None:
                        self._session_id = event_session_id
                    elif event_session_id != self._session_id:
                        raise ClaudeCLIError(
                            "Claude CLI session correlation changed unexpectedly",
                            visible_output=visible_output,
                            category="correlation",
                        )
                if event_type == "user":
                    ack_seen = True
                    continue
                if event_type == "system" and event.get("subtype") == "init":
                    tools = event.get("tools")
                    if not isinstance(tools, list) or tools:
                        raise ClaudeCLIError(
                            "Claude CLI did not honor the Stage-0 zero-tool contract",
                            visible_output=visible_output,
                            category="capability",
                        )
                    self._initialized_generation = generation
                    init_seen = True
                    continue
                message = event.get("message")
                if isinstance(message, Mapping):
                    content = message.get("content")
                    if isinstance(content, list) and any(
                        isinstance(block, Mapping) and block.get("type") == "tool_use"
                        for block in content
                    ):
                        raise ClaudeCLIError(
                            "Claude CLI emitted a tool event in text-only Stage 0",
                            visible_output=visible_output,
                            category="capability",
                        )
                delta = ClaudeCLIEventProjector.text_delta(event)
                if delta:
                    visible_output = True
                    delta_text.append(delta)
                    if on_delta is not None:
                        on_delta(delta)
                    continue
                projected = ClaudeCLIEventProjector.assistant_text(event)
                if projected:
                    visible_output = True
                    assistant_text = projected
                    continue
                if event_type != "result":
                    continue
                if not ack_seen:
                    raise ClaudeCLIError(
                        "Claude CLI result arrived without the replay acknowledgment",
                        visible_output=visible_output,
                        category="correlation",
                    )
                if not init_seen:
                    raise ClaudeCLIError(
                        "Claude CLI result arrived before Stage-0 initialization",
                        visible_output=visible_output,
                        category="correlation",
                    )
                if event.get("is_error"):
                    subtype = str(event.get("subtype") or "provider_error")
                    category = (
                        "policy"
                        if any(word in subtype.lower() for word in ("auth", "billing", "policy", "permission"))
                        else "provider"
                    )
                    raise ClaudeCLIError(
                        f"Claude CLI turn failed ({category})",
                        visible_output=visible_output,
                        category=category,
                    )
                if self._proc.poll() is not None:
                    raise ClaudeCLIError(
                        "Claude CLI exited instead of preserving the persistent session",
                        visible_output=visible_output,
                        category="lifecycle",
                    )
                final_text = ClaudeCLIEventProjector.result_text(event) or assistant_text or "".join(delta_text)
                self._completed_turns += 1
                return ClaudeCLITurnResult(
                    final_text=final_text,
                    usage=ClaudeCLIEventProjector.usage(event),
                    session_id=self._session_id,
                    event_counts=event_counts,
                    generation=generation,
                )
        except ClaudeCLIError as exc:
            exc.visible_output = exc.visible_output or visible_output
            raise
        finally:
            self._turn_lock.release()

    def request_interrupt(self) -> None:
        """Fail closed by retiring the process; no native cancel frame is claimed."""
        self._cancelled = True
        self.close()

    def close(self, grace: float = 1.0, terminate_timeout: float = 2.0) -> None:
        with self._lifecycle_lock:
            self._close_locked(grace, terminate_timeout)

    def _close_locked(self, grace: float, terminate_timeout: float) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        stdin = getattr(proc, "stdin", None)
        try:
            if stdin is not None and not stdin.closed:
                stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=terminate_timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            except Exception:
                pass
        for reader in (self._stdout_reader, self._stderr_reader):
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=1.0)


class ClaudeCLISession:
    """Persistent session facade with the single permitted pre-output retry."""

    def __init__(self, **client_kwargs: Any) -> None:
        self._client_kwargs = dict(client_kwargs)
        self._client: Optional[ClaudeCLIClient] = None
        self._closed = False
        self._process_generation = 0

    @property
    def client(self) -> Optional[ClaudeCLIClient]:
        return self._client

    def _new_client(self) -> ClaudeCLIClient:
        self._client = ClaudeCLIClient(
            **self._client_kwargs,
            generation_start=self._process_generation,
        )
        self._process_generation += 1
        return self._client

    def run_turn(self, text: str, **kwargs: Any) -> ClaudeCLITurnResult:
        if self._closed:
            raise ClaudeCLIError("claude-cli session is closed", category="closed")
        client = self._client or self._new_client()
        try:
            return client.run_turn(text, **kwargs)
        except ClaudeCLIBusyError:
            raise
        except ClaudeCLIError as exc:
            can_retry = (
                not getattr(client, "_cancelled", False)
                and not self._closed
                and exc.transient
                and not exc.visible_output
                and client.completed_turns == 0
                and exc.category in {"spawn", "pipe", "eof"}
            )
            client.close()
            if self._client is client:
                self._client = None
            if not can_retry:
                raise
            return self._new_client().run_turn(text, **kwargs)

    def request_interrupt(self) -> None:
        if self._client is not None:
            self._client.request_interrupt()
            self._client = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            self._client.close()
            self._client = None


class ClaudeCLITransport(ProviderTransport):
    """Registry adapter for the special ``claude_cli`` runtime."""

    @property
    def api_mode(self) -> str:
        return "claude_cli"

    def convert_messages(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        del kwargs
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("claude-cli requires a trailing text user message")
        content = messages[-1].get("content")
        if not isinstance(content, str):
            raise ValueError("claude-cli Stage 0 is text-only")
        return content

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[Any]:
        if tools:
            raise ValueError("claude-cli Stage 0 does not expose tools")
        return []

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **params: Any,
    ) -> dict[str, Any]:
        if params.get("api_key") or params.get("base_url"):
            raise ValueError("claude-cli does not accept api_key or base_url")
        build_claude_cli_argv(model)
        self.convert_tools(tools or [])
        return {"model": model, "text": self.convert_messages(messages)}

    def normalize_response(self, response: Any, **kwargs: Any) -> NormalizedResponse:
        del kwargs
        if not isinstance(response, ClaudeCLITurnResult):
            raise TypeError("expected ClaudeCLITurnResult")
        prompt = int(response.usage.get("input_tokens", 0))
        cached = int(response.usage.get("cache_read_input_tokens", 0))
        completion = int(response.usage.get("output_tokens", 0))
        return NormalizedResponse(
            content=response.final_text,
            tool_calls=None,
            finish_reason="stop",
            usage=Usage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                cached_tokens=cached,
            ),
        )


def _usage_int(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(int(value), 0)
    return 0


def _record_claude_cli_usage(agent: Any, turn: ClaudeCLITurnResult) -> dict[str, Any]:
    """Route CLI-reported usage through Hermes' canonical accounting wrapper."""
    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    agent.session_api_calls = getattr(agent, "session_api_calls", 0) + 1
    raw = turn.usage if isinstance(turn.usage, dict) else {}
    canonical = CanonicalUsage(
        input_tokens=_usage_int(raw, "input_tokens"),
        output_tokens=_usage_int(raw, "output_tokens"),
        cache_read_tokens=_usage_int(raw, "cache_read_input_tokens"),
        cache_write_tokens=_usage_int(raw, "cache_creation_input_tokens"),
        reasoning_tokens=0,
        raw_usage=raw,
    )
    usage = {
        "prompt_tokens": canonical.prompt_tokens,
        "completion_tokens": canonical.output_tokens,
        "total_tokens": canonical.total_tokens,
        "input_tokens": canonical.input_tokens,
        "output_tokens": canonical.output_tokens,
        "cache_read_tokens": canonical.cache_read_tokens,
        "cache_write_tokens": canonical.cache_write_tokens,
        "reasoning_tokens": 0,
    }
    agent._last_turn_usage = usage

    compressor = getattr(agent, "context_compressor", None)
    if compressor is not None:
        try:
            compressor.update_from_response(usage)
        except Exception:
            pass

    counter_fields = {
        "session_prompt_tokens": canonical.prompt_tokens,
        "session_completion_tokens": canonical.output_tokens,
        "session_total_tokens": canonical.total_tokens,
        "session_input_tokens": canonical.input_tokens,
        "session_output_tokens": canonical.output_tokens,
        "session_cache_read_tokens": canonical.cache_read_tokens,
        "session_cache_write_tokens": canonical.cache_write_tokens,
        "session_reasoning_tokens": 0,
    }
    for name, value in counter_fields.items():
        setattr(agent, name, getattr(agent, name, 0) + value)

    cost = estimate_usage_cost(
        agent.model,
        canonical,
        provider=agent.provider,
        base_url=agent.base_url,
        api_key="",
    )
    if cost.amount_usd is not None:
        agent.session_estimated_cost_usd = getattr(
            agent, "session_estimated_cost_usd", 0.0
        ) + float(cost.amount_usd)
    agent.session_cost_status = cost.status
    agent.session_cost_source = cost.source

    if getattr(agent, "_session_db", None) is not None and getattr(agent, "session_id", None):
        try:
            if not getattr(agent, "_session_db_created", False):
                agent._ensure_db_session()
            agent._session_db.queue_token_counts(
                agent.session_id,
                input_tokens=canonical.input_tokens,
                output_tokens=canonical.output_tokens,
                cache_read_tokens=canonical.cache_read_tokens,
                cache_write_tokens=canonical.cache_write_tokens,
                reasoning_tokens=0,
                estimated_cost_usd=float(cost.amount_usd)
                if cost.amount_usd is not None
                else None,
                cost_status=cost.status,
                cost_source=cost.source,
                billing_provider=agent.provider,
                billing_base_url="",
                billing_mode="subscription_included",
                model=agent.model,
                api_call_count=1,
            )
        except Exception:
            pass

    return {
        **usage,
        "last_prompt_tokens": canonical.prompt_tokens,
        "estimated_cost_usd": float(cost.amount_usd)
        if cost.amount_usd is not None
        else None,
        "cost_status": cost.status,
        "cost_source": cost.source,
    }


def run_claude_cli_turn(
    agent: Any,
    *,
    user_message: str,
    original_user_message: Any,
    messages: list[dict[str, Any]],
    effective_task_id: str,
    should_review_memory: bool = False,
) -> dict[str, Any]:
    """Run one normal Hermes turn through the persistent text-only session."""
    del effective_task_id

    def on_delta(text: str) -> None:
        callback = getattr(agent, "_fire_stream_delta", None)
        if callable(callback):
            callback(text)

    if getattr(agent, "_claude_cli_session", None) is None:
        from agent.runtime_cwd import resolve_agent_cwd

        cwd = getattr(agent, "session_cwd", None) or str(resolve_agent_cwd())
        agent._claude_cli_session = ClaudeCLISession(model=agent.model, cwd=cwd)

    try:
        turn = agent._claude_cli_session.run_turn(user_message, on_delta=on_delta)
    except ClaudeCLIBusyError as exc:
        return {
            "final_response": f"Claude CLI turn failed: {exc}",
            "messages": messages,
            "api_calls": 0,
            "completed": False,
            "partial": False,
            "interrupted": False,
            "error": str(exc),
        }
    except ClaudeCLIError as exc:
        try:
            agent._claude_cli_session.close()
        except Exception:
            pass
        agent._claude_cli_session = None
        interrupted = bool(getattr(agent, "_interrupt_requested", False))
        interrupt_message = getattr(agent, "_interrupt_message", None) if interrupted else None
        if interrupted:
            agent.clear_interrupt()
        return {
            "final_response": f"Claude CLI turn failed: {exc}",
            "messages": messages,
            "api_calls": 0,
            "completed": False,
            "partial": bool(exc.visible_output),
            "interrupted": interrupted,
            **({"interrupt_message": interrupt_message} if interrupt_message else {}),
            "error": str(exc),
        }

    interrupt_requested = bool(getattr(agent, "_interrupt_requested", False))
    user_interrupted = bool(turn.interrupted and interrupt_requested)
    interrupt_message = (
        getattr(agent, "_interrupt_message", None) if user_interrupted else None
    )
    if interrupt_requested:
        agent.clear_interrupt()

    messages.append({"role": "assistant", "content": turn.final_text})
    if getattr(agent, "_session_db", None) is not None:
        try:
            agent._flush_messages_to_session_db(messages)
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "claude-cli turn was delivered but could not be persisted",
                exc_info=True,
            )

    usage = _record_claude_cli_usage(agent, turn)
    agent._iters_since_skill = getattr(agent, "_iters_since_skill", 0) + 1

    if not turn.interrupted:
        try:
            agent._sync_external_memory_for_turn(
                original_user_message=original_user_message,
                final_response=turn.final_text,
                interrupted=False,
                messages=messages,
            )
        except Exception:
            pass

    if should_review_memory and turn.final_text and not turn.interrupted:
        try:
            agent._spawn_background_review(
                messages_snapshot=list(messages),
                review_memory=True,
                review_skills=False,
            )
        except Exception:
            pass

    return {
        "final_response": turn.final_text,
        "messages": messages,
        "api_calls": 1,
        "completed": not turn.interrupted,
        "partial": turn.interrupted,
        "interrupted": user_interrupted,
        **({"interrupt_message": interrupt_message} if interrupt_message else {}),
        "error": None,
        "agent_persisted": True,
        "claude_cli_generation": turn.generation,
        **usage,
    }


from agent.transports import register_transport

register_transport("claude_cli", ClaudeCLITransport)


__all__ = [
    "CLAUDE_CLI_MODEL",
    "CLAUDE_CLI_MIN_VERSION",
    "CLAUDE_CLI_ENV_BLOCKLIST",
    "ClaudeCLIBusyError",
    "ClaudeCLICapabilities",
    "ClaudeCLIClient",
    "ClaudeCLIError",
    "ClaudeCLIEventProjector",
    "ClaudeCLISession",
    "ClaudeCLITurnResult",
    "ClaudeCLITransport",
    "build_claude_cli_argv",
    "build_claude_cli_env",
    "check_claude_cli_available",
    "parse_claude_cli_help",
    "parse_claude_cli_version",
    "run_claude_cli_turn",
]
