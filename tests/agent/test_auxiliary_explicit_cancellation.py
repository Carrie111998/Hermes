"""Deterministic cross-thread cancellation tests for compression aux transports."""

from __future__ import annotations

import contextvars
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from agent import auxiliary_client as aux


class _BlockingStream:
    def __init__(self, started: threading.Event) -> None:
        self.started = started
        self.closed = threading.Event()

    def __iter__(self):
        self.started.set()
        self.closed.wait(timeout=5)
        raise RuntimeError("transport closed")

    def close(self) -> None:
        self.closed.set()

    def get_final_message(self) -> Any:
        self.started.set()
        self.closed.wait(timeout=5)
        raise RuntimeError("transport closed")


class _GenericCompletions:
    def __init__(self, stream: _BlockingStream) -> None:
        self.stream = stream

    def create(self, **_kwargs: Any) -> _BlockingStream:
        return self.stream


class _GenericClient:
    def __init__(self, stream: _BlockingStream) -> None:
        self.chat = SimpleNamespace(completions=_GenericCompletions(stream))
        self.stream = stream
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()
        self.stream.close()


class _CodexResponses:
    def __init__(self, stream: _BlockingStream) -> None:
        self.stream = stream

    def create(self, **_kwargs: Any) -> _BlockingStream:
        return self.stream


class _CodexRealClient:
    def __init__(self, stream: _BlockingStream) -> None:
        self.responses = _CodexResponses(stream)
        self.api_key = "test"
        self.base_url = "https://example.test/codex"
        self.stream = stream
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()
        self.stream.close()


class _AnthropicStreamContext:
    def __init__(self, stream: _BlockingStream) -> None:
        self.stream = stream

    def __enter__(self) -> _BlockingStream:
        return self.stream

    def __exit__(self, *_args: Any) -> None:
        self.stream.close()


class _AnthropicMessages:
    def __init__(self, stream: _BlockingStream) -> None:
        self.stream_obj = stream

    def stream(self, **_kwargs: Any) -> _AnthropicStreamContext:
        return _AnthropicStreamContext(self.stream_obj)


class _AnthropicRealClient:
    def __init__(self, stream: _BlockingStream) -> None:
        self.messages = _AnthropicMessages(stream)
        self.stream = stream
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()
        self.stream.close()


class _BedrockRuntimeClient:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release
        self.closed = threading.Event()

    def converse(self, **_kwargs: Any) -> dict[str, Any]:
        self.started.set()
        self.release.wait(timeout=5)
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": "cancelled response"}],
                }
            },
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            "stopReason": "end_turn",
        }

    def close(self) -> None:
        self.closed.set()


