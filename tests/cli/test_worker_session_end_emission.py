"""Kanban worker hard-exit session-end emission (HM2).

A kanban worker session must emit the memory-provider ``on_session_end``
lifecycle event when it finishes, exactly once per session, so memory
providers (e.g. ``memori_byodb``, which writes only from that hook) ingest the
worker's last turns.

The dispatcher spawns workers as ``hermes ... chat -q`` subprocesses. The
single-query ``try:``/``finally:`` in ``main()`` ends with ``finally:
_finalize_single_query`` → ``_run_cleanup`` → ``shutdown_memory_provider`` →
providers' ``on_session_end``. ``sys.exit`` raises SystemExit so that
``finally:`` ALWAYS runs on a normal exit — that is base behaviour.

A prior revision added an explicit ``_finalize_single_query`` call just before
``sys.exit`` on the ``-Q`` path. It was redundant (the ``finally:`` already
covers it) AND missed the exits that actually skip the ``finally:`` — the
kanban ``os._exit(0)`` signal handler and the exit watchdog. Both terminate
the process directly, so no ``finally:``/``atexit`` ever runs.

This suite locks in the fix: a single shared helper, ``_emit_session_end_for_exit``,
is called immediately before each hard ``os._exit`` (signal handler + watchdog).
It flushes the memory provider via the same ``shutdown_memory_provider`` path the
``finally:`` reaches, guarded by ``_cleanup_done`` so it is exactly-once, and
fully try/except-wrapped so a raising provider can never block the exit.

Tests:
  - the signal-handler path emits exactly once before ``os._exit`` (wiring test
    through the real ``_signal_handler_q`` closure + helper unit test);
  - the watchdog path emits exactly once (wiring test through the real
    ``_watchdog`` closure + helper unit test);
  - a raising provider does not prevent process exit (helper unit test);
  - no double-emission when the normal ``finally:`` also runs (guard test).
"""

import os
import signal as _real_signal
import threading
from types import SimpleNamespace
from unittest.mock import patch

import cli as cli_mod
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class _RecordingProvider(MemoryProvider):
    """A memory provider that records its lifecycle calls."""

    def __init__(self, log, *, raise_on_end=False):
        self._log = log
        self._raise = raise_on_end

    @property
    def name(self):
        return "recording"

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self._log.append("initialize")

    def on_session_end(self, messages):
        self._log.append(f"on_session_end:{len(messages)}")
        if self._raise:
            raise RuntimeError("provider on_session_end boom")

    def shutdown_all(self):
        self._log.append("shutdown_all")

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        return "{}"


def _build_fake_agent(log, *, raise_on_end=False):
    """A fake agent whose MemoryManager is wired to a _RecordingProvider.

    A successful emission actually exercises the provider's on_session_end,
    so these tests verify real emission, not just that a stub was called.
    """
    mm = MemoryManager()
    mm._providers = [_RecordingProvider(log, raise_on_end=raise_on_end)]
    agent = SimpleNamespace(
        session_id="worker-session",
        platform="cli",
        model="probe",
        _session_messages=[{"role": "user", "content": "hi"}],
        _memory_manager=mm,
    )

    def shutdown(msgs=None):
        log.append("shutdown_memory_provider")
        mm.on_session_end(msgs if msgs is not None else [])
        mm.shutdown_all()

    agent.shutdown_memory_provider = shutdown
    return agent


def _count_ends(log):
    return sum(1 for c in log if c.startswith("on_session_end"))


def _build_fake_cli(log, *, fail=False, raise_on_end=False):
    """A full FakeCLI that lets ``main()`` run the single-query path to
    completion (used by the signal-handler wiring test)."""
    agent = _build_fake_agent(log, raise_on_end=raise_on_end)

    def _chat(query, images=None):
        log.append("chat")
        return "done"

    cli = SimpleNamespace(
        console=SimpleNamespace(print=lambda *a, **kw: None),
        session_id="worker-session",
        conversation_history=[],
        agent=agent,
        chat=_chat,
        _claim_active_session=lambda surface, *, stderr=False: True,
        _show_security_advisories=lambda: None,
        _print_exit_summary=lambda clear_screen=True: None,
        _release_active_session=lambda: log.append("release_active_session"),
        _ensure_runtime_credentials=lambda: True,
        _init_agent=lambda **kw: True,
        _resolve_turn_agent_config=lambda q: {
            "signature": "s", "model": None, "runtime": None, "request_overrides": None,
        },
        _active_agent_route_signature="s",
    )
    return cli


def _patch_fake_cli(fake_cli):
    """Point the cli module at the fake and neutralise atexit + helpers."""
    __import__("sys").modules["cli"].HermesCLI = lambda **kw: fake_cli
    cli_mod.atexit.register = lambda *a, **kw: None
    cli_mod._active_agent_ref = fake_cli.agent
    cli_mod._cleanup_done = False
    cli_mod._single_query_finalize_attempted_session_ids = set()


