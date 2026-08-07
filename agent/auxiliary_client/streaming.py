"""Streaming helpers for auxiliary chat.completions calls.

Extracted from the former ``agent/auxiliary_client.py`` monolith into a
subpackage module. Provides progress-ticking stream aggregation used by
``call_llm``/``async_call_llm`` and stream-only providers.
"""

import inspect
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from . import (
    _aux_progress_active,
    _aux_stream_total_ceiling,
    _client_streams_internally,
    _is_auth_error,
    _is_payment_error,
    _is_rate_limit_error,
    _is_transient_transport_error,
    _notify_aux_progress,
    logger,
)

def _create_with_progress(
    client: Any,
    kwargs: Dict[str, Any],
    task: Optional[str] = None,
    *,
    force_stream: bool = False,
) -> Any:
    """chat.completions.create() that streams when a progress hook is active
    or the provider only accepts streamed requests.

    Behavior is byte-for-byte identical to a plain ``create(**kwargs)`` when
    neither trigger applies (every existing caller/task) or when the client's
    wire adapter streams internally. With a hook + a chunk-capable client,
    the request is sent with ``stream=True`` and aggregated, ticking the hook
    per chunk — so the configured ``timeout`` acts per stream read (idle)
    rather than as a total budget, and outer liveness watchdogs see tokens
    moving. ``force_stream=True`` (stream-only providers such as Tencent
    Copilot — credit @kudi88, PR #60686) takes the same streamed path even
    without a hook. Providers that reject the streamed request fall back to
    the plain non-streaming call — except under ``force_stream``, where a
    stream-only provider rejects the plain call by definition, so the
    original error is surfaced to the normal recovery chains instead.
    """
    _notify_aux_progress()  # request dispatched counts as progress
    if (not _aux_progress_active() and not force_stream) or _client_streams_internally(client):
        return client.chat.completions.create(**kwargs)

    total_ceiling = _aux_stream_total_ceiling(kwargs.get("timeout"))
    stream_kwargs = dict(kwargs)
    stream_kwargs["stream"] = True
    stream_kwargs["stream_options"] = {"include_usage": True}
    try:
        chunks = client.chat.completions.create(**stream_kwargs)
    except Exception as exc:
        # Genuine provider failures (auth, credit, rate limit, network) are
        # not streaming's fault — surface them unchanged so the existing
        # recovery chains (credential refresh, pool rotation, provider
        # fallback) see the same error they would on a plain call.
        if (
            force_stream
            or _is_transient_transport_error(exc)
            or _is_auth_error(exc)
            or _is_payment_error(exc)
            or _is_rate_limit_error(exc)
        ):
            raise
        # Anything else may be a streaming-specific rejection (explicit
        # "stream not supported", stream_options 400, or an idiosyncratic
        # 4xx). Retry non-streaming once; if the request itself is bad the
        # plain call reproduces the real error for the normal except-chains.
        logger.debug(
            "Auxiliary %s: streamed request failed (%s); retrying "
            "non-streaming", task or "call", exc,
        )
        return client.chat.completions.create(**kwargs)

    # Some shims (MoA virtual provider under quiet mode, defensive adapters)
    # return a complete response even when stream=True was requested.
    if hasattr(chunks, "choices"):
        _notify_aux_progress()
        return chunks
    return _aggregate_chat_stream(
        chunks, model=str(kwargs.get("model") or ""), total_ceiling=total_ceiling,
    )