def _cancel_silent_request(
    client: Any,
    started: threading.Event,
    invoke: Callable[[Any], Any],
) -> tuple[BaseException, float]:
    cancel_event = threading.Event()
    result: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            with aux.aux_interrupt_protection(cancel_event=cancel_event):
                invoke(client)
        except BaseException as exc:
            result["exc"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    assert started.wait(timeout=1), "request never entered its silent transport"
    cancelled_at = time.monotonic()
    cancel_event.set()
    worker.join(timeout=1)
    elapsed = time.monotonic() - cancelled_at
    assert not worker.is_alive(), "explicit cancellation did not wake the silent request"
    return result["exc"], elapsed


def _invoke_generic(client: Any) -> Any:
    return aux._relay_sync_completion(
        client,
        {"model": "test", "messages": [], "timeout": 30},
        create=lambda request: aux._create_with_progress(
            client, request, "compression", force_stream=True
        ),
    )


def test_protected_silent_provider_is_isolated_and_raises_frozen_explicit_cancel() -> None:
    started = threading.Event()
    stream = _BlockingStream(started)
    client = _GenericClient(stream)

    exc, elapsed = _cancel_silent_request(client, started, _invoke_generic)

    assert isinstance(exc, aux.AuxiliaryExplicitCancellation)
    assert exc.cause == "explicit_host_cancel"
    assert not client.closed.is_set()
    assert elapsed < 0.75
    stream.close()  # release the bounded daemon provider worker


def test_codex_silent_stream_is_isolated_without_closing_shared_client() -> None:
    started = threading.Event()
    stream = _BlockingStream(started)
    real_client = _CodexRealClient(stream)
    client = aux.CodexAuxiliaryClient(real_client, "gpt-test")

    exc, elapsed = _cancel_silent_request(client, started, _invoke_generic)

    assert isinstance(exc, aux.AuxiliaryExplicitCancellation)
    assert not real_client.closed.is_set()
    assert elapsed < 0.75
    stream.close()


def test_cancelled_codex_orphan_timeout_preserves_cached_shared_client() -> None:
    """A cancelled Codex worker's delayed timer owns only its event stream."""
    owner_started = threading.Event()

    class _SilentOwnerStream:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def __iter__(self):
            owner_started.set()
            self.closed.wait(timeout=5)
            raise RuntimeError("owner stream closed")

        def close(self) -> None:
            self.closed.set()

    class _SuccessStream:
        def __iter__(self):
            message = SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="ok")],
            )
            return iter(
                [
                    SimpleNamespace(type="response.output_item.done", item=message),
                    SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            status="completed", id="success", usage=None
                        ),
                    ),
                ]
            )

        def close(self) -> None:
            pass

    owner_stream = _SilentOwnerStream()

    class _SharedResponses:
        def __init__(self, real_client: Any) -> None:
            self.real_client = real_client

        def create(self, **kwargs: Any) -> Any:
            if self.real_client.closed.is_set():
                raise RuntimeError("shared client was closed")
            if kwargs["model"] == "owner":
                return owner_stream
            return _SuccessStream()

    class _SharedRealClient:
        def __init__(self) -> None:
            self.closed = threading.Event()
            self.api_key = "test"
            self.base_url = "https://example.test/codex"
            self.responses = _SharedResponses(self)

        def close(self) -> None:
            self.closed.set()
            owner_stream.close()

    real_client = _SharedRealClient()
    wrapper = aux.CodexAuxiliaryClient(real_client, "gpt-test")
    cache_key = ("openai-codex", False, None, None, None)
    cancel_event = threading.Event()
    owner_outcome: dict[str, BaseException] = {}

    def _run_owner() -> None:
        try:
            with aux.aux_interrupt_protection(cancel_event=cancel_event):
                aux._relay_sync_completion(
                    wrapper,
                    {"model": "owner", "messages": [], "timeout": 0.12},
                )
        except BaseException as exc:
            owner_outcome["exc"] = exc

    with aux._client_cache_lock:
        aux._client_cache.clear()
        aux._client_cache[cache_key] = (wrapper, "gpt-test", None)
    owner = threading.Thread(target=_run_owner, daemon=True)
    try:
        owner.start()
        assert owner_started.wait(timeout=1)
        cancel_event.set()
        owner.join(timeout=1)
        assert not owner.is_alive()
        assert isinstance(owner_outcome["exc"], aux.AuxiliaryExplicitCancellation)
        # A real frontend clears the reusable host Event when the next turn
        # starts. The orphan must retain a frozen per-attempt cancellation cause.
        cancel_event.clear()

        # A second user can use the shared client while the cancelled provider
        # worker is still orphaned and its total-timeout timer is still armed.
        assert not owner_stream.closed.is_set()
        concurrent = aux._relay_sync_completion(
            wrapper,
            {"model": "concurrent", "messages": [], "timeout": 1},
        )
        assert concurrent.choices[0].message.content == "ok"

        # Let the orphan's real adapter timer fire. It may close the attempt's
        # event stream to wake that worker, but never the process-shared client.
        assert owner_stream.closed.wait(timeout=1)
        time.sleep(0.03)
        assert not real_client.closed.is_set()
        with aux._client_cache_lock:
            assert aux._client_cache[cache_key][0] is wrapper

        successive = aux._relay_sync_completion(
            wrapper,
            {"model": "successive", "messages": [], "timeout": 1},
        )
        assert successive.choices[0].message.content == "ok"
    finally:
        owner_stream.close()
        with aux._client_cache_lock:
            aux._client_cache.clear()