def _unpatch_fake_cli():
    cli_mod._active_agent_ref = None
    cli_mod._cleanup_done = False
    cli_mod._single_query_finalize_attempted_session_ids = set()
    __import__("sys").modules["cli"].HermesCLI = cli_mod.HermesCLI


def _emit(log, *, cli=None, raise_on_end=False):
    """Drive the shared hard-exit helper, wiring _active_agent_ref like the
    real worker does (hermes_cli.cli_agent_setup_mixin sets it on init)."""
    agent = _build_fake_agent(log, raise_on_end=raise_on_end)
    if cli is None:
        cli = SimpleNamespace(agent=agent)
    cli_mod._active_agent_ref = agent
    cli_mod._cleanup_done = False
    try:
        cli_mod._emit_session_end_for_exit(cli)
    finally:
        cli_mod._active_agent_ref = None
        cli_mod._cleanup_done = False


# ---------------------------------------------------------------------------
# Shared helper unit tests
# ---------------------------------------------------------------------------

def test_signal_handler_shape_emits_exactly_once():
    """_emit_session_end_for_exit(cli) — the shape the signal handler calls —
    emits the provider on_session_end exactly once, with the real transcript."""
    log = []
    _emit(log, cli=SimpleNamespace(agent=_build_fake_agent(log)))
    assert _count_ends(log) == 1
    assert "on_session_end:1" in log  # forwarded the real transcript
    assert log.count("shutdown_memory_provider") == 1


def test_watchdog_shape_emits_exactly_once():
    """_emit_session_end_for_exit() with no cli — the shape the watchdog calls,
    resolving the agent via _active_agent_ref — emits exactly once."""
    log = []
    _emit(log)  # cli defaults to None → uses _active_agent_ref
    assert _count_ends(log) == 1
    assert log.count("shutdown_memory_provider") == 1


def test_no_double_emission_after_normal_finally():
    """Once _run_cleanup (the normal finally: path) has run, _cleanup_done is
    set and a later hard-exit emission is a no-op — no double-emission."""
    log = []
    agent = _build_fake_agent(log)
    cli = SimpleNamespace(agent=agent)
    cli_mod._active_agent_ref = agent
    cli_mod._cleanup_done = False
    try:
        cli_mod._run_cleanup()            # normal finally: flushes the provider
        cli_mod._emit_session_end_for_exit(cli)  # hard-exit emission → no-op
    finally:
        cli_mod._active_agent_ref = None
        cli_mod._cleanup_done = False
    assert _count_ends(log) == 1
    assert log.count("shutdown_memory_provider") == 1


def test_no_double_emission_on_repeated_hard_exit():
    """The helper is exactly-once even when called repeatedly (two signals /
    watchdog + signal both firing): the second call is a no-op."""
    log = []
    agent = _build_fake_agent(log)
    cli = SimpleNamespace(agent=agent)
    cli_mod._active_agent_ref = agent
    cli_mod._cleanup_done = False
    try:
        cli_mod._emit_session_end_for_exit(cli)
        cli_mod._emit_session_end_for_exit(cli)
        cli_mod._emit_session_end_for_exit()  # also no-op via _active_agent_ref
    finally:
        cli_mod._active_agent_ref = None
        cli_mod._cleanup_done = False
    assert _count_ends(log) == 1
    assert log.count("shutdown_memory_provider") == 1


def test_raising_provider_does_not_prevent_exit():
    """A memory provider on_session_end that raises is swallowed — the helper
    returns normally (does not raise), so the following os._exit is never
    prevented. Logs a warning and continues."""
    log = []
    # Must not raise out of _emit — if it does, the test fails here.
    _emit(log, raise_on_end=True)
    assert _count_ends(log) == 1  # the provider was still called
    assert log.count("shutdown_memory_provider") == 1


def test_no_agent_is_safe_noop():
    """No active agent → the helper is a safe no-op (never raises)."""
    cli_mod._cleanup_done = False
    cli_mod._active_agent_ref = None
    try:
        cli_mod._emit_session_end_for_exit()  # must not raise
        cli_mod._emit_session_end_for_exit(SimpleNamespace())  # clause w/o agent
    finally:
        cli_mod._cleanup_done = False


# ---------------------------------------------------------------------------
# Wiring tests through the real closures
# ---------------------------------------------------------------------------

