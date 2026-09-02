"""Tests for hermes_cli.foreign_sessions — Claude Code / Codex CLI / Kimi Code import.

Fixture JSONL is synthesized inline (tmp_path); the SessionDB is opened
against a temp path so nothing touches the real HERMES_HOME store.
"""

import json
from datetime import datetime, timezone

import pytest

from hermes_cli.foreign_sessions import (
    gather_foreign_sessions,
    import_foreign_session,
    list_claude_sessions,
    list_codex_sessions,
    list_kimi_sessions,
    parse_claude_session,
    parse_codex_session,
    parse_kimi_session,
)


# ── fixture builders ─────────────────────────────────────────────────────


def _claude_lines():
    def msg(role, content):
        return {
            "type": role,
            "sessionId": "abc-123",
            "cwd": "/home/user/proj",
            "message": {"role": role, "content": content},
        }

    return [
        {"type": "summary", "summary": "Fix the flaky test"},
        msg("user", "Please fix the flaky test in CI."),
        msg(
            "assistant",
            [
                {"type": "text", "text": "Looking into it now."},
                {"type": "tool_use", "name": "Bash", "id": "t1", "input": {}},
            ],
        ),
        # tool_result echoed back as a user message — must NOT become a turn
        msg("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        msg("assistant", [{"type": "text", "text": "Fixed — the sleep was too short."}]),
        msg("user", "Great, thanks!"),
        msg("assistant", [{"type": "text", "text": "Anytime."}]),
    ]


def _write_claude_fixture(tmp_path, extra_lines=None):
    proj = tmp_path / ".claude" / "projects" / "-home-user-proj"
    proj.mkdir(parents=True)
    f = proj / "abc-123.jsonl"
    lines = [json.dumps(entry) for entry in _claude_lines()]
    if extra_lines:
        lines = extra_lines + lines
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def _codex_lines():
    def item(payload):
        return {"timestamp": "2026-08-15T21:35:28Z", "type": "response_item", "payload": payload}

    def message(role, kind, text):
        return item({"type": "message", "role": role, "content": [{"type": kind, "text": text}]})

    return [
        {
            "type": "session_meta",
            "payload": {"session_id": "0000-1111", "cwd": "/home/user/repo"},
        },
        # developer/system payloads must never be imported
        message("developer", "input_text", "<skills_instructions>secret system stuff"),
        message("user", "input_text", "<recommended_plugins>\ninjected wrapper"),
        message("user", "input_text", "Summarize the transcripts please."),
        message("assistant", "output_text", "Reading them one at a time."),
        item({"type": "custom_tool_call", "name": "shell", "call_id": "c1"}),
        item({"type": "custom_tool_call_output", "call_id": "c1", "output": "big output"}),
        message("assistant", "output_text", "Done — here is the summary."),
        message("user", "input_text", "Now write it to a file."),
        message("assistant", "output_text", "Written to summary.md."),
    ]


def _write_codex_fixture(tmp_path, extra_lines=None):
    day = tmp_path / ".codex" / "sessions" / "2026" / "08" / "15"
    day.mkdir(parents=True)
    f = day / "rollout-2026-08-15T21-35-28-0000-1111.jsonl"
    lines = [json.dumps(entry) for entry in _codex_lines()]
    if extra_lines:
        lines = extra_lines + lines
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def _write_kimi_fixture(tmp_path, extra_lines=None):
    """A Kimi Code session dir: state.json + agents/main/wire.jsonl.

    Mirrors the real on-disk layout (~/.kimi-code/sessions/<wd>/<session>/)
    and the real wire record types observed in protocol 1.5: turn.prompt
    duplicates every typed user message into context.append_message, and
    assistant output streams as loop events (content.part / tool.call).
    """
    session_dir = (
        tmp_path / ".kimi-code" / "sessions" / "wd_user_abc123" / "session_kimi-0001"
    )
    wire_dir = session_dir / "agents" / "main"
    wire_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "session_kimi-0001",
                "cwd": "/home/user/kproj",
                "title": "Fix the importer",
                "updatedAt": 1786707200000,
            }
        ),
        encoding="utf-8",
    )

    def append_user(text, kind="user"):
        return {
            "type": "context.append_message",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
                "origin": {"kind": kind},
                "id": "msg_" + kind,
            },
        }

    def loop_event(event):
        return {"type": "context.append_loop_event", "event": event}

    entries = [
        {"type": "metadata", "protocol_version": "1.5", "created_at": 1786707196309},
        # turn.prompt duplicates the typed message — must NOT double-import
        {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "Summarize the repo please."}],
            "origin": {"kind": "user"},
        },
        append_user("Summarize the repo please."),
        # A wire written before Kimi tagged provenance: no origin at all.
        # Backward-compatibility says treat it as typed input.
        {
            "type": "context.append_message",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "And check the tests."}],
                "id": "msg_untagged",
            },
        },
        # Every machine-generated kind observed on real wires rides the same
        # role=="user" transport and must stay out of the imported turns.
        append_user("<system-reminder>injected wrapper", kind="injection"),
        append_user("async delegation finished", kind="task"),
        append_user("Continue working toward the active goal.", kind="system_trigger"),
        append_user("<notification>bun run check completed", kind="background_task"),
        append_user("Skill tool loaded instructions.", kind="skill_activation"),
        # A kind this parser has never seen — an allowlist must drop it too.
        append_user("some future machine row", kind="totally_new_kind"),
        loop_event({"type": "step.begin", "turnId": "0", "step": 1}),
        loop_event(
            {"type": "content.part", "part": {"type": "think", "think": "secret reasoning"}}
        ),
        loop_event(
            {"type": "content.part", "part": {"type": "text", "text": "Reading the files."}}
        ),
        loop_event(
            {
                "type": "tool.call",
                "name": "Bash",
                "args": {"command": "ls"},
                "toolCallId": "tool_1",
            }
        ),
        loop_event(
            {"type": "tool.result", "toolCallId": "tool_1", "result": {"output": "ok"}}
        ),
        # A second call back-to-back: the two bracketed summaries must stay
        # readable once _merge_turns folds them into one assistant turn.
        loop_event(
            {
                "type": "tool.call",
                "name": "Read",
                "args": {"path": "README.md"},
                "toolCallId": "tool_2",
            }
        ),
        loop_event(
            {
                "type": "content.part",
                "part": {"type": "text", "text": "Done — the summary is above."},
            }
        ),
        append_user("Thanks!"),
    ]
    lines = [json.dumps(entry) for entry in entries]
    if extra_lines:
        lines = extra_lines + lines
    wire = wire_dir / "wire.jsonl"
    wire.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return wire