@pytest.mark.parametrize("winner", ["timeout", "cancel"])
def test_codex_timeout_and_explicit_cancel_have_one_linearized_outcome(
    winner: str,
) -> None:
    """Timeout and explicit cancel can never produce a mixed owner/cleanup result."""
    timer_read_started = threading.Event()
    allow_timer_read_return = threading.Event()
    request_cancelled = threading.Event()
    stream_started = threading.Event()

    class _RacingCancelSource:
        def is_set(self) -> bool:
            if winner == "timeout" and threading.current_thread().name.startswith(
                "Thread-"
            ):
                # Take the timer's false snapshot, then hold it at the exact seam
                # where the historical implementation could race owner polling.
                was_set = request_cancelled.is_set()
                timer_read_started.set()
                assert allow_timer_read_return.wait(timeout=1)
                return was_set
            return request_cancelled.is_set()

    class _SilentStream:
        def __init__(self) -> None:
            self.closed = threading.Event()

        def __iter__(self):
            stream_started.set()
            self.closed.wait(timeout=5)
            raise RuntimeError("stream closed")

        def close(self) -> None:
            self.closed.set()

    stream = _SilentStream()

    class _RealClient:
        def __init__(self) -> None:
            self.api_key = "test"
            self.base_url = "https://example.test/codex"
            self.responses = SimpleNamespace(create=lambda **_kwargs: stream)
            self.closed = threading.Event()

        def close(self) -> None:
            self.closed.set()
            stream.close()

    real_client: Any = _RealClient()
    wrapper = aux.CodexAuxiliaryClient(real_client, "gpt-test")
    owner_outcome: dict[str, BaseException] = {}

    def _run_owner() -> None:
        try:
            with aux.aux_interrupt_protection(cancel_event=_RacingCancelSource()):
                aux._relay_sync_completion(
                    wrapper,
                    {"model": "owner", "messages": [], "timeout": 0.08},
                )
        except BaseException as exc:
            owner_outcome["exc"] = exc

    owner = threading.Thread(target=_run_owner, name="race-owner", daemon=True)
    owner.start()
    assert stream_started.wait(timeout=1)
    if winner == "timeout":
        assert timer_read_started.wait(timeout=1)
        request_cancelled.set()
        allow_timer_read_return.set()
    else:
        request_cancelled.set()
    owner.join(timeout=1)

    assert not owner.is_alive()
    if winner == "timeout":
        assert real_client.closed.is_set()
        assert isinstance(owner_outcome["exc"], TimeoutError)
        assert not isinstance(owner_outcome["exc"], aux.AuxiliaryExplicitCancellation)
    else:
        assert isinstance(owner_outcome["exc"], aux.AuxiliaryExplicitCancellation)
        assert stream.closed.wait(timeout=1), "cancelled timer did not wake its stream"
        assert not real_client.closed.is_set()


def test_anthropic_silent_stream_is_isolated_without_closing_shared_client() -> None:
    started = threading.Event()
    stream = _BlockingStream(started)
    real_client = _AnthropicRealClient(stream)
    client = aux.AnthropicAuxiliaryClient(
        real_client,
        "claude-test",
        "test-key",
        "https://api.anthropic.test",
    )

    exc, elapsed = _cancel_silent_request(client, started, _invoke_generic)

    assert isinstance(exc, aux.AuxiliaryExplicitCancellation)
    assert not real_client.closed.is_set()
    assert elapsed < 0.75
    stream.close()