def test_signal_handler_emits_before_os_exit():
    """The real ``_signal_handler_q`` kanban branch calls the shared helper
    before ``os._exit``. We capture the handler from ``main()``'s signal
    registration (aborting ``main`` right after), then invoke the captured
    closure — exactly as the OS would on SIGTERM — with ``os._exit`` and
    ``signal.signal`` still patched, and assert the emission fired precisely
    once and that ``os._exit(0)`` was reached."""
    log = []
    fake = _build_fake_cli(log)
    _patch_fake_cli(fake)
    captured = {"count": 0}

    def _fake_signal_signal(signum, handler):
        captured["count"] += 1
        captured["handler"] = handler
        if captured["count"] == 1:
            # The FIRST registration is the real _signal_handler_q via
            # main(); abort main now that it's installed — we only need the
            # closure. Subsequent registrations (SIGTERM/SIGHUP/and the
            # handler's own SIGALRM) just record and fall through.
            raise SystemExit()

    exit_calls = []

    def _fake_os_exit(code):
        exit_calls.append(code)
        raise SystemExit(code)  # don't kill the test worker

    os.environ["HERMES_KANBAN_TASK"] = "t_test"
    os.environ["HERMES_SIGTERM_GRACE"] = "0"  # skip the in-handler sleep
    try:
        # Keep os._exit AND signal.signal patched across BOTH main() and the
        # handler invocation so the handler's os._exit(0) and its managed
        # SIGALRM deadman hit the fakes, never the real process.
        with patch.object(cli_mod.os, "_exit", _fake_os_exit), \
             patch.object(_real_signal, "signal", _fake_signal_signal):
            try:
                cli_mod.main(query="q", quiet=True, toolsets="terminal")
            except SystemExit:
                pass
            handler = captured.get("handler")
            assert handler is not None, "signal handler was not installed"
            # Invoke the real closure exactly as the OS would on SIGTERM.
            try:
                handler(_real_signal.SIGTERM, None)
            except SystemExit:
                pass
            # Cancel the 2s SIGALRM the handler armed, so it cannot fire after
            # the test subprocess unwinds and kill the next test / worker.
            try:
                _real_signal.alarm(0)
            except Exception:
                pass
            assert _count_ends(log) == 1
            assert log.count("shutdown_memory_provider") == 1
            # The kanban branch was reached: after emitting, the handler
            # called os._exit(0), which our fake translated into SystemExit(0).
            assert exit_calls == [0]
    finally:
        _unpatch_fake_cli()
        del os.environ["HERMES_KANBAN_TASK"]
        del os.environ["HERMES_SIGTERM_GRACE"]
        # Restore the process's real signal handlers (main() replaced them).
        _real_signal.signal(_real_signal.SIGINT, _real_signal.default_int_handler)
        try:
            _real_signal.signal(_real_signal.SIGTERM, _real_signal.SIG_DFL)
        except Exception:
            pass
        try:
            _real_signal.signal(_real_signal.SIGHUP, _real_signal.SIG_DFL)
        except Exception:
            pass


def test_watchdog_emits_before_os_exit():
    """The real exit-watchdog ``_watchdog`` closure calls the shared helper
    before ``os._exit``. We capture the watchdog thread's target from a real
    ``_arm_exit_watchdog`` call and run the inner closure — with patched
    ``os._exit`` — asserting the emission fired exactly once before it."""
    log = []
    agent = _build_fake_agent(log)
    captured = {}

    def _fake_thread_start(self):
        captured["target"] = self._target

    exit_calls = []

    def _fake_os_exit(code):
        exit_calls.append(code)
        raise SystemExit(code)  # don't kill the test worker

    cli_mod._active_agent_ref = agent
    cli_mod._cleanup_done = False
    had_pytest_env = "PYTEST_CURRENT_TEST" in os.environ
    pytest_val = os.environ.get("PYTEST_CURRENT_TEST")
    # _arm_exit_watchdog early-returns under PYTEST_CURRENT_TEST (so the real
    # os._exit backstop never kills a test worker). We want the watchdog thread
    # itself, so clear it for the arming call; os._exit stays patched to the
    # fake, so even if the thread were to actually run it could not kill us.
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    try:
        # Keep os._exit patched across arming AND running the closure so the
        # inner os._exit(0) hits the fake, never the real process. Also no-op
        # time.sleep so running the closure directly doesn't sleep 9999s.
        with patch.object(cli_mod.os, "_exit", _fake_os_exit), \
             patch.object(cli_mod.time, "sleep", lambda *a, **k: None), \
             patch.object(threading.Thread, "start", _fake_thread_start):
            cli_mod._arm_exit_watchdog(timeout_s=9999)
            target = captured.get("target")
            assert target is not None, "watchdog thread was not started"
            # Run the inner watchdog closure directly (skip the 9999s sleep).
            try:
                target()
            except SystemExit:
                pass
            assert _count_ends(log) == 1
            assert log.count("shutdown_memory_provider") == 1
            assert exit_calls == [0]
    finally:
        cli_mod._active_agent_ref = None
        cli_mod._cleanup_done = False
        cli_mod._signal_watchdog_armed = False
        if had_pytest_env and pytest_val is not None:
            os.environ["PYTEST_CURRENT_TEST"] = pytest_val
        else:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
