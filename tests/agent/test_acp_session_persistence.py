"""Tests for ACP session persistence.

Covers the four commits that added session persistence to the ACP client
transport:

  * ``agent/transports/acp_session_mapping.py``  — the resume path in
    ``ACPClientSession.ensure_started()`` that reattaches to a persisted
    Hermes↔ACP binding, plus binding lifecycle (create / lookup / stale).
  * ``agent/transports/acp_client_session.py``    — tool-call notification
    capture projected into OpenAI-shaped message pairs (integration-level,
    complementing the unit tests in test_acp_client_session.py).
  * ``agent/acp_runtime.py``                      — ``_inject_hermes_history_to_acp``
    which carries Hermes history into a fresh ACP session on a Native→ACP
    runtime switch.

The subprocess layer (``ACPClient``) is always mocked so no real process is
spawned. SQLite mappers use pytest's ``tmp_path`` so the real ``state.db`` is
never touched, and ``Path.home`` is patched when exercising the transcript
write so nothing lands in the real ``~/.claude``.
"""

from __future__ import annotations

import contextlib
import time
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from agent.acp_runtime import _inject_hermes_history_to_acp, run_acp_client_turn
from agent.transports.acp_client import ACPClientError
from agent.transports.acp_client_session import ACPClientSession, TurnResult
from agent.transports.acp_session_mapping import (
    ACPSessionBinding,
    SQLiteACPSessionMapper,
)


# ---------------------------------------------------------------------------
# Helpers — mock ACPClient (subprocess transport)
# ---------------------------------------------------------------------------


def _mock_client() -> MagicMock:
    """A MagicMock shaped like the subset of ACPClient the session drives."""
    client = MagicMock()
    client.is_alive.return_value = True
    client.initialize.return_value = {"protocolVersion": 1}
    client.request.return_value = {}
    client.take_notification.return_value = None
    client.take_server_request.return_value = None
    client.stderr_tail.return_value = []
    return client


def _make_mapped_session(
    *,
    mapper: Optional[MagicMock],
    hermes_session_id: str = "hermes-1",
    provider: str = "claude",
    model: Optional[str] = None,
    permission_mode: Optional[str] = None,
) -> tuple[ACPClientSession, MagicMock]:
    """An ACPClientSession wired to a (mock or real) mapper and mock client."""
    client = _mock_client()
    session = ACPClientSession(
        command="fake-acp",
        model=model,
        permission_mode=permission_mode,
        mapper=mapper,
        hermes_session_id=hermes_session_id,
        provider=provider,
        client_factory=lambda **kw: client,
    )
    return session, client


def _make_plain_session() -> tuple[ACPClientSession, MagicMock]:
    """An ACPClientSession with no mapper (baseline behaviour)."""
    client = _mock_client()
    session = ACPClientSession(
        command="fake-acp",
        client_factory=lambda **kw: client,
    )
    return session, client


def _request_methods(client: MagicMock) -> list[str]:
    """The wire method names passed to client.request(), in call order."""
    return [c[0][0] for c in client.request.call_args_list]


# ---------------------------------------------------------------------------
# Helpers — ACP session/update notification builders
# ---------------------------------------------------------------------------


def _tool_note(
    *,
    kind: str,
    tool_call_id: str = "tc-1",
    title: str = "bash",
    status: Optional[str] = None,
    raw_input=None,
    raw_output=None,
) -> dict:
    """A session/update carrying a tool-call lifecycle event (start/update)."""
    update: dict = {"sessionUpdate": kind, "toolCallId": tool_call_id}
    if title is not None:
        update["title"] = title
    if status is not None:
        update["status"] = status
    if raw_input is not None:
        update["rawInput"] = raw_input
    if raw_output is not None:
        update["rawOutput"] = raw_output
    return {"method": "session/update", "params": {"sessionId": "sess-tool", "update": update}}


