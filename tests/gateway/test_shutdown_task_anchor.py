"""Regression: the SIGINT/SIGTERM shutdown task must stay strongly referenced.

``shutdown_signal_handler`` in ``gateway/run.py`` finishes by scheduling
``runner.stop()``.  For a long time it did that with a bare
``asyncio.create_task(...)`` and threw the handle away.  The event loop keeps
only a weak reference to a task, so a still-pending task can be
garbage-collected mid-flight — which is exactly why the *other* signal path
(``request_restart``, SIGUSR1) holds ``self._restart_task`` and says so in a
comment.

If the shutdown task is collected before ``stop()`` reaches
``self._stop_task = asyncio.create_task(_stop_impl())``, no teardown coroutine
is ever created: the gateway simply keeps running until systemd's
``TimeoutStopSec`` escalates to SIGKILL, so in-flight turns are never drained
and sessions are never finalized.

``shutdown_signal_handler`` is a closure built inside the gateway start path and
cannot be imported, so the three structural invariants below are asserted by
parsing ``gateway/run.py`` — the same technique
``test_adapter_connect_is_reconnect_contract.py`` uses for a contract that also
cannot be reached by importing.  The cancel-sweep invariant *is* reachable and
is exercised behaviourally against a real ``stop()``.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner


RUN_PY = Path(__file__).resolve().parents[2] / "gateway" / "run.py"


def _shutdown_signal_handler_node() -> ast.FunctionDef:
    """The single ``shutdown_signal_handler`` definition in ``gateway/run.py``."""
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "shutdown_signal_handler"
    ]
    assert len(matches) == 1, (
        "expected exactly one shutdown_signal_handler in gateway/run.py, found "
        f"{len(matches)} — this test needs updating if the handler moved or was "
        "renamed"
    )
    return matches[0]


def _is_create_task(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_task"
    )


def test_gateway_runner_declares_a_shutdown_task_slot():
    """The anchor needs a class-level default so bare runners inherit ``None``.

    Shutdown-path tests build runners via ``object.__new__`` (``stop()`` even
    carries a getattr-guard for them), so the attribute has to exist on the
    class the way ``_stop_task`` and ``_restart_task`` do.
    """
    assert GatewayRunner._stop_task is None
    assert GatewayRunner._restart_task is None
    assert GatewayRunner._shutdown_task is None


def test_shutdown_handler_never_discards_a_created_task():
    """No ``asyncio.create_task(...)`` in the handler may be a bare statement."""
    handler = _shutdown_signal_handler_node()
    discarded = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Expr) and _is_create_task(node.value)
    ]
    assert discarded == [], (
        "shutdown_signal_handler discards the result of asyncio.create_task(); "
        "the event loop holds only a weak reference, so the task can be "
        "garbage-collected before stop() creates _stop_impl and the gateway "
        "then never shuts down"
    )


def test_shutdown_handler_anchors_its_task_on_the_runner():
    """The created task is stored in ``runner._shutdown_task``."""
    handler = _shutdown_signal_handler_node()
    anchored = [
        target.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Assign) and _is_create_task(node.value)
        for target in node.targets
        if isinstance(target, ast.Attribute)
    ]
    assert anchored == ["_shutdown_task"], (
        "expected the shutdown task to be anchored on the runner as "
        f"_shutdown_task, found {anchored!r}"
    )


def test_shutdown_anchor_is_only_repointed_when_the_previous_task_is_done():
    """A second signal must not overwrite the handle to the live teardown.

    The handler runs more than once in ordinary operation (a second Ctrl+C, a
    SIGINT followed by the service manager's SIGTERM, or the planned-stop
    watcher thread racing a real signal via ``call_soon_threadsafe``).  An
    unconditional assignment would drop the only strong reference to the task
    that is actually shutting the gateway down.
    """
    handler = _shutdown_signal_handler_node()
    guarded = False
    for node in ast.walk(handler):
        if not isinstance(node, ast.If):
            continue
        if not any(
            isinstance(inner, ast.Assign) and _is_create_task(inner.value)
            for inner in ast.walk(node)
        ):
            continue
        if any(
            isinstance(probe, ast.Attribute) and probe.attr == "done"
            for probe in ast.walk(node.test)
        ):
            guarded = True
    assert guarded, (
        "the _shutdown_task assignment is not guarded by a done() check, so a "
        "repeat signal replaces the handle to the still-running shutdown task"
    )


@pytest.mark.asyncio
async def test_stop_does_not_cancel_the_anchored_shutdown_task():
    """``_stop_impl``'s cancel sweep must skip ``_shutdown_task``.

    The anchored task is parked in ``await self._stop_task`` while the sweep
    runs, so cancelling it would push ``CancelledError`` into the very
    ``_stop_impl`` doing the cancelling.  This is why the shutdown task cannot
    simply be parked in ``_background_tasks`` — the same reason ``_stop_task``
    and ``_restart_task`` are already exempt.
    """
    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.0
    adapter.disconnect = AsyncMock()

    async def _park() -> None:
        await asyncio.Event().wait()

    shutdown_task = asyncio.create_task(_park())
    control_task = asyncio.create_task(_park())
    await asyncio.sleep(0)

    runner._shutdown_task = shutdown_task
    runner._background_tasks = {shutdown_task, control_task}

    try:
        with (
            patch("gateway.status.remove_pid_file"),
            patch("gateway.status.write_runtime_status"),
            patch("agent.auxiliary_client.shutdown_cached_clients"),
        ):
            await runner.stop()

        for _ in range(10):
            if control_task.done():
                break
            await asyncio.sleep(0)

        assert control_task.cancelled() is True, (
            "an ordinary background task should still be swept by _stop_impl"
        )
        assert shutdown_task.done() is False, (
            "_stop_impl cancelled the shutdown task, which is awaiting the very "
            "_stop_task that runs this sweep"
        )
    finally:
        for task in (shutdown_task, control_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