def test_cancelled_attempt_does_not_close_or_fail_concurrent_shared_client_call(
    monkeypatch,
) -> None:
    a_started = threading.Event()
    a_release = threading.Event()
    b_started = threading.Event()
    b_release = threading.Event()
    closed = threading.Event()

    class _SharedCompletions:
        def create(self, **kwargs: Any) -> Any:
            if kwargs["model"] == "session-a":
                a_started.set()
                a_release.wait(timeout=5)
            else:
                b_started.set()
                b_release.wait(timeout=5)
            if closed.is_set():
                raise RuntimeError("shared client was closed")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_SharedCompletions()),
        close=lambda: closed.set(),
    )
    cancel_event = threading.Event()
    outcomes: dict[str, Any] = {}
    evictions: list[Any] = []
    monkeypatch.setattr(
        aux, "_evict_cached_client_instance", lambda value: evictions.append(value)
    )

    def _session_a() -> None:
        try:
            with aux.aux_interrupt_protection(cancel_event=cancel_event):
                aux._relay_sync_completion(
                    client, {"model": "session-a", "messages": [], "timeout": 30}
                )
        except BaseException as exc:
            outcomes["a"] = exc

    def _session_b() -> None:
        try:
            outcomes["b"] = aux._relay_sync_completion(
                client, {"model": "session-b", "messages": [], "timeout": 30}
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcomes["b"] = exc

    a_thread = threading.Thread(target=_session_a, daemon=True)
    b_thread = threading.Thread(target=_session_b, daemon=True)
    a_thread.start()
    b_thread.start()
    assert a_started.wait(timeout=1)
    assert b_started.wait(timeout=1)
    cancel_event.set()
    a_thread.join(timeout=1)
    try:
        assert not a_thread.is_alive()
        assert isinstance(outcomes["a"], aux.AuxiliaryExplicitCancellation)
        assert not closed.is_set()
        assert evictions == []
        b_release.set()
        b_thread.join(timeout=1)
        assert not b_thread.is_alive()
        assert not isinstance(outcomes["b"], BaseException)
        assert outcomes["b"].choices[0].message.content == "ok"
    finally:
        a_release.set()
        b_release.set()


def test_bedrock_silent_nonstream_request_is_isolated_without_close_wakeup() -> None:
    from agent.bedrock_adapter import _bedrock_runtime_client_cache, reset_client_cache

    started = threading.Event()
    release = threading.Event()
    runtime_client = _BedrockRuntimeClient(started, release)
    reset_client_cache()
    _bedrock_runtime_client_cache["us-test-1"] = runtime_client
    client = aux.BedrockAuxiliaryClient("us-test-1", "bedrock-test")
    try:
        exc, elapsed = _cancel_silent_request(client, started, _invoke_generic)
    finally:
        release.set()
        reset_client_cache()

    assert isinstance(exc, aux.AuxiliaryExplicitCancellation)
    assert not runtime_client.closed.is_set()
    assert elapsed < 0.75


def test_unprotected_sync_completion_stays_on_calling_thread() -> None:
    caller = threading.get_ident()
    observed: list[int] = []
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: (
                    observed.append(threading.get_ident()),
                    SimpleNamespace(choices=[]),
                )[1]
            )
        )
    )

    aux._relay_sync_completion(client, {"model": "test", "messages": []})

    assert observed == [caller]


def test_isolated_provider_worker_inherits_protection_and_progress_hook() -> None:
    caller = threading.get_ident()
    cancel_event = threading.Event()
    progress: list[str] = []
    observed: dict[str, Any] = {}

    def _create(**_kwargs: Any) -> Any:
        observed["thread"] = threading.get_ident()
        observed["protected"] = aux._aux_interrupt_protected()
        aux._notify_aux_progress()
        return SimpleNamespace(choices=[])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )
    with aux.aux_progress_hook(lambda: progress.append("tick")), aux.aux_interrupt_protection(
        cancel_event=cancel_event
    ):
        aux._relay_sync_completion(client, {"model": "test", "messages": []})

    assert observed["protected"] is True
    assert observed["thread"] != caller
    assert progress == ["tick"]


def test_isolated_provider_worker_inherits_caller_contextvars() -> None:
    from tools.approval import (
        get_current_session_key,
        reset_current_session_key,
        set_current_session_key,
    )

    arbitrary = contextvars.ContextVar("isolated-provider-test", default="missing")
    arbitrary_token = arbitrary.set("caller-value")
    session_token = set_current_session_key("session-from-caller")
    observed: dict[str, str] = {}
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: (
                    observed.update(
                        arbitrary=arbitrary.get(),
                        session_key=get_current_session_key(),
                    ),
                    SimpleNamespace(choices=[]),
                )[1]
            )
        )
    )
    try:
        with aux.aux_interrupt_protection(cancel_event=threading.Event()):
            aux._relay_sync_completion(client, {"model": "test", "messages": []})
    finally:
        reset_current_session_key(session_token)
        arbitrary.reset(arbitrary_token)

    assert observed == {
        "arbitrary": "caller-value",
        "session_key": "session-from-caller",
    }


def test_hard_cancel_wins_when_provider_result_is_published_in_same_race() -> None:
    cancel_event = threading.Event()

    def _create(**_kwargs: Any) -> Any:
        cancel_event.set()
        return SimpleNamespace(choices=[])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )
    with aux.aux_interrupt_protection(cancel_event=cancel_event):
        with pytest.raises(aux.AuxiliaryExplicitCancellation):
            aux._relay_sync_completion(client, {"model": "test", "messages": []})