@pytest.fixture
def session_db(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    yield db
    db.close()


def _assert_alternating(messages):
    roles = [m["role"] for m in messages]
    assert roles, "no messages"
    assert roles[0] == "user"
    for a, b in zip(roles, roles[1:]):
        assert a != b, f"two consecutive {a} messages"


# ── parsing ──────────────────────────────────────────────────────────────


def test_parse_claude_session(tmp_path):
    f = _write_claude_fixture(tmp_path)
    parsed = parse_claude_session(f)
    turns = parsed["turns"]
    _assert_alternating(turns)
    assert len(turns) == 4  # tool_result-only user line skipped; assistants merge
    assert parsed["cwd"] == "/home/user/proj"
    assert parsed["title_guess"] == "Fix the flaky test"
    assert parsed["session_id"] == "abc-123"
    # tool_use flattened to a bracketed summary, merged with adjacent text
    joined_assistant = "\n".join(t["content"] for t in turns if t["role"] == "assistant")
    assert "[ran tool: Bash]" in joined_assistant
    # no fabricated tool_call structures
    assert all(set(t) == {"role", "content"} for t in turns)


def test_parse_codex_session(tmp_path):
    f = _write_codex_fixture(tmp_path)
    parsed = parse_codex_session(f)
    turns = parsed["turns"]
    _assert_alternating(turns)
    assert len(turns) == 4
    assert parsed["cwd"] == "/home/user/repo"
    assert parsed["session_id"] == "0000-1111"
    assert parsed["title_guess"].startswith("Summarize the transcripts")
    # developer + wrapper user lines excluded
    all_text = "\n".join(t["content"] for t in turns)
    assert "skills_instructions" not in all_text
    assert "recommended_plugins" not in all_text
    # tool call summarized in assistant text
    assert "[ran tool: shell]" in all_text


def test_malformed_lines_are_skipped(tmp_path):
    garbage = [
        "this is not json at all {{{",
        '"just a string"',
        json.dumps({"type": "user", "message": "not-a-dict-payload... wait, string"}),
        json.dumps({"type": "user"}),  # missing message
        "",
    ]
    f = _write_claude_fixture(tmp_path, extra_lines=garbage)
    parsed = parse_claude_session(f)
    assert len(parsed["turns"]) == 4  # good lines still parse

    g = _write_codex_fixture(tmp_path, extra_lines=["not json", json.dumps({"type": "response_item"})])
    parsed_codex = parse_codex_session(g)
    assert len(parsed_codex["turns"]) == 4


# ── discovery ────────────────────────────────────────────────────────────


def test_list_sessions(tmp_path):
    _write_claude_fixture(tmp_path)
    _write_codex_fixture(tmp_path)
    claude = list_claude_sessions(tmp_path / ".claude" / "projects")
    codex = list_codex_sessions(tmp_path / ".codex" / "sessions")
    assert len(claude) == 1 and claude[0].source == "claude"
    assert claude[0].turn_count == 4
    assert len(codex) == 1 and codex[0].source == "codex"
    assert codex[0].turn_count == 4
    both = gather_foreign_sessions(
        claude_root=tmp_path / ".claude" / "projects",
        codex_root=tmp_path / ".codex" / "sessions",
        # Pin the kimi root to a nonexistent dir too — otherwise the gather
        # scans the real ~/.kimi-code/sessions and the count depends on the
        # machine running the test.
        kimi_root=tmp_path / ".kimi-code" / "sessions",
    )
    assert len(both) == 2
    assert both[0].mtime >= both[1].mtime  # newest first


def test_list_sessions_missing_roots(tmp_path):
    assert list_claude_sessions(tmp_path / "nope") == []
    assert list_codex_sessions(tmp_path / "nope") == []


# ── import into SessionDB ────────────────────────────────────────────────


def test_import_claude_session(tmp_path, session_db):
    f = _write_claude_fixture(tmp_path)
    session_id = import_foreign_session("claude", f, db=session_db)
    row = session_db.get_session(session_id)
    assert row is not None
    assert row["source"] == "claude-code"
    assert row["cwd"] == "/home/user/proj"
    assert row["message_count"] == 4
    title = session_db.get_session_title(session_id)
    assert title.startswith("Imported from Claude Code: ")
    assert "Please fix the flaky test" in title
    messages = session_db.get_messages(session_id)
    assert len(messages) == 4
    _assert_alternating(messages)
    origin = json.loads(row["origin_json"])
    assert origin["imported_from"]["tool"] == "claude-code"
    assert origin["imported_from"]["path"] == str(f)


def test_import_codex_session(tmp_path, session_db):
    f = _write_codex_fixture(tmp_path)
    session_id = import_foreign_session("@codex", f, db=session_db)
    row = session_db.get_session(session_id)
    assert row is not None
    assert row["source"] == "codex-cli"
    assert row["message_count"] == 4
    title = session_db.get_session_title(session_id)
    assert title.startswith("Imported from Codex CLI: ")
    messages = session_db.get_messages(session_id)
    _assert_alternating(messages)
    # resumable: resolve_session_id round-trips
    assert session_db.resolve_session_id(session_id) == session_id


def test_import_rejects_bad_input(tmp_path, session_db):
    with pytest.raises(ValueError, match="Unknown foreign session source"):
        import_foreign_session("gemini", tmp_path / "x.jsonl", db=session_db)
    with pytest.raises(ValueError, match="not found"):
        import_foreign_session("claude", tmp_path / "missing.jsonl", db=session_db)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="No user/assistant conversation"):
        import_foreign_session("claude", empty, db=session_db)


