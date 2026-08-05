"""Relay compatibility and notification adapters for Hermes model attempts."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

from agent import relay_runtime

logger = logging.getLogger(__name__)


def execute(
    request: dict[str, Any],
    callback: Callable[[dict[str, Any]], Any],
    *,
    session_id: str,
    name: str,
    model_name: str,
    metadata: dict[str, Any] | None = None,
    defer_logical_completion: bool = False,
) -> Any:
    """Run one direct, model-authoritative non-streaming provider attempt."""
    logical = _begin_logical_notification(
        _execution_notification_metadata(metadata, name=name, model_name=model_name),
        session_id=session_id,
    )
    try:
        response = callback(request)
    except BaseException:
        _complete_logical(logical, outcome="failed")
        raise
    if not defer_logical_completion:
        _complete_logical(logical, outcome="success")
    return response


async def execute_async(
    request: dict[str, Any],
    callback: Callable[[dict[str, Any]], Any],
    *,
    session_id: str,
    name: str,
    model_name: str,
    metadata: dict[str, Any] | None = None,
    defer_logical_completion: bool = False,
) -> Any:
    """Run one direct, model-authoritative async provider attempt."""
    logical = _begin_logical_notification(
        _execution_notification_metadata(metadata, name=name, model_name=model_name),
        session_id=session_id,
    )
    try:
        response = await callback(request)
    except BaseException:
        _complete_logical(logical, outcome="failed")
        raise
    if not defer_logical_completion:
        _complete_logical(logical, outcome="success")
    return response


def execute_current(
    request: dict[str, Any],
    callback: Callable[[dict[str, Any]], Any],
    *,
    name: str,
    model_name: str,
    metadata: dict[str, Any] | None = None,
    defer_logical_completion: bool = False,
) -> Any:
    """Run a provider attempt under the inherited Hermes turn when present."""
    turn = relay_runtime.active_turn()
    if turn is None:
        return callback(request)
    return execute(
        request,
        callback,
        session_id=turn.lease.session_id,
        name=name,
        model_name=model_name,
        metadata=metadata,
        defer_logical_completion=defer_logical_completion,
    )


async def execute_current_async(
    request: dict[str, Any],
    callback: Callable[[dict[str, Any]], Any],
    *,
    name: str,
    model_name: str,
    metadata: dict[str, Any] | None = None,
    defer_logical_completion: bool = False,
) -> Any:
    """Run an async provider attempt under the inherited turn when present."""
    turn = relay_runtime.active_turn()
    if turn is None:
        return await callback(request)
    return await execute_async(
        request,
        callback,
        session_id=turn.lease.session_id,
        name=name,
        model_name=model_name,
        metadata=metadata,
        defer_logical_completion=defer_logical_completion,
    )


def stream_current(
    request: dict[str, Any],
    stream_factory: Callable[[dict[str, Any]], Any],
    *,
    name: str,
    model_name: str,
    finalizer: Callable[[], Any],
    metadata: dict[str, Any] | None = None,
    defer_logical_completion: bool = False,
    completed_response_predicate: Callable[[Any], bool] | None = None,
) -> Any:
    """Open a direct provider stream through the historical current-turn API.

    This is an observer-only compatibility surface, so it returns the exact
    provider object whether that object is an iterator or an already-completed
    response. It cannot derive or finalize a logical outcome from transport
    iteration state.
    """
    del (
        name,
        model_name,
        finalizer,
        metadata,
        defer_logical_completion,
        completed_response_predicate,
    )
    # Historical/extension callers get the exact raw provider object. They do
    # not own semantic validation and therefore cannot open or finalize a
    # logical lifecycle scope from raw iteration state.
    return stream_factory(request)


def provider_stream(
    request: dict[str, Any],
    stream_factory: Callable[[dict[str, Any]], Any],
    *,
    on_provider_chunk: Callable[[Any], None] | None = None,
    observer: Callable[[Any], None] | None = None,
    accept_chunk: Callable[[Any], bool] | None = None,
    lifecycle_metadata: dict[str, Any] | None = None,
    lifecycle_session_id: str | None = None,
    completed_response_predicate: Callable[[Any], bool] | None = None,
) -> "ProviderLlmStream":
    """Open one direct, model-authoritative provider stream.

    Lifecycle observation receives detached structural metadata only.  Raw
    stream exhaustion, cleanup, and transport failures are deliberately not
    logical outcomes: only the trusted consumer can decide whether the bytes
    form a semantically accepted response.  It completes the logical call via
    :func:`complete_logical_call` after validation.
    """
    _begin_logical_notification(
        lifecycle_metadata,
        session_id=lifecycle_session_id,
    )

    return ProviderLlmStream(
        request,
        stream_factory,
        on_provider_chunk=on_provider_chunk,
        observer=observer,
        accept_chunk=accept_chunk,
        completed_response_predicate=completed_response_predicate,
    )


def stream(
    request: dict[str, Any],
    stream_factory: Callable[[dict[str, Any]], Any],
    *,
    session_id: str,
    name: str,
    model_name: str,
    finalizer: Callable[[], Any],
    on_chunk: Callable[[Any], None] | None = None,
    chunk_adapter: Callable[[Any], Any] | None = None,
    accept_chunk: Callable[[Any], bool] | None = None,
    completed_response_predicate: Callable[[Any], bool] | None = None,
    metadata: dict[str, Any] | None = None,
    defer_logical_completion: bool = False,
) -> "ManagedLlmStream":
    """Return a model-authoritative synchronous provider stream view.

    The historical Relay-shaped arguments are accepted for compatibility but
    are inert. Only the detached chunk observer remains; compatibility callers
    cannot open or finalize logical lifecycle state.
    """
    del session_id, name, model_name, finalizer, chunk_adapter, metadata
    # Historical Relay/plugin callers cannot install a stop predicate.  Only
    # trusted core call sites can use ``provider_stream(accept_chunk=...)`` for
    # exact single-writer/stale-attempt fencing.
    del accept_chunk
    del defer_logical_completion
    return provider_stream(
        request,
        stream_factory,
        observer=on_chunk,
        completed_response_predicate=completed_response_predicate,
    )


class ProviderLlmStream(Iterator[Any]):
    """Pass through one model-authoritative provider stream.

    ``ManagedLlmStream`` remains as a compatibility surface for callers that
    expect ``final_response``/``output_modified`` and explicit ``close()``.
    Relay is deliberately *not* in the execution path: it cannot rewrite the
    request, invoke the provider zero or multiple times, replace chunks, stop
    iteration early, or synthesize a final response.  Lifecycle/middleware
    observers are notified by the surrounding agent boundary with detached
    snapshots; this adapter only preserves the provider's exact stream.
    """

    def __init__(
        self,
        request: dict[str, Any],
        stream_factory: Callable[[dict[str, Any]], Any],
        *,
        on_provider_chunk: Callable[[Any], None] | None,
        observer: Callable[[Any], None] | None,
        accept_chunk: Callable[[Any], bool] | None,
        completed_response_predicate: Callable[[Any], bool] | None,
    ) -> None:
        self.final_response: Any = None
        self.output_modified = False
        self._closed = False
        self._close_error: BaseException | None = None
        self._on_provider_chunk = on_provider_chunk
        self._observer = observer
        self._accept_chunk = accept_chunk
        self._stream: Any = None
        self._raw_stream_resource: Any = None

        raw_stream = stream_factory(request)

        self._raw_stream_resource = raw_stream
        try:
            if (
                completed_response_predicate is not None
                and completed_response_predicate(raw_stream)
            ):
                self.final_response = raw_stream
                self._raw_stream_resource = None
                self._stream = iter(())
                return

            self._stream = iter(raw_stream)
        except BaseException:
            self._close()
            raise

    def __iter__(self) -> "ProviderLlmStream":
        return self

    def __next__(self) -> Any:
        if self._closed:
            raise StopIteration
        try:
            chunk = next(self._stream)
        except StopIteration:
            self._close()
            raise
        except BaseException:
            self._close()
            raise

        if self._accept_chunk is not None and not self._accept_chunk(chunk):
            self._close()
            raise StopIteration

        if self._on_provider_chunk is not None:
            # Trusted core parsing is part of the exact provider-response
            # boundary.  A parse/accumulation failure must be visible; silently
            # continuing could fabricate an incomplete final response.
            try:
                self._on_provider_chunk(chunk)
            except BaseException:
                self._close()
                raise

        if self._observer is not None:
            try:
                # A detached JSON-compatible snapshot prevents observers from
                # retaining or mutating the provider-owned chunk.  Notification
                # failure is fail-open and never changes the stream.
                self._observer(_jsonable(chunk))
            except Exception:
                logger.warning(
                    "Provider stream observer failed; preserving provider chunk",
                    exc_info=True,
                )
        return chunk

    def close(self) -> None:
        """Close an explicitly abandoned provider stream exactly once."""
        self._close()
        close_error = self._close_error
        self._close_error = None
        if close_error is not None:
            raise close_error

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        resources = (self._stream, self._raw_stream_resource)
        self._stream = None
        self._raw_stream_resource = None
        closed_ids: set[int] = set()
        for resource in resources:
            if resource is None or id(resource) in closed_ids:
                continue
            closed_ids.add(id(resource))
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:
                if self._close_error is None:
                    self._close_error = exc
                logger.debug("Provider stream cleanup failed", exc_info=True)

    def __del__(self) -> None:
        self._close()


# Backward-compatible name for extensions that imported the historical type.
ManagedLlmStream = ProviderLlmStream


class AnthropicStreamAccumulator:
    """Rebuild an Anthropic Message from post-intercept SSE events."""

    def __init__(self) -> None:
        self._message: dict[str, Any] = {}
        self._blocks: dict[int, dict[str, Any]] = {}

    def observe(self, event: Any) -> None:
        payload = _jsonable(event)
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type")
        if event_type == "message_start":
            message = payload.get("message")
            if isinstance(message, dict):
                for key in ("id", "type", "role", "model", "usage"):
                    if key in message:
                        self._message[key] = message[key]
            return
        if event_type == "content_block_start":
            index = payload.get("index")
            block = payload.get("content_block")
            if isinstance(index, int) and isinstance(block, dict):
                self._blocks[index] = dict(block)
            return
        if event_type == "content_block_delta":
            index = payload.get("index")
            delta = payload.get("delta")
            if not isinstance(index, int) or not isinstance(delta, dict):
                return
            block = self._blocks.setdefault(index, {})
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                block["text"] = str(block.get("text") or "") + str(
                    delta.get("text") or ""
                )
            elif delta_type == "thinking_delta":
                block["thinking"] = str(block.get("thinking") or "") + str(
                    delta.get("thinking") or ""
                )
            elif delta_type == "signature_delta":
                block["signature"] = str(block.get("signature") or "") + str(
                    delta.get("signature") or ""
                )
            elif delta_type == "input_json_delta":
                partial = str(block.pop("_partial_json", "")) + str(
                    delta.get("partial_json") or ""
                )
                block["_partial_json"] = partial
            elif delta_type == "citations_delta" and "citation" in delta:
                block.setdefault("citations", []).append(delta["citation"])
            return
        if event_type == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict):
                for key in ("stop_reason", "stop_sequence"):
                    if key in delta:
                        self._message[key] = delta[key]
            if "usage" in payload:
                usage = payload["usage"]
                current_usage = self._message.get("usage")
                if isinstance(current_usage, dict) and isinstance(usage, dict):
                    self._message["usage"] = {**current_usage, **usage}
                else:
                    self._message["usage"] = usage

    def finalize(self) -> dict[str, Any]:
        blocks = []
        for index in sorted(self._blocks):
            block = dict(self._blocks[index])
            partial = block.pop("_partial_json", None)
            if partial is not None:
                try:
                    block["input"] = json.loads(partial)
                except (TypeError, ValueError):
                    block["input"] = partial
            blocks.append(block)
        return {**self._message, "content": blocks}

    def response(self, base: Any = None) -> Any:
        """Return the attribute-shaped response consumed by Hermes."""
        assembled = self.finalize()
        base_payload = _jsonable(base)
        if not isinstance(base_payload, dict):
            base_payload = {}
        content = assembled.pop("content", [])
        merged = {**base_payload, **assembled}
        if content or "content" not in merged:
            merged["content"] = content
        return _namespace(merged)


def _logical_parent(
    runtime: relay_runtime.RelayRuntime,
    session: Any,
    parent: Any,
    metadata: dict[str, Any] | None,
) -> tuple[relay_runtime.RelayTurnContext, Any, str] | None:
    turn = relay_runtime.active_turn(session.session_id)
    request_id = str((metadata or {}).get("api_request_id") or "")
    if turn is None or not request_id or turn.lease.host is not runtime:
        return None
    with turn.finalize_lock:
        if turn.closed:
            return None
        with turn.logical_llm_lock:
            handle = turn.logical_llm_calls.get(request_id)
            if handle is None:
                handle = runtime.run_in_session(
                    session,
                    runtime.relay.scope.push,
                    relay_runtime.LOGICAL_LLM_SCOPE,
                    runtime.relay.ScopeType.Function,
                    handle=parent,
                    input={},
                    metadata={
                        relay_runtime.RUNTIME_SCHEMA_KEY: relay_runtime.RUNTIME_SCHEMA_VERSION,
                        relay_runtime.RUNTIME_INSTANCE_KEY: runtime.runtime_id,
                        "hermes.call_role": str(
                            (metadata or {}).get("call_role") or "primary"
                        ),
                    },
                )
                turn.logical_llm_calls[request_id] = handle
    return turn, handle, request_id


def _execution_notification_metadata(
    metadata: dict[str, Any] | None,
    *,
    name: str,
    model_name: str,
) -> dict[str, Any]:
    """Return a detached scalar-only lifecycle description."""
    source = metadata or {}
    return {
        "api_request_id": str(source.get("api_request_id") or ""),
        "call_role": str(source.get("call_role") or "primary"),
        "api_mode": str(source.get("api_mode") or "unknown"),
        "provider": str(name or source.get("provider") or "unknown"),
        "model": str(model_name or source.get("model") or "unknown"),
    }


def _begin_logical_notification(
    metadata: dict[str, Any] | None,
    *,
    session_id: str | None = None,
) -> tuple[relay_runtime.RelayTurnContext, Any, str] | None:
    """Open an optional Relay lifecycle scope without mediating execution.

    This function reads only structural turn state.  It never passes the
    provider request, stream factory, chunks, or response through Relay.  A
    missing runtime, disabled observer, or notification failure therefore
    falls back to the provider's raw stream contract.
    """
    turn = relay_runtime.active_turn(session_id)
    request_id = str((metadata or {}).get("api_request_id") or "")
    if turn is None or not request_id:
        return None
    lease = turn.lease
    runtime = lease.host
    session = lease.session
    if (
        not isinstance(runtime, relay_runtime.RelayRuntime)
        or session is None
        or not runtime.managed_execution_enabled()
    ):
        return None
    try:
        return _logical_parent(
            runtime,
            session,
            turn.handle or session.handle,
            metadata,
        )
    except Exception:
        logger.warning(
            "Hermes Relay logical LLM notification start failed; "
            "returning the raw provider stream",
            exc_info=True,
        )
        return None


def _complete_logical(
    logical: tuple[relay_runtime.RelayTurnContext, Any, str] | None,
    *,
    outcome: str,
    model_name: str | None = None,
    provider_name: str | None = None,
    response_model_name: str | None = None,
) -> None:
    if logical is None:
        return
    turn, handle, request_id = logical
    lease = turn.lease
    if not isinstance(lease.host, relay_runtime.RelayRuntime):
        return
    with turn.finalize_lock:
        with turn.logical_llm_lock:
            if turn.logical_llm_calls.get(request_id) is not handle:
                return
        if lease.session is None:
            return
        try:
            output: dict[str, Any] = {"outcome": outcome}
            if model_name is not None and provider_name is not None:
                output.update({"model": model_name, "provider": provider_name})
                if response_model_name is not None:
                    output["response_model"] = response_model_name
            lease.host.run_in_session(
                lease.session,
                lease.host.relay.scope.pop,
                handle,
                output=output,
                metadata={
                    relay_runtime.RUNTIME_SCHEMA_KEY: relay_runtime.RUNTIME_SCHEMA_VERSION,
                    relay_runtime.RUNTIME_INSTANCE_KEY: lease.host.runtime_id,
                },
            )
        except Exception:
            # The provider result is authoritative. Retain the handle so turn
            # finalization can retry cleanup without changing that result.
            logger.warning(
                "Hermes Relay logical LLM finalization failed",
                exc_info=True,
            )
            return
        with turn.logical_llm_lock:
            if turn.logical_llm_calls.get(request_id) is handle:
                turn.logical_llm_calls.pop(request_id, None)


def complete_logical_call(
    api_request_id: str,
    *,
    outcome: str,
    model_name: str | None = None,
    provider_name: str | None = None,
    response_model_name: str | None = None,
) -> None:
    """Complete the active turn's logical LLM call after caller validation."""
    turn = relay_runtime.active_turn()
    if turn is None or not api_request_id:
        return
    with turn.logical_llm_lock:
        handle = turn.logical_llm_calls.get(api_request_id)
    if handle is not None:
        _complete_logical(
            (turn, handle, api_request_id),
            outcome=outcome,
            model_name=model_name,
            provider_name=provider_name,
            response_model_name=response_model_name,
        )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(type(value), "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(value.model_dump(mode="json"))
        except Exception:
            pass
    try:
        attributes = {
            str(key): item
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    except (TypeError, AttributeError):
        return str(value)
    return _jsonable(attributes) if attributes else str(value)


def _namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{
            str(key): _namespace(item) for key, item in value.items()
        })
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value