def test_unrelated_interrupted_error_is_not_reclassified_as_explicit_cancel() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: (_ for _ in ()).throw(
                    InterruptedError("provider syscall interrupted")
                )
            )
        ),
        close=lambda: None,
    )

    with aux.aux_interrupt_protection(cancel_event=threading.Event()):
        with pytest.raises(InterruptedError, match="provider syscall interrupted") as caught:
            aux._relay_sync_completion(client, {"model": "test", "messages": []})

    assert not isinstance(caught.value, aux.AuxiliaryExplicitCancellation)


def test_every_physical_retry_uses_a_fenced_dispatch_wrapper(monkeypatch):
    """A cancel after the dispatch snapshot denies a default relay callback."""
    cancel_event = threading.Event()
    snapshot_taken = threading.Event()
    release_snapshot = threading.Event()
    reads = 0
    reads_lock = threading.Lock()
    provider_calls = []
    outcome = {}

    def _source():
        nonlocal reads
        with reads_lock:
            reads += 1
            current = reads
        if current == 1:
            snapshot_taken.set()
            assert release_snapshot.wait(timeout=1)
            return False
        return cancel_event.is_set()

    def _create(**kwargs):
        provider_calls.append(kwargs)
        return SimpleNamespace(choices=[])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    def _run():
        try:
            with aux.aux_interrupt_protection(cancel_check=_source):
                aux._relay_sync_completion(
                    client,
                    {"model": "retry", "messages": [], "timeout": 1},
                )
        except BaseException as exc:
            outcome["exc"] = exc

    worker = threading.Thread(target=_run, name="retry-dispatch-fence", daemon=True)
    worker.start()
    try:
        assert snapshot_taken.wait(timeout=1)
        cancel_event.set()
        release_snapshot.set()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert isinstance(outcome.get("exc"), aux.AuxiliaryExplicitCancellation)
        assert provider_calls == []
    finally:
        release_snapshot.set()
        worker.join(timeout=1)


@pytest.mark.parametrize(
    "branch",
    ["temperature", "structured_output", "max_tokens", "model_heal"],
)
def test_cancel_before_each_named_retry_branch_sends_no_next_rpc(monkeypatch, branch):
    """Every named retry rung must use the physical dispatch fence."""
    cancel_event = threading.Event()
    calls = []
    first_error = RuntimeError(f"{branch} retry trigger")
    provider = "nous" if branch == "model_heal" else "test"
    base_url = (
        "https://inference-api.nousresearch.com/v1"
        if branch == "model_heal"
        else "https://example.test/v1"
    )

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                # ``active=False`` keeps the first transport exception visible
                # to the retry ladder; the next physical handoff must still
                # consult the token.
                cancel_event.set()
                raise first_error
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="must not be reached")
                    )
                ]
            )

    client = SimpleNamespace(
        base_url=base_url,
        chat=SimpleNamespace(completions=_Completions()),
    )
    monkeypatch.setattr(
        aux, "_get_cached_client", lambda *_args, **_kwargs: (client, "stale-model")
    )
    monkeypatch.setattr(
        aux,
        "_effective_provider_for_client",
        lambda _client, _provider: provider,
    )
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda _task: {})

    call_kwargs = {"temperature": 0.2} if branch == "temperature" else {}
    max_tokens = 32 if branch == "max_tokens" else None
    if branch == "temperature":
        monkeypatch.setattr(
            aux, "_is_unsupported_temperature_error", lambda exc: exc is first_error
        )
    elif branch == "structured_output":
        monkeypatch.setattr(
            aux, "_is_structured_output_rejection", lambda exc: exc is first_error
        )
        monkeypatch.setattr(
            aux, "_without_structured_output_format", lambda request: dict(request)
        )
    elif branch == "model_heal":
        monkeypatch.setattr(
            aux, "_is_model_not_found_error", lambda exc: exc is first_error
        )
        monkeypatch.setattr(
            aux,
            "_refresh_nous_recommended_model",
            lambda **_kwargs: "healed-model",
        )

    with aux.aux_interrupt_protection(
        active=False, cancel_event=cancel_event
    ):
        with pytest.raises(aux.AuxiliaryExplicitCancellation):
            aux._call_llm_impl(
                task="compression",
                provider=provider,
                model="stale-model",
                base_url=base_url,
                api_key="test-key",
                messages=[],
                max_tokens=max_tokens,
                timeout=1,
                **call_kwargs,
            )

    assert len(calls) == 1, f"{branch} admitted a cancelled retry: {calls!r}"