def test_leading_assistant_gets_single_stub(tmp_path):
    day = tmp_path / ".codex" / "sessions" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    f = day / "rollout-x.jsonl"
    lines = [
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Continuing from before."}],
                },
            }
        )
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    parsed = parse_codex_session(f)
    _assert_alternating(parsed["turns"])
    assert len(parsed["turns"]) == 2
    assert parsed["turns"][0]["role"] == "user"


# ── Kimi Code ────────────────────────────────────────────────────────────


def test_parse_kimi_session(tmp_path):
    f = _write_kimi_fixture(tmp_path)
    parsed = parse_kimi_session(f)
    turns = parsed["turns"]
    _assert_alternating(turns)
    # user: "Summarize..." / assistant: text+tool+text merged / user: "Thanks!"
    assert len(turns) == 3
    assert parsed["cwd"] == "/home/user/kproj"
    assert parsed["title_guess"] == "Fix the importer"
    assert parsed["session_id"] == "session_kimi-0001"

    all_text = "\n".join(t["content"] for t in turns)
    # the typed message appears exactly once (turn.prompt duplicate ignored)
    assert all_text.count("Summarize the repo please.") == 1
    # non-"user" origin rows are not typed input
    assert "system-reminder" not in all_text
    assert "async delegation" not in all_text
    # an untagged row (pre-provenance wire) still counts as typed input
    assert "And check the tests." in all_text
    # thinking stays out; the tool call is a bracketed assistant summary
    assert "secret reasoning" not in all_text
    assert "[ran tool: Bash]" in all_text
    assert all(set(t) == {"role", "content"} for t in turns)


