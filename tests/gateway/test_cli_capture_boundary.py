"""Concurrent gateway commands must not share process-global stdout.

`contextlib.redirect_stdout` swaps `sys.stdout` for the whole process, not for
the calling thread. Two gateway requests that each capture a CLI handler's
printed output in a worker thread therefore interleave: a reply can absorb
another request's text, a reply can be lost, and output can escape to the real
terminal. The negative control below reproduces all three on demand.

`/project` and `/kanban` both capture those streams, so the boundary is one
shared lock rather than one per command.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_cli import cli_capture
from hermes_cli.cli_capture import captured_streams, cli_output_lock


def _drive(worker, tags=("A", "B", "C", "D")):
    """Run `worker(tag)` in one thread per tag, all released together."""
    gate = threading.Barrier(len(tags))
    threads = [threading.Thread(target=worker, args=(gate, t)) for t in tags]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a worker deadlocked"


# ---------------------------------------------------------------------------
# Negative control — why the boundary exists
# ---------------------------------------------------------------------------

def test_raw_redirect_is_unsafe_across_threads():
    """Documents the defect. Contained: escapes land in a buffer we own."""
    foreign: list[str] = []
    results: dict[str, str] = {}

    def worker(gate, tag):
        out, err = io.StringIO(), io.StringIO()
        gate.wait(timeout=10)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            print(f"{tag}-START")
            time.sleep(0.05)
            print(f"{tag}-END")
            if sys.stdout is not out:
                foreign.append(tag)
        results[tag] = " ".join(out.getvalue().split())

    real_out, real_err = sys.stdout, sys.stderr
    escaped = io.StringIO()
    try:
        sys.stdout = escaped
        _drive(worker)
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    damaged = (
        bool(foreign)
        or any(results[t] != f"{t}-START {t}-END" for t in results)
        or bool(escaped.getvalue().strip())
    )
    assert damaged, (
        "the raw primitive did not misbehave in this run, which would mean the "
        "shared boundary is guarding nothing — investigate before removing it"
    )


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def test_concurrent_captures_stay_complete_and_disjoint():
    foreign: list[str] = []
    results: dict[str, str] = {}

    def worker(gate, tag):
        gate.wait(timeout=10)
        with captured_streams() as (out, err):
            print(f"{tag}-START")
            if sys.stdout is not out:
                foreign.append(f"{tag}@start")
            time.sleep(0.05)
            print(f"{tag}-END", file=sys.stderr)
            if sys.stderr is not err:
                foreign.append(f"{tag}@end")
        results[tag] = " ".join((out.getvalue() + err.getvalue()).split())

    real_out, real_err = sys.stdout, sys.stderr
    escaped = io.StringIO()
    try:
        sys.stdout = escaped
        _drive(worker, tags=tuple("ABCDEFGH"))
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    assert foreign == [], f"a thread saw another thread's stream: {foreign}"
    for tag, text in results.items():
        assert text == f"{tag}-START {tag}-END", f"{tag} got {text!r}"
    assert escaped.getvalue() == "", "output escaped to the process stream"


def test_streams_are_restored_after_an_exception():
    real_out, real_err = sys.stdout, sys.stderr
    with pytest.raises(RuntimeError):
        with captured_streams():
            print("swallowed")
            raise RuntimeError("boom")
    assert sys.stdout is real_out
    assert sys.stderr is real_err
    # and the boundary was released, not left held
    with captured_streams() as (out, _err):
        print("after")
    assert out.getvalue().strip() == "after"


def test_the_boundary_is_reentrant_and_does_not_deadlock():
    done = threading.Event()

    def nested():
        with cli_output_lock():
            with captured_streams() as (out, _err):
                print("inner")
            assert out.getvalue().strip() == "inner"
        done.set()

    t = threading.Thread(target=nested)
    t.start()
    t.join(timeout=10)
    assert done.is_set(), "re-entering the boundary deadlocked"


def test_capture_cli_output_returns_streams_and_result():
    def fn():
        print("to stdout")
        print("to stderr", file=sys.stderr)
        return 7

    out, err, result = cli_capture.capture_cli_output(fn)
    assert out.strip() == "to stdout"
    assert err.strip() == "to stderr"
    assert result == 7


# ---------------------------------------------------------------------------
# /project and /kanban share ONE boundary
# ---------------------------------------------------------------------------

class _CountingLock:
    def __init__(self, real):
        self._real = real
        self.holders: list[str] = []

    def acquire(self, *a, **k):
        got = self._real.acquire(*a, **k)
        if got:
            self.holders.append(threading.current_thread().name)
        return got

    def release(self):
        return self._real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "cap.db"))
    from hermes_cli import kanban_db as kb

    kb.init_db()
    conn = kb.connect()
    try:
        kb.ensure_pm_project(conn, project_id="proj-1")
        kb.submit_plan(conn, project_id="proj-1", body="the plan body")
    finally:
        conn.close()
    return tmp_path


def _event(text):
    from gateway.config import Platform

    src = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="c1",
                          chat_type="dm", thread_id="", user_id="u1")
    return SimpleNamespace(text=text, source=src, message_id="1",
                           reply_to_message_id=None)


@pytest.mark.asyncio
async def test_project_and_kanban_take_the_same_boundary(board, monkeypatch):
    from gateway.run import GatewayRunner

    counting = _CountingLock(threading.RLock())
    monkeypatch.setattr(cli_capture, "_CAPTURE_LOCK", counting)

    runner = object.__new__(GatewayRunner)
    runner._owns_kanban_dispatcher_lock = lambda: True
    project_reply, kanban_reply = await asyncio.gather(
        GatewayRunner._handle_project_command(
            runner, _event("/project plan-show proj-1")),
        GatewayRunner._handle_kanban_command(runner, _event("/kanban list")),
    )
    assert len(counting.holders) >= 2, (
        "both commands must pass through the shared boundary; "
        f"observed {counting.holders}"
    )
    assert "the plan body" in project_reply
    assert "the plan body" not in kanban_reply, "output crossed commands"


@pytest.mark.asyncio
async def test_concurrent_gateway_replies_are_complete_and_do_not_escape(board):
    from gateway.run import GatewayRunner
    from hermes_cli import projects_cmd

    inside_streams: list[bool] = []

    def marked(args):
        tag = (getattr(args, "target", None) or "?").upper()
        current = sys.stdout
        print(f"{tag}-START")
        time.sleep(0.05)
        print(f"{tag}-END")
        inside_streams.append(sys.stdout is current)
        return 0

    real_command = projects_cmd.projects_command
    projects_cmd.projects_command = marked
    real_out, real_err = sys.stdout, sys.stderr
    escaped = io.StringIO()
    try:
        sys.stdout = escaped
        runner = object.__new__(GatewayRunner)
        replies = await asyncio.gather(*[
            GatewayRunner._handle_project_command(
                runner, _event(f"/project plan-show {tag}"))
            for tag in ("alpha", "beta", "gamma", "delta")
        ])
    finally:
        projects_cmd.projects_command = real_command
        sys.stdout, sys.stderr = real_out, real_err

    assert all(inside_streams), "a handler's stream changed under it"
    for tag, reply in zip(("ALPHA", "BETA", "GAMMA", "DELTA"), replies):
        assert reply.split() == [f"{tag}-START", f"{tag}-END"], reply
    assert escaped.getvalue() == "", "reply text escaped to the process stream"
    assert sys.stdout is real_out and sys.stderr is real_err


@pytest.mark.asyncio
async def test_the_boundary_never_blocks_the_event_loop(board):
    """The lock is taken in worker threads, so the loop keeps servicing work."""
    from gateway.run import GatewayRunner
    from hermes_cli import projects_cmd

    def slow(args):
        time.sleep(0.3)
        print("done")
        return 0

    real_command = projects_cmd.projects_command
    projects_cmd.projects_command = slow
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.02)
            ticks += 1

    try:
        runner = object.__new__(GatewayRunner)
        await asyncio.gather(
            GatewayRunner._handle_project_command(
                runner, _event("/project plan-show a")),
            GatewayRunner._handle_project_command(
                runner, _event("/project plan-show b")),
            ticker(),
        )
    finally:
        projects_cmd.projects_command = real_command

    assert ticks == 20, (
        f"the event loop stalled while commands serialized (ticks={ticks})"
    )


# ---------------------------------------------------------------------------
# argparse is output-producing, so it belongs inside the boundary too
# ---------------------------------------------------------------------------

MALFORMED = "/project show"          # `show` requires a positional


@pytest.mark.asyncio
async def test_malformed_input_emits_nothing_to_the_process_streams(board):
    """argparse prints usage + error to stderr before raising SystemExit."""
    from gateway.run import GatewayRunner

    real_out, real_err = sys.stdout, sys.stderr
    escaped_out, escaped_err = io.StringIO(), io.StringIO()
    try:
        sys.stdout, sys.stderr = escaped_out, escaped_err
        runner = object.__new__(GatewayRunner)
        reply = await GatewayRunner._handle_project_command(
            runner, _event(MALFORMED))
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    assert escaped_out.getvalue() == "", escaped_out.getvalue()
    assert escaped_err.getvalue() == "", escaped_err.getvalue()
    assert sys.stdout is real_out and sys.stderr is real_err
    # The caller still gets a friendly, bounded answer — not an argparse dump.
    assert "could not parse those arguments" in reply
    assert "usage: hermes project" not in reply
    assert len(reply) < 400


def test_malformed_input_cannot_reach_another_requests_reply(board):
    """One request holds the boundary; a malformed one runs against it.

    Before the fix the malformed request's argparse error was written to the
    process-global stream that the FIRST request owned, and came back appended
    to that user's reply.
    """
    import asyncio as _asyncio

    from gateway.run import GatewayRunner
    from hermes_cli import projects_cmd

    inside = threading.Event()
    may_finish = threading.Event()
    replies: dict[str, str] = {}

    def slow_valid(args):
        print("ALPHA-ONLY")
        inside.set()
        may_finish.wait(timeout=1.5)   # stay inside the capture region
        return 0

    real_command = projects_cmd.projects_command
    projects_cmd.projects_command = slow_valid
    real_out, real_err = sys.stdout, sys.stderr
    escaped = io.StringIO()

    def run_valid():
        runner = object.__new__(GatewayRunner)
        replies["valid"] = _asyncio.run(
            GatewayRunner._handle_project_command(
                runner, _event("/project plan-show alpha")))

    def run_malformed():
        inside.wait(timeout=10)
        runner = object.__new__(GatewayRunner)
        replies["malformed"] = _asyncio.run(
            GatewayRunner._handle_project_command(runner, _event(MALFORMED)))
        may_finish.set()

    try:
        sys.stdout = escaped
        threads = [threading.Thread(target=run_valid),
                   threading.Thread(target=run_malformed)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        may_finish.set()
        assert not any(t.is_alive() for t in threads), "a request deadlocked"
    finally:
        projects_cmd.projects_command = real_command
        sys.stdout, sys.stderr = real_out, real_err

    assert replies["valid"].split() == ["ALPHA-ONLY"], replies["valid"]
    assert "usage:" not in replies["valid"]
    assert "error:" not in replies["valid"]
    assert "could not parse those arguments" in replies["malformed"]
    assert "ALPHA-ONLY" not in replies["malformed"]
    assert escaped.getvalue() == "", "argparse text escaped to the process stream"


@pytest.mark.asyncio
async def test_streams_survive_a_handler_exception_inside_the_boundary(board):
    from gateway.run import GatewayRunner
    from hermes_cli import projects_cmd

    def explode(args):
        print("partial output")
        raise RuntimeError("handler blew up")

    real_command = projects_cmd.projects_command
    projects_cmd.projects_command = explode
    real_out, real_err = sys.stdout, sys.stderr
    try:
        runner = object.__new__(GatewayRunner)
        reply = await GatewayRunner._handle_project_command(
            runner, _event("/project plan-show alpha"))
    finally:
        projects_cmd.projects_command = real_command

    assert sys.stdout is real_out and sys.stderr is real_err
    assert "handler blew up" in reply
    # and the boundary is free for the next request
    with captured_streams() as (out, _err):
        print("next")
    assert out.getvalue().strip() == "next"


@pytest.mark.asyncio
async def test_a_valid_request_still_answers_after_a_malformed_one(board):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    await GatewayRunner._handle_project_command(runner, _event(MALFORMED))
    reply = await GatewayRunner._handle_project_command(
        runner, _event("/project plan-show proj-1"))
    assert "the plan body" in reply