def _text_note(text: str) -> dict:
    """A session/update carrying an agent_message_chunk of user-facing text."""
    return {
        "method": "session/update",
        "params": {
            "sessionId": "sess-tool",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


def _drive_turn(notes: list) -> TurnResult:
    """Run one turn through a plain session, feeding ``notes`` then a sentinel."""
    session, client = _make_plain_session()

    def req_side_effect(method, params=None, timeout=30, **kwargs):
        if method == "session/new":
            return {"sessionId": "sess-tool"}
        if method == "session/prompt":
            # Brief block so the drain loop processes notifications first,
            # mirroring the real session/prompt which blocks for the whole turn.
            time.sleep(0.05)
            return {"stopReason": "end_turn"}
        return {}

    client.request.side_effect = req_side_effect
    notes_iter = iter(list(notes) + [None, None])
    client.take_notification.side_effect = lambda timeout=0.0: next(notes_iter, None)
    return session.run_turn("use a tool", cwd="/tmp")


# ---------------------------------------------------------------------------
# Helpers — run_acp_client_turn call-site harness
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Minimal stand-in for AIAgent exposing only what run_acp_client_turn reads.

    Real-valued attributes (not MagicMocks) keep the numeric comparisons in
    run_acp_client_turn deterministic — e.g. ``_skill_nudge_interval = 0``
    cleanly disables the skill-nudge branch.
    """

    def __init__(self, *, session_db=object(), session_id: str = "hermes-inject") -> None:
        self.session_cwd = "/tmp/proj"
        self._acp_session = None
        self.acp_command = "fake-acp"
        self.acp_args = []
        self.model = "sonnet"
        self.acp_mcp_servers = []
        self.acp_session_meta = None
        self.session_id = session_id
        self._session_db = session_db
        self._iters_since_skill = 0
        self._skill_nudge_interval = 0
        self.valid_tool_names: set = set()

    def _fire_stream_delta(self, *a, **k) -> None:  # pragma: no cover - trivial
        pass

    def _flush_messages_to_session_db(self, msgs) -> None:  # pragma: no cover
        pass

    def _sync_external_memory_for_turn(self, *a, **k) -> None:  # pragma: no cover
        pass

    def _spawn_background_review(self, *a, **k) -> None:  # pragma: no cover
        pass


@contextlib.contextmanager
def _guarded_turn(agent: _FakeAgent):
    """Mock the subprocess + approval layers so run_acp_client_turn runs
    without spawning anything or touching the real mapper DB.

    The mock mapper's ``lookup`` returns ``None`` (no existing binding) so the
    Native→ACP history-injection branch is taken. Yields
    ``(session_mock, mapper_mock)``.
    """
    session_mock = MagicMock()
    session_mock.run_turn.return_value = TurnResult()
    mapper_mock = MagicMock()
    mapper_mock.lookup.return_value = None
    with patch(
        "agent.transports.acp_client_session.ACPClientSession",
        return_value=session_mock,
    ), patch(
        "agent.transports.acp_session_mapping.SQLiteACPSessionMapper",
        return_value=mapper_mock,
    ), patch(
        "agent.transports.acp_approval.make_acp_approval_callback",
        return_value=None,
    ), patch(
        "tools.approval.is_approval_bypass_active",
        return_value=False,
    ):
        yield session_mock, mapper_mock


def _run_turn(agent: _FakeAgent) -> dict:
    return run_acp_client_turn(
        agent,
        user_message="hi",
        original_user_message="hi",
        messages=[],
        effective_task_id="t",
    )


# ---------------------------------------------------------------------------
# Tests: resume path in ACPClientSession.ensure_started()
# ---------------------------------------------------------------------------


class TestACPClientSessionResume:
    def test_resume_succeeds_when_binding_exists(self, tmp_path):
        """An active binding is resumed via session/resume; session/new is NOT
        called, and the bound ACP session id is returned."""
        mapper = SQLiteACPSessionMapper(db_path=tmp_path / "state.db")
        mapper.bind(ACPSessionBinding(
            hermes_session_id="hermes-1",
            acp_session_id="acp-resumed",
            provider="claude",
            cwd="/tmp",
            status="active",
        ))
        session, client = _make_mapped_session(mapper=mapper)

        sid = session.ensure_started(cwd="/tmp")

        assert sid == "acp-resumed"
        assert session._session_id == "acp-resumed"
        methods = _request_methods(client)
        assert "session/resume" in methods
        assert "session/new" not in methods
        resume_call = next(
            c for c in client.request.call_args_list if c[0][0] == "session/resume"
        )
        assert resume_call[0][1]["sessionId"] == "acp-resumed"

    def test_resume_falls_back_to_new_on_resource_not_found(self):
        """A -32002 resourceNotFound from session/resume marks the binding stale
        and falls through to session/new, persisting the fresh binding."""
        mapper = MagicMock()
        mapper.lookup.return_value = ACPSessionBinding(
            hermes_session_id="hermes-1",
            acp_session_id="acp-gone",
            provider="claude",
            cwd="/tmp",
            status="active",
        )
        session, client = _make_mapped_session(mapper=mapper)

        def req_side(method, params=None, timeout=30, **kwargs):
            if method == "session/resume":
                raise ACPClientError(code=-32002, message="resource not found")
            if method == "session/new":
                return {"sessionId": "acp-fresh"}
            return {}

        client.request.side_effect = req_side

        sid = session.ensure_started(cwd="/tmp")

        assert sid == "acp-fresh"
        mapper.mark_stale.assert_called_once_with("hermes-1")
        # The fresh session/new binding is persisted.
        bound = mapper.bind.call_args[0][0]
        assert bound.acp_session_id == "acp-fresh"
        assert "session/new" in _request_methods(client)

    def test_resume_reraises_non_32002_errors(self):
        """A resume error that is NOT -32002 propagates, does not mark the
        binding stale, and does not fall through to session/new."""
        mapper = MagicMock()
        mapper.lookup.return_value = ACPSessionBinding(
            hermes_session_id="hermes-1",
            acp_session_id="acp-x",
            provider="claude",
            cwd="/tmp",
            status="active",
        )
        session, client = _make_mapped_session(mapper=mapper)

        def req_side(method, params=None, timeout=30, **kwargs):
            if method == "session/resume":
                raise ACPClientError(code=-32603, message="internal error")
            return {"sessionId": "should-not-reach"}

        client.request.side_effect = req_side

        with pytest.raises(ACPClientError) as exc_info:
            session.ensure_started(cwd="/tmp")

        assert exc_info.value.code == -32603
        mapper.mark_stale.assert_not_called()
        assert "session/new" not in _request_methods(client)

    def test_new_session_persists_binding(self, tmp_path):
        """With no binding present, session/new is used and the resulting
        session id is written to the mapper as an active binding."""
        mapper = SQLiteACPSessionMapper(db_path=tmp_path / "state.db")
        session, client = _make_mapped_session(mapper=mapper)
        client.request.return_value = {"sessionId": "acp-new"}

        sid = session.ensure_started(cwd="/work")

        assert sid == "acp-new"
        found = mapper.lookup("hermes-1", "claude")
        assert found is not None
        assert found.acp_session_id == "acp-new"
        assert found.status == "active"
        assert found.cwd == "/work"
        methods = _request_methods(client)
        assert "session/new" in methods
        assert "session/resume" not in methods

    def test_no_mapper_skips_resume_entirely(self):
        """mapper=None → behaviour identical to before: always session/new,
        no resume attempt and no binding operations."""
        session, client = _make_mapped_session(mapper=None)
        client.request.return_value = {"sessionId": "s"}

        sid = session.ensure_started(cwd="/tmp")

        assert sid == "s"
        assert _request_methods(client) == ["session/new"]

    def test_close_marks_binding_stale(self):
        """close() marks the persisted binding stale so the next session for
        this Hermes session starts fresh instead of resuming a closed one."""
        mapper = MagicMock()
        session, client = _make_mapped_session(mapper=mapper)
        client.request.return_value = {"sessionId": "s"}
        session.ensure_started(cwd="/tmp")
        mapper.mark_stale.reset_mock()

        session.close()

        mapper.mark_stale.assert_called_once_with("hermes-1")


# ---------------------------------------------------------------------------
# Tests: tool-call notification capture (integration level, full run_turn)
# ---------------------------------------------------------------------------


class TestToolCallCapture:
    def test_full_turn_with_tool_calls_produces_correct_projected_messages(self):
        """text → tool_call start → tool_call_update(completed) → more text
        projects to [assistant(tool_calls), tool(result), assistant(text)].

        Both text chunks are merged into a single trailing assistant message
        (all chunks are joined and projected once at the end of the turn).
        """
        result = _drive_turn([
            _text_note("Thinking... "),
            _tool_note(kind="tool_call", tool_call_id="tc-1", title="bash",
                       raw_input={"command": "ls"}),
            _tool_note(kind="tool_call_update", tool_call_id="tc-1",
                       status="completed", raw_output="done"),
            _text_note("Finished."),
        ])

        msgs = result.projected_messages
        assert [m["role"] for m in msgs] == ["assistant", "tool", "assistant"]

        # assistant tool_call first
        assert msgs[0]["content"] is None
        assert msgs[0]["tool_calls"][0]["id"] == "tc-1"
        assert msgs[0]["tool_calls"][0]["function"]["name"] == "bash"
        # tool result second
        assert msgs[1]["tool_call_id"] == "tc-1"
        assert msgs[1]["content"] == "done"
        # merged final assistant text last
        assert msgs[2]["content"] == "Thinking... Finished."
        assert result.tool_iterations == 1

    def test_multiple_tool_calls_in_one_turn(self):
        """Two sequential tool calls produce two pairs (4 messages) ahead of
        the final assistant text message."""
        result = _drive_turn([
            _tool_note(kind="tool_call", tool_call_id="tc-a", title="ls", raw_input={}),
            _tool_note(kind="tool_call_update", tool_call_id="tc-a",
                       status="completed", raw_output="a-out"),
            _tool_note(kind="tool_call", tool_call_id="tc-b", title="cat",
                       raw_input={"path": "x"}),
            _tool_note(kind="tool_call_update", tool_call_id="tc-b",
                       status="completed", raw_output="b-out"),
            _text_note("done"),
        ])

        msgs = result.projected_messages
        assert len(msgs) == 5
        assert [m["role"] for m in msgs] == [
            "assistant", "tool", "assistant", "tool", "assistant",
        ]
        # The first four messages are the two tool pairs, in order.
        assert msgs[0]["tool_calls"][0]["id"] == "tc-a"
        assert msgs[1]["content"] == "a-out"
        assert msgs[2]["tool_calls"][0]["id"] == "tc-b"
        assert msgs[3]["content"] == "b-out"
        # The fifth is the final assistant text.
        assert msgs[4]["content"] == "done"
        assert result.tool_iterations == 2

    def test_failed_tool_call_still_captured(self):
        """A tool_call_update with status='failed' is terminal and still
        produces the assistant+tool pair (with the failure output)."""
        result = _drive_turn([
            _tool_note(kind="tool_call", tool_call_id="tc-1", title="bash",
                       raw_input={"command": "bad"}),
            _tool_note(kind="tool_call_update", tool_call_id="tc-1",
                       status="failed", raw_output="error: exit 1"),
            _text_note("oops"),
        ])

        msgs = result.projected_messages
        assert len(msgs) == 3
        assert msgs[0]["tool_calls"][0]["id"] == "tc-1"
        assert msgs[1]["role"] == "tool"
        assert msgs[1]["content"] == "error: exit 1"
        assert result.tool_iterations == 1

    def test_orphan_tool_update_skipped(self):
        """A terminal tool_call_update with no matching start does not fabricate
        a pair; the final assistant text is still projected."""
        result = _drive_turn([
            _tool_note(kind="tool_call_update", tool_call_id="orphan",
                       status="completed", raw_output="ghost"),
            _text_note("hello"),
        ])

        msgs = result.projected_messages
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["content"] == "hello"
        # No start seen → no iteration ticked and no pair fabricated.
        assert result.tool_iterations == 0

    def test_pending_cleared_between_turns(self):
        """A tool call that starts but never completes in one turn does not
        leak into the next turn's projected history."""
        session, client = _make_plain_session()

        def req_side_effect(method, params=None, timeout=30, **kwargs):
            if method == "session/new":
                return {"sessionId": "sess-clear"}
            if method == "session/prompt":
                time.sleep(0.03)
                return {"stopReason": "end_turn"}
            return {}

        client.request.side_effect = req_side_effect

        # Turn 1: a tool call starts but never reaches a terminal update.
        notes1 = iter([
            _tool_note(kind="tool_call", tool_call_id="leak", title="bash", raw_input={}),
            None, None,
        ])
        client.take_notification.side_effect = lambda timeout=0.0: next(notes1, None)
        r1 = session.run_turn("interrupt me", cwd="/tmp")
        assert r1.tool_iterations == 1
        assert r1.projected_messages == []  # no completion → no pair
        assert "leak" in session._pending_tool_calls  # dangling within the turn

        # Turn 2: clean — no stale pair from the leaked pending entry.
        notes2 = iter([_text_note("fresh"), None, None])
        client.take_notification.side_effect = lambda timeout=0.0: next(notes2, None)
        r2 = session.run_turn("fresh turn", cwd="/tmp")
        assert r2.tool_iterations == 0
        assert [m["content"] for m in r2.projected_messages] == ["fresh"]
        assert session._pending_tool_calls == {}


# ---------------------------------------------------------------------------
# Tests: _inject_hermes_history_to_acp (Native → ACP context injection)
# ---------------------------------------------------------------------------


class TestInjectHermesHistory:
    def test_injection_creates_jsonl_and_binding(self, tmp_path):
        """History is rendered to a Claude Code JSONL transcript under the
        sanitized-cwd projects dir, and an active binding is created whose
        acp_session_id matches the transcript filename."""
        mapper = MagicMock()
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        agent = MagicMock()
        agent.model = "sonnet"

        with patch("pathlib.Path.home", return_value=tmp_path), patch(
            "agent.trace_upload.load_session_messages", return_value=(msgs, {}),
        ), patch(
            "agent.trace_upload.build_trace_jsonl", return_value='{"jsonl":true}',
        ) as btj:
            _inject_hermes_history_to_acp(
                agent, "hermes-1", "claude", "/home/nbot/proj", mapper,
            )

        mapper.bind.assert_called_once()
        binding = mapper.bind.call_args[0][0]
        assert binding.hermes_session_id == "hermes-1"
        assert binding.provider == "claude"
        assert binding.status == "active"
        assert binding.model == "sonnet"
        assert binding.cwd == "/home/nbot/proj"

        # Local context file, not an upload → redaction is intentionally off.
        assert btj.call_args.kwargs.get("redact") is False

        # Transcript written to the sanitized path, named after the session id.
        transcript = (
            tmp_path / ".claude" / "projects" / "-home-nbot-proj"
            / f"{binding.acp_session_id}.jsonl"
        )
        assert transcript.exists()
        assert transcript.read_text() == '{"jsonl":true}\n'

    def test_injection_skipped_when_no_messages(self, tmp_path):
        """A brand-new session has no messages → no file written, no binding,
        and the trace renderer is never invoked."""
        mapper = MagicMock()
        with patch("pathlib.Path.home", return_value=tmp_path), patch(
            "agent.trace_upload.load_session_messages", return_value=([], {}),
        ), patch("agent.trace_upload.build_trace_jsonl") as btj:
            _inject_hermes_history_to_acp(
                MagicMock(), "hermes-empty", "claude", "/tmp/x", mapper,
            )

        mapper.bind.assert_not_called()
        btj.assert_not_called()
        assert not (tmp_path / ".claude" / "projects").exists()

    def test_injection_skipped_when_no_session_db(self):
        """Agent without a backing ``_session_db`` → the call-site guard skips
        injection entirely (never called), and the turn still completes."""
        agent = _FakeAgent(session_db=None)
        with _guarded_turn(agent) as (_sess, _mapper), patch(
            "agent.acp_runtime._inject_hermes_history_to_acp",
        ) as inject_spy:
            result = _run_turn(agent)

        inject_spy.assert_not_called()
        assert result["completed"] is True

    def test_injection_failure_does_not_raise(self):
        """A failure inside injection (build_trace_jsonl raises) is swallowed by
        the run_acp_client_turn call site — the turn still runs and completes.

        The 'never raises' guarantee lives at the call site (try/except around
        _inject_hermes_history_to_acp), so this drives the real call site rather
        than the helper in isolation.
        """
        agent = _FakeAgent()
        msgs = [{"role": "user", "content": "hi"}]
        with _guarded_turn(agent) as (sess, _mapper), patch(
            "agent.trace_upload.load_session_messages", return_value=(msgs, {}),
        ), patch(
            "agent.trace_upload.build_trace_jsonl",
            side_effect=RuntimeError("render failed"),
        ):
            result = _run_turn(agent)  # must not raise

        assert result["completed"] is True
        sess.run_turn.assert_called_once()

    def test_sanitized_cwd_path(self, tmp_path):
        """Every non-alphanumeric char in the absolute cwd maps to '-' (per
        char, not collapsed): /home/nbot/my.project → -home-nbot-my-project."""
        mapper = MagicMock()
        with patch("pathlib.Path.home", return_value=tmp_path), patch(
            "agent.trace_upload.load_session_messages",
            return_value=([{"role": "user", "content": "x"}], {}),
        ), patch("agent.trace_upload.build_trace_jsonl", return_value="line"):
            _inject_hermes_history_to_acp(
                MagicMock(), "h", "claude", "/home/nbot/my.project", mapper,
            )

        expected_dir = tmp_path / ".claude" / "projects" / "-home-nbot-my-project"
        assert expected_dir.is_dir()
        assert len(list(expected_dir.glob("*.jsonl"))) == 1


# ---------------------------------------------------------------------------
# Tests: end-to-end lifecycle (mapping + resume)
# ---------------------------------------------------------------------------


class TestEndToEndResumeFlow:
    def test_full_lifecycle_new_resume_close(self, tmp_path):
        """new → resume → close(stale) → fresh, all against one persisted mapper:

        1. First session: no binding → session/new → binding created (active).
        2. Restart (same mapper): binding found → session/resume succeeds.
        3. close(): binding marked stale.
        4. Another restart: binding stale → session/new (fresh start).
        """
        mapper = SQLiteACPSessionMapper(db_path=tmp_path / "state.db")
        hsid, provider = "hermes-e2e", "claude"

        # 1. First session — no binding yet → session/new → binding created.
        s1, c1 = _make_mapped_session(mapper=mapper, hermes_session_id=hsid, provider=provider)
        c1.request.return_value = {"sessionId": "acp-first"}
        assert mapper.lookup(hsid, provider) is None
        assert s1.ensure_started(cwd="/work") == "acp-first"
        b = mapper.lookup(hsid, provider)
        assert b is not None
        assert b.acp_session_id == "acp-first"
        assert b.status == "active"

        # 2. Restart — binding found and active → session/resume (same id).
        s2, c2 = _make_mapped_session(mapper=mapper, hermes_session_id=hsid, provider=provider)
        c2.request.return_value = {}  # session/resume success
        assert s2.ensure_started(cwd="/work") == "acp-first"
        methods2 = _request_methods(c2)
        assert "session/resume" in methods2
        assert "session/new" not in methods2

        # 3. Close — binding marked stale.
        s2.close()
        assert mapper.lookup(hsid, provider).status == "stale"

        # 4. Another restart — stale binding is not resumed → fresh session/new.
        s3, c3 = _make_mapped_session(mapper=mapper, hermes_session_id=hsid, provider=provider)
        c3.request.return_value = {"sessionId": "acp-second"}
        assert s3.ensure_started(cwd="/work") == "acp-second"
        methods3 = _request_methods(c3)
        assert "session/new" in methods3
        assert "session/resume" not in methods3
        # The fresh binding replaces the stale one as active.
        assert mapper.lookup(hsid, provider).acp_session_id == "acp-second"