def test_kimi_origin_filter_is_an_allowlist_not_a_denylist(tmp_path):
    """Every machine-generated ``role=="user"`` kind must stay out.

    role=="user" is a transport role, not a claim about authorship: Kimi
    ships several machine-generated kinds on it, and a denylist of the two
    known ones both missed kinds already in the wild and would fail open on
    every kind added later.
    """
    f = _write_kimi_fixture(tmp_path)
    all_text = "\n".join(t["content"] for t in parse_kimi_session(f)["turns"])

    assert "Continue working toward the active goal." not in all_text
    assert "bun run check completed" not in all_text
    assert "Skill tool loaded instructions." not in all_text
    # the point of the allowlist: an unknown future kind is dropped by default
    assert "some future machine row" not in all_text


def test_kimi_adjacent_tool_calls_stay_readable_when_merged(tmp_path):
    """Two back-to-back tool calls must not run together into one blob.

    Each call becomes a synthetic assistant row that ``_merge_turns`` folds
    into its neighbors; the join has to keep the bracketed names separate.
    """
    f = _write_kimi_fixture(tmp_path)
    all_text = "\n".join(t["content"] for t in parse_kimi_session(f)["turns"])

    assert "[ran tool: Bash]" in all_text
    assert "[ran tool: Read]" in all_text
    assert "[ran tool: Bash][ran tool: Read]" not in all_text
    assert "[ran tool: Bash]\n\n[ran tool: Read]" in all_text


def test_parse_kimi_session_skips_malformed_lines(tmp_path):
    f = _write_kimi_fixture(
        tmp_path, extra_lines=["not json {{{", json.dumps({"type": "context.append_message"})]
    )
    parsed = parse_kimi_session(f)
    assert len(parsed["turns"]) == 3


