"""Non-installed dispatcher mutation seam for tests only."""
from __future__ import annotations


def dispatch_once(kb, conn, **kwargs):
    from hermes_cli.dispatcher_authority import AcquireState, acquire_machine_dispatcher

    acquired = acquire_machine_dispatcher("pytest")
    assert acquired.state is AcquireState.ACQUIRED, acquired
    assert acquired.lease is not None
    with acquired.lease:
        return kb.dispatch_once(acquired.lease, conn, **kwargs)


def outbox_call(fn, *args, **kwargs):
    from hermes_cli.dispatcher_authority import AcquireState, acquire_machine_dispatcher

    acquired = acquire_machine_dispatcher("pytest-outbox")
    assert acquired.state is AcquireState.ACQUIRED, acquired
    assert acquired.lease is not None
    with acquired.lease:
        return fn(acquired.lease, *args, **kwargs)


async def outbox_async_call(fn, *args, **kwargs):
    from hermes_cli.dispatcher_authority import AcquireState, acquire_machine_dispatcher

    acquired = acquire_machine_dispatcher("pytest-outbox-async")
    assert acquired.state is AcquireState.ACQUIRED, acquired
    assert acquired.lease is not None
    with acquired.lease:
        return await fn(acquired.lease, *args, **kwargs)


def spawn_worker(kb, task, workspace, **kwargs):
    from hermes_cli.dispatcher_authority import AcquireState, acquire_machine_dispatcher

    acquired = acquire_machine_dispatcher("pytest-spawn")
    assert acquired.state is AcquireState.ACQUIRED, acquired
    assert acquired.lease is not None
    original = kb._resolve_effective_worker_pins
    from hermes_cli import worker_scope
    original_scope = worker_scope.build_scoped_worker_command
    kb._resolve_effective_worker_pins = lambda home, item: (
        item.model_override or "test-model",
        item.provider_override or "test-provider",
        kb._resolve_worker_cli_toolsets(home) or ["terminal"],
    )
    worker_scope.build_scoped_worker_command = (
        lambda command, **_kwargs: list(command)
    )
    try:
        with acquired.lease:
            return kb._default_spawn(acquired.lease, task, workspace, **kwargs)
    finally:
        kb._resolve_effective_worker_pins = original
        worker_scope.build_scoped_worker_command = original_scope