def test_cancellation_during_auxiliary_backoff_makes_retry_inert_and_releases_permit(
    monkeypatch,
):
    """Backoff polling must stop before the next physical RPC."""
    cancel_event = threading.Event()
    first_attempt = threading.Event()
    release_first = threading.Event()
    calls = []
    outcome = {}

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                first_attempt.set()
                raise ConnectionError("connection reset")
            return SimpleNamespace(choices=[])

    client = SimpleNamespace(
        base_url="https://example.test/v1",
        chat=SimpleNamespace(completions=_Completions()),
    )
    monkeypatch.setattr(
        aux,
        "_get_cached_client",
        lambda *_args, **_kwargs: (client, "retry-model"),
    )
    monkeypatch.setattr(aux, "_effective_provider_for_client", lambda *_args: "test")
    monkeypatch.setattr(aux, "_transient_retry_count", lambda: 1)
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 2.0)
    monkeypatch.setattr(
        aux,
        "_get_auxiliary_task_config",
        lambda _task: {"max_concurrency": 1, "transient_retries": 1},
    )
    aux._reset_aux_semaphores()

    def _run():
        try:
            with aux.aux_interrupt_protection(
                active=False, cancel_event=cancel_event
            ):
                aux.call_llm(
                    task="compression",
                    provider="test",
                    model="retry-model",
                    base_url="https://example.test/v1",
                    api_key="test-key",
                    messages=[],
                    timeout=5,
                )
        except BaseException as exc:
            outcome["exc"] = exc

    worker = threading.Thread(target=_run, name="retry-backoff-cancel", daemon=True)
    worker.start()
    try:
        assert first_attempt.wait(timeout=5)
        cancelled_at = time.monotonic()
        cancel_event.set()
        worker.join(timeout=0.75)
        elapsed = time.monotonic() - cancelled_at
        assert not worker.is_alive()
        assert elapsed < 0.75
        assert isinstance(outcome.get("exc"), aux.AuxiliaryExplicitCancellation)
        assert len(calls) == 1
        with aux._aux_sem_lock:
            semaphore = aux._aux_sync_semaphores["compression"][1]
            assert semaphore._value == 1
    finally:
        release_first.set()
        worker.join(timeout=1)
        aux._reset_aux_semaphores()


def test_cancelled_token_registration_runs_cleanup_immediately_outside_lock():
    """Registration after cancellation must invoke cleanup without the lock."""
    token = aux.AuxiliaryCancellationToken(lambda: False)
    token.cancel()
    callback_called = threading.Event()
    callback_observed_unlocked = threading.Event()

    def _cleanup():
        acquired = token._lock.acquire(timeout=0.2)
        if acquired:
            token._lock.release()
            callback_observed_unlocked.set()
        callback_called.set()

    token.register_cancel_callback(_cleanup)
    assert callback_called.is_set()
    assert callback_observed_unlocked.is_set()