def test_parse_kimi_session_without_state_json(tmp_path):
    f = _write_kimi_fixture(tmp_path)
    (tmp_path / ".kimi-code" / "sessions" / "wd_user_abc123" / "session_kimi-0001" / "state.json").unlink()
    parsed = parse_kimi_session(f)
    # falls back to the session dir name and the first typed line
    assert parsed["session_id"] == "session_kimi-0001"
    assert parsed["cwd"] is None
    assert parsed["title_guess"].startswith("Summarize the repo")


def test_list_kimi_sessions(tmp_path):
    _write_kimi_fixture(tmp_path)
    kimi = list_kimi_sessions(tmp_path / ".kimi-code" / "sessions")
    assert len(kimi) == 1
    assert kimi[0].source == "kimi"
    assert kimi[0].turn_count == 3
    assert kimi[0].mtime == 1786707200000 / 1000.0
    assert list_kimi_sessions(tmp_path / "nope") == []

    both = gather_foreign_sessions(
        source="kimi", kimi_root=tmp_path / ".kimi-code" / "sessions"
    )
    assert len(both) == 1 and both[0].source == "kimi"


def test_import_kimi_session(tmp_path, session_db):
    f = _write_kimi_fixture(tmp_path)
    session_id = import_foreign_session("@kimi", f, db=session_db)
    row = session_db.get_session(session_id)
    assert row is not None
    assert row["source"] == "kimi-code"
    assert row["cwd"] == "/home/user/kproj"
    assert row["message_count"] == 3
    title = session_db.get_session_title(session_id)
    assert title.startswith("Imported from Kimi Code: ")
    assert "Summarize the repo" in title
    messages = session_db.get_messages(session_id)
    _assert_alternating(messages)
    origin = json.loads(row["origin_json"])
    assert origin["imported_from"]["tool"] == "kimi-code"
    assert origin["imported_from"]["foreign_session_id"] == "session_kimi-0001"
    assert session_db.resolve_session_id(session_id) == session_id


def _rewrite_kimi_state(wire, state):
    """Replace a fixture's state.json with ``state`` (None deletes it)."""
    path = wire.parent.parent.parent / "state.json"
    if state is None:
        path.unlink()
    else:
        path.write_text(json.dumps(state), encoding="utf-8")
    return wire


def test_parse_kimi_session_legacy_state_shape(tmp_path):
    """The older state.json spells cwd ``workDir`` and dates ISO-8601.

    Both shapes exist side by side in a real store, so reading only the
    current keys drops the cwd and the session id on every legacy dir.
    """
    f = _rewrite_kimi_state(
        _write_kimi_fixture(tmp_path),
        {
            "workDir": "/home/user/legacy",
            "title": "New Session",  # Kimi's placeholder, not a name
            "updatedAt": "2026-08-24T00:37:56.953Z",
            "isCustomTitle": False,
        },
    )
    parsed = parse_kimi_session(f)

    assert parsed["cwd"] == "/home/user/legacy"
    # no "id" key in this shape — fall back to the session dir name
    assert parsed["session_id"] == "session_kimi-0001"
    # the placeholder title must not win over the first typed line
    assert parsed["title_guess"].startswith("Summarize the repo")

    listed = list_kimi_sessions(tmp_path / ".kimi-code" / "sessions")
    assert len(listed) == 1
    assert listed[0].mtime == datetime(
        2026, 8, 24, 0, 37, 56, 953000, tzinfo=timezone.utc
    ).timestamp()


def test_list_kimi_sessions_falls_back_to_file_mtime(tmp_path):
    """An unusable ``updatedAt`` must not sort the session into 1970."""
    f = _rewrite_kimi_state(
        _write_kimi_fixture(tmp_path), {"id": "s1", "updatedAt": "not a date"}
    )
    listed = list_kimi_sessions(tmp_path / ".kimi-code" / "sessions")
    assert len(listed) == 1
    assert listed[0].mtime == f.stat().st_mtime