def _aggregate_chat_stream(
    chunks: Any,
    *,
    model: str = "",
    total_ceiling: Optional[float] = None,
) -> Any:
    """Consume a chat.completions chunk stream into a complete response.

    Ticks the thread-local aux progress hook on every chunk. Raises
    TimeoutError when *total_ceiling* seconds elapse before the stream
    finishes — phrased with "timed out" so existing timeout classification
    (``_is_timeout_error``) treats it exactly like a request timeout.
    Accumulation is shared with the async mirror via
    :class:`_ChatStreamAccumulator`.
    """
    acc = _ChatStreamAccumulator(model=model, total_ceiling=total_ceiling)
    try:
        for chunk in chunks:
            acc.feed(chunk)
    finally:
        close_fn = getattr(chunks, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
    return acc.finish()


class _ChatStreamAccumulator:
    """Shared per-chunk accumulation for sync and async stream aggregation.

    Mirrors :func:`_aggregate_chat_stream`'s chunk handling so the async
    consumer below cannot drift from the sync one (same content/reasoning/
    tool-call delta reassembly, same "timed out" ceiling phrasing).
    """

    def __init__(self, model: str = "", total_ceiling: Optional[float] = None):
        self._started = time.monotonic()
        self._total_ceiling = total_ceiling
        self.content_parts: List[str] = []
        self.reasoning_parts: List[str] = []
        self.tool_calls_acc: Dict[int, Dict[str, Any]] = {}
        self.finish_reason = None
        self.usage = None
        self.resp_id = ""
        self.resp_model = model or ""

    def feed(self, chunk: Any) -> None:
        _notify_aux_progress()
        if (
            self._total_ceiling is not None
            and (time.monotonic() - self._started) >= self._total_ceiling
        ):
            raise TimeoutError(
                f"Auxiliary streamed call timed out after {self._total_ceiling:.0f}s "
                "total ceiling (stream still open but over budget)"
            )
        self.resp_id = getattr(chunk, "id", None) or self.resp_id
        self.resp_model = getattr(chunk, "model", None) or self.resp_model
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage:
            self.usage = chunk_usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        choice = choices[0]
        self.finish_reason = getattr(choice, "finish_reason", None) or self.finish_reason
        delta = getattr(choice, "delta", None)
        if delta is None:
            return
        piece = getattr(delta, "content", None)
        if piece:
            self.content_parts.append(piece)
        reasoning_piece = (
            getattr(delta, "reasoning", None)
            or getattr(delta, "reasoning_content", None)
        )
        if reasoning_piece and isinstance(reasoning_piece, str):
            self.reasoning_parts.append(reasoning_piece)
        for tc in (getattr(delta, "tool_calls", None) or []):
            idx = getattr(tc, "index", 0) or 0
            acc = self.tool_calls_acc.setdefault(
                idx, {"id": "", "name": "", "arguments": []}
            )
            if getattr(tc, "id", None):
                acc["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    acc["name"] = fn.name
                if getattr(fn, "arguments", None):
                    acc["arguments"].append(fn.arguments)

    def finish(self) -> Any:
        tool_calls = None
        if self.tool_calls_acc:
            tool_calls = [
                SimpleNamespace(
                    id=acc["id"],
                    type="function",
                    function=SimpleNamespace(
                        name=acc["name"],
                        arguments="".join(acc["arguments"]),
                    ),
                )
                for _idx, acc in sorted(self.tool_calls_acc.items())
            ]
        message = SimpleNamespace(
            role="assistant",
            content="".join(self.content_parts),
            tool_calls=tool_calls,
            reasoning="".join(self.reasoning_parts) or None,
        )
        choice = SimpleNamespace(
            index=0,
            message=message,
            finish_reason=self.finish_reason or "stop",
        )
        return SimpleNamespace(
            id=self.resp_id,
            model=self.resp_model,
            object="chat.completion",
            choices=[choice],
            usage=self.usage,
        )


async def _aggregate_chat_stream_async(
    chunks: Any,
    *,
    model: str = "",
    total_ceiling: Optional[float] = None,
) -> Any:
    """Async mirror of :func:`_aggregate_chat_stream` (``async for`` consumer).

    The AsyncOpenAI stream contract is an async iterator — consuming it with
    the sync helper raises. Same accumulation and ceiling semantics via
    :class:`_ChatStreamAccumulator`.
    """
    acc = _ChatStreamAccumulator(model=model, total_ceiling=total_ceiling)
    try:
        async for chunk in chunks:
            acc.feed(chunk)
    finally:
        close_fn = getattr(chunks, "close", None) or getattr(chunks, "aclose", None)
        if callable(close_fn):
            try:
                result = close_fn()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
    return acc.finish()


async def _acreate_with_stream(
    client: Any,
    kwargs: Dict[str, Any],
    task: Optional[str] = None,
) -> Any:
    """Async chat.completions.create() for stream-only providers.

    Sends ``stream=True`` and aggregates the async chunk stream into a
    complete response (credit @kudi88, PR #60686 — async contract fixed to
    ``async for`` and tool-call deltas preserved per sweeper review).
    """
    total_ceiling = _aux_stream_total_ceiling(kwargs.get("timeout"))
    stream_kwargs = dict(kwargs)
    stream_kwargs["stream"] = True
    stream_kwargs["stream_options"] = {"include_usage": True}
    chunks = await client.chat.completions.create(**stream_kwargs)
    # Defensive: shims may hand back a complete response despite stream=True.
    if hasattr(chunks, "choices"):
        return chunks
    return await _aggregate_chat_stream_async(
        chunks, model=str(kwargs.get("model") or ""), total_ceiling=total_ceiling,
    )


def _release_sync_semaphore_after_stream(
    stream: Any, semaphore: threading.BoundedSemaphore,
):
    """Release a permit only after a streaming response is consumed or closed."""
    try:
        yield from stream
    finally:
        try:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        finally:
            semaphore.release()