def test_cancel_before_final_provider_dispatch_sends_zero_requests(monkeypatch) -> None:
    """Cancellation at the final admission seam must deny the provider call."""
    cancel_event = threading.Event()
    claim_entered = threading.Event()
    release_claim = threading.Event()
    provider_calls: list[dict[str, Any]] = []
    outcome: dict[str, BaseException] = {}

    original_notify = aux._notify_aux_dispatch

    def _gate_before_claim() -> None:
        claim_entered.set()
        assert release_claim.wait(timeout=1)
        original_notify()

    # The production notify is the deterministic seam immediately before the
    # short OPEN -> ADMITTED dispatch claim.  Keeping the gate around the real
    # notify also proves the provider callback is not invoked while a cancel is
    # waiting to win that claim.
    monkeypatch.setattr(aux, "_notify_aux_dispatch", _gate_before_claim)

    def _create(**kwargs: Any) -> Any:
        provider_calls.append(kwargs)
        return SimpleNamespace(choices=[])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )

    def _run() -> None:
        try:
            with aux.aux_interrupt_protection(cancel_event=cancel_event):
                aux._relay_sync_completion(
                    client,
                    {"model": "test", "messages": [], "timeout": 1},
                    create=lambda request: aux._create_with_progress(
                        client, request, "compression", force_stream=True
                    ),
                )
        except BaseException as exc:
            outcome["exc"] = exc

    worker = threading.Thread(target=_run, name="final-dispatch-cancel", daemon=True)
    started = time.monotonic()
    worker.start()
    try:
        assert claim_entered.wait(timeout=1), "dispatch claim seam was not reached"
        cancel_event.set()
        release_claim.set()
        worker.join(timeout=1)
        elapsed = time.monotonic() - started
        assert not worker.is_alive()
        assert elapsed < 0.75
        assert isinstance(outcome.get("exc"), aux.AuxiliaryExplicitCancellation)
        assert provider_calls == []
    finally:
        release_claim.set()
        worker.join(timeout=1)


def test_commit_fence_cancellation_interrupts_aux_semaphore_waiter(monkeypatch) -> None:
    """A fenced waiter exits without stealing or releasing another permit."""
    from agent.conversation_compression import CompressionCommitFence

    first_provider_started = threading.Event()
    release_first_provider = threading.Event()
    second_semaphore_lookup = threading.Event()
    outcomes: dict[str, Any] = {}
    provider_calls: list[str] = []
    lookup_count = 0
    lookup_lock = threading.Lock()

    monkeypatch.setattr(
        aux,
        "_get_auxiliary_task_config",
        lambda _task: {"max_concurrency": 1},
    )
    original_lookup = aux._acquire_sync_aux_semaphore

    def _tracked_lookup(task: Any) -> Any:
        nonlocal lookup_count
        semaphore = original_lookup(task)
        with lookup_lock:
            lookup_count += 1
            if lookup_count == 2:
                second_semaphore_lookup.set()
        return semaphore

    monkeypatch.setattr(aux, "_acquire_sync_aux_semaphore", _tracked_lookup)

    def _fake_impl(**kwargs: Any) -> Any:
        provider_calls.append(str(kwargs.get("model")))
        first_provider_started.set()
        assert release_first_provider.wait(timeout=5)
        return SimpleNamespace(choices=[])

    monkeypatch.setattr(aux, "_call_llm_impl", _fake_impl)
    aux._reset_aux_semaphores()
    semaphore = None

    def _first() -> None:
        try:
            outcomes["first"] = aux.call_llm(
                task="compression",
                provider="test",
                model="first",
                messages=[],
                timeout=1,
            )
        except BaseException as exc:
            outcomes["first"] = exc

    fence = CompressionCommitFence()

    def _second() -> None:
        try:
            with aux.aux_interrupt_protection(cancel_check=lambda: fence.is_cancelled):
                outcomes["second"] = aux.call_llm(
                    task="compression",
                    provider="test",
                    model="second",
                    messages=[],
                    timeout=1,
                )
        except BaseException as exc:
            outcomes["second"] = exc

    first = threading.Thread(target=_first, name="compression-semaphore-owner", daemon=True)
    second = threading.Thread(target=_second, name="compression-semaphore-waiter", daemon=True)
    first.start()
    second_started = False
    try:
        assert first_provider_started.wait(timeout=1)
        second.start()
        assert second_semaphore_lookup.wait(timeout=1)
        second_started = True
        assert fence.cancel_before_commit() is True
        second.join(timeout=0.75)
        assert not second.is_alive(), "cancelled semaphore waiter did not exit"
        assert isinstance(outcomes.get("second"), aux.AuxiliaryExplicitCancellation)
        assert provider_calls == ["first"]
        with aux._aux_sem_lock:
            semaphore = aux._aux_sync_semaphores["compression"][1]
            assert semaphore._value == 0  # first provider still owns it
    finally:
        release_first_provider.set()
        first.join(timeout=2)
        second.join(timeout=2)
        aux._reset_aux_semaphores()
    assert not first.is_alive()
    if second_started:
        assert not second.is_alive()
