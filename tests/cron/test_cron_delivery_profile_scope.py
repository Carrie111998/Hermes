"""Cron delivery must keep the profile scope across the worker thread (#100489).

The multiplex cron ticker scopes each profile with
``set_hermes_home_override(home)`` — a ContextVar. ``_deliver_result``'s
standalone send path submits into a fresh ``ThreadPoolExecutor``, and
ContextVars do NOT propagate into new threads, so ``_send_to_platform`` ->
``load_gateway_config()`` resolved ``get_hermes_home()`` back to the
process-level ``HERMES_HOME`` (the default profile) and delivered a secondary
profile's job with the DEFAULT profile's bot token.
"""

import concurrent.futures
import contextvars
import inspect
import threading

import pytest

_scope = contextvars.ContextVar("probe_home_override", default="DEFAULT-PROFILE")


def _read_scope():
    return _scope.get()


def _submit_bare():
    """The pre-fix shape: submit straight into a fresh pool thread."""
    _scope.set("link-ss")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(_read_scope).result(timeout=5)
    finally:
        pool.shutdown(wait=False)


def _submit_with_context():
    """The fix: copy the caller's context into the worker thread."""
    _scope.set("link-ss")
    ctx = contextvars.copy_context()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(ctx.run, _read_scope).result(timeout=5)
    finally:
        pool.shutdown(wait=False)


def _in_fresh_thread(fn):
    """Run fn in its own thread so ContextVar sets don't leak between cases."""
    box = {}
    t = threading.Thread(target=lambda: box.__setitem__("v", fn()))
    t.start()
    t.join(10)
    return box.get("v")


class TestContextVarThreadSemantics:
    """Pin the mechanism itself, so the reason for the fix stays documented."""

    def test_bare_submit_loses_the_profile_scope(self):
        assert _in_fresh_thread(_submit_bare) == "DEFAULT-PROFILE"

    def test_copy_context_preserves_the_profile_scope(self):
        assert _in_fresh_thread(_submit_with_context) == "link-ss"


class TestDeliveryUsesCopiedContext:
    """The real call site must use the copy_context form."""

    def test_deliver_result_copies_context_into_the_send_thread(self):
        from cron import scheduler

        source = inspect.getsource(scheduler._deliver_result)
        assert "contextvars.copy_context()" in source, (
            "the standalone send path must copy the caller's context so the "
            "multiplex profile scope survives the worker thread (#100489)"
        )
        # The submit must go through the copied context, not bare asyncio.run.
        assert "submit(_delivery_context.run, asyncio.run" in source

    def test_no_bare_asyncio_run_submit_remains(self):
        from cron import scheduler

        source = inspect.getsource(scheduler._deliver_result)
        assert "submit(asyncio.run," not in source, (
            "a bare submit would silently drop the profile scope again"
        )

    def test_context_copy_precedes_the_submit(self):
        from cron import scheduler

        source = inspect.getsource(scheduler._deliver_result)
        copy_at = source.index("contextvars.copy_context()")
        submit_at = source.index("submit(_delivery_context.run")
        assert copy_at < submit_at
