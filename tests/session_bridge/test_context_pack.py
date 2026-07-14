from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from session_bridge.context_pack import ContextPackBuilder, ContextPackRequest
from session_bridge.models import (
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
)
from session_bridge.store import SessionBridgeStore


SECTION_HEADINGS = (
    "## Identity / Snapshot",
    "## Goal / Latest Intent",
    "## Decisions and Constraints",
    "## Unresolved Work",
    "## Recent Turns",
    "## Files",
    "## Repository State",
    "## Referenced MemPalace / GBrain Links",
    "## Warnings",
)


@pytest.fixture
def db(tmp_path: Path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def _message(
    event_id: str,
    role: str,
    content: str | None,
    *,
    timestamp: float,
    tool_name: str | None = None,
    tool_calls: list[dict] | None = None,
    tool_call_id: str | None = None,
) -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=event_id,
        ordinal=0,
        role=role,
        content=content,
        timestamp=timestamp,
        tool_name=tool_name,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
    )


def _projection(
    messages: list[ProjectedMessage],
    *,
    native_id: str = "source-1",
    cwd: str | None = None,
    cursor: str = "cursor-exact",
    source_hash: str = "sha256:exact",
    git_branch: str | None = "feature/handoff",
) -> SessionProjection:
    return SessionProjection(
        provider=Provider.CLAUDE,
        native_id=native_id,
        title="Build the bridge",
        cwd=cwd,
        started_at=100.0,
        last_active=max((message.timestamp for message in messages), default=100.0),
        messages=messages,
        native_path=f"C:/claude/{native_id}.jsonl",
        native_status="active",
        native_cursor=cursor,
        native_hash=source_hash,
        parser_version=3,
        git_branch=git_branch,
    )


def _request(
    *,
    budget: int = 8000,
    cursor: str = "cursor-exact",
    source_hash: str = "sha256:exact",
    stale: bool = False,
    diverged: bool = False,
) -> ContextPackRequest:
    return ContextPackRequest(
        source_session_id="claude:source-1",
        target_provider=Provider.CODEX,
        bridge_id="bridge-7",
        source_cursor=cursor,
        source_hash=source_hash,
        budget_chars=budget,
        stale=stale,
        diverged=diverged,
    )


def _seed(db: SessionDB, messages: list[ProjectedMessage], **projection_kwargs):
    store = SessionBridgeStore(db, clock=lambda: 900.0)
    store.upsert_projection(_projection(messages, **projection_kwargs))
    return store


def _section(payload: str, heading: str) -> str:
    start = payload.index(heading) + len(heading)
    following = [payload.find(candidate, start) for candidate in SECTION_HEADINGS]
    stops = [position for position in following if position >= 0]
    stop = min(stops) if stops else len(payload)
    return payload[start:stop]


def test_pack_has_exact_section_order_and_snapshot_identity(db: SessionDB):
    store = _seed(
        db,
        [
            _message("u1", "user", "Build a safe handoff.", timestamp=101.0),
            _message("a1", "assistant", "I will implement it.", timestamp=102.0),
        ],
    )

    pack = ContextPackBuilder(db, store).build(_request())

    positions = [pack.payload.index(heading) for heading in SECTION_HEADINGS]
    assert positions == sorted(positions)
    identity = _section(pack.payload, SECTION_HEADINGS[0])
    assert "Bridge ID: bridge-7" in identity
    assert "Source canonical ID: claude:source-1" in identity
    assert "Source provider: claude" in identity
    assert "Source native ID: source-1" in identity
    assert "Target provider: codex" in identity
    assert "Source cursor: cursor-exact" in identity
    assert "Source hash: sha256:exact" in identity
    assert "Snapshot timestamp: 102.000000" in identity
    assert pack.source_cursor == "cursor-exact"
    assert pack.source_hash == "sha256:exact"
    assert pack.created_at == 102.0
    persisted = store.get_context_pack("bridge-7", budget_chars=8000)
    assert persisted is not None
    assert persisted["id"] == pack.id
    assert persisted["payload"] == pack.payload


def test_goal_decision_constraint_and_unresolved_work_are_extracted(db: SessionDB):
    store = _seed(
        db,
        [
            _message(
                "u1", "user", "Original goal: bridge the sessions.", timestamp=101.0
            ),
            _message(
                "a1",
                "assistant",
                "Decision: use immutable snapshots.\nConstraint: never write provider JSONL.",
                timestamp=102.0,
            ),
            _message(
                "u2",
                "user",
                "TODO: add the coordinator. Latest intent: finish context packs first.",
                timestamp=103.0,
            ),
        ],
    )

    payload = ContextPackBuilder(db, store).build(_request()).payload

    goal = _section(payload, SECTION_HEADINGS[1])
    decisions = _section(payload, SECTION_HEADINGS[2])
    unresolved = _section(payload, SECTION_HEADINGS[3])
    assert "Original goal: bridge the sessions." in goal
    assert "Latest intent: finish context packs first." in goal
    assert "Decision: use immutable snapshots." in decisions
    assert "Constraint: never write provider JSONL." in decisions
    assert "TODO: add the coordinator." in unresolved


def test_recent_turns_keep_latest_and_collapse_tool_noise(db: SessionDB):
    store = _seed(
        db,
        [
            _message("u1", "user", "Old user turn", timestamp=101.0),
            _message(
                "a-tool",
                "assistant",
                "I will inspect it.",
                timestamp=102.0,
                tool_calls=[{"id": "call-1", "name": "read_file"}],
            ),
            _message(
                "t1",
                "tool",
                "very noisy tool output that must not be copied",
                timestamp=103.0,
                tool_name="read_file",
                tool_call_id="call-1",
            ),
            _message(
                "t2",
                "tool",
                "more very noisy tool output",
                timestamp=104.0,
                tool_name="read_file",
                tool_call_id="call-1",
            ),
            _message("a2", "assistant", "Newest assistant turn", timestamp=105.0),
            _message("u2", "user", "Newest user intent", timestamp=106.0),
        ],
    )

    recent = _section(
        ContextPackBuilder(db, store).build(_request()).payload,
        SECTION_HEADINGS[4],
    )

    assert "I will inspect it." in recent
    assert "[tool activity collapsed: 3 events]" in recent
    assert "very noisy tool output" not in recent
    assert recent.index("Old user turn") < recent.index("Newest user intent")


def test_file_references_are_ranked_by_frequency_then_recency(db: SessionDB):
    store = _seed(
        db,
        [
            _message(
                "u1",
                "user",
                "Inspect docs/once.md and src/repeated.py.",
                timestamp=101.0,
            ),
            _message(
                "a1",
                "assistant",
                "Edited src/repeated.py and tests/test_context.py.",
                timestamp=102.0,
                tool_calls=[
                    {"name": "write_file", "input": {"path": "src/repeated.py"}}
                ],
            ),
        ],
    )

    files = _section(
        ContextPackBuilder(db, store).build(_request()).payload,
        SECTION_HEADINGS[5],
    )

    assert files.index("src/repeated.py") < files.index("tests/test_context.py")
    assert files.index("tests/test_context.py") < files.index("docs/once.md")
    assert "references: 3" in files


def test_git_metadata_uses_exact_safe_subprocess_contract(
    db: SessionDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    store = _seed(
        db,
        [_message("u1", "user", "Check the repository.", timestamp=101.0)],
        cwd=str(cwd),
    )
    with db._lock:
        conn = db._conn
        assert conn is not None
        conn.execute(
            "UPDATE sessions SET git_repo_root = ? WHERE id = ?",
            (str(cwd), "claude:source-1"),
        )
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="## feature/handoff\n M session_bridge/context_pack.py\n",
            stderr="",
        )

    monkeypatch.setattr("session_bridge.context_pack.subprocess.run", fake_run)

    payload = ContextPackBuilder(db, store).build(_request()).payload

    assert calls == [
        (
            ["git", "-C", str(cwd), "status", "--short", "--branch"],
            {
                "timeout": 3,
                "check": False,
                "capture_output": True,
                "text": True,
            },
        )
    ]
    repository = _section(payload, SECTION_HEADINGS[6])
    assert f"Cwd: {cwd}" in repository
    assert f"Repository root: {cwd}" in repository
    assert "Recorded branch: feature/handoff" in repository
    assert "M session_bridge/context_pack.py" in repository


def test_missing_and_non_repository_cwd_warn_without_failing(
    db: SessionDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = _seed(
        db,
        [_message("u1", "user", "Continue.", timestamp=101.0)],
        cwd=None,
    )

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("git must not run without a cwd")

    monkeypatch.setattr("session_bridge.context_pack.subprocess.run", must_not_run)
    missing = ContextPackBuilder(db, store).build(_request()).payload
    assert "[missing cwd]" in _section(missing, SECTION_HEADINGS[8])

    nonrepo = tmp_path / "plain-directory"
    nonrepo.mkdir()
    with db._lock:
        conn = db._conn
        assert conn is not None
        conn.execute(
            "UPDATE sessions SET cwd = ? WHERE id = ?",
            (str(nonrepo), "claude:source-1"),
        )

    def not_a_repo(_command, **_kwargs):
        return SimpleNamespace(
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository",
        )

    monkeypatch.setattr("session_bridge.context_pack.subprocess.run", not_a_repo)
    non_repository = (
        ContextPackBuilder(db, store)
        .build(replace(_request(), source_hash="sha256:second"))
        .payload
    )
    assert "[repository unavailable]" in _section(non_repository, SECTION_HEADINGS[8])


def test_stale_diverged_and_index_identity_mismatch_are_visible(db: SessionDB):
    store = _seed(
        db,
        [_message("u1", "user", "Continue.", timestamp=101.0)],
    )

    payload = (
        ContextPackBuilder(db, store)
        .build(
            _request(
                cursor="older-cursor",
                source_hash="sha256:older",
                stale=True,
                diverged=True,
            )
        )
        .payload
    )

    warnings = _section(payload, SECTION_HEADINGS[8])
    assert "[stale source]" in warnings
    assert "[diverged]" in warnings
    assert "[snapshot identity mismatch]" in warnings
    identity = _section(payload, SECTION_HEADINGS[0])
    assert "Source cursor: older-cursor" in identity
    assert "Source hash: sha256:older" in identity


def test_memory_references_are_copied_without_requiring_backends(db: SessionDB):
    store = _seed(
        db,
        [
            _message(
                "u1",
                "user",
                "Use mempalace://drawer/hermes-123 and "
                "gbrain://page/systems/session-bridge. Ignore https://example.com.",
                timestamp=101.0,
            )
        ],
    )

    links = _section(
        ContextPackBuilder(db, store).build(_request()).payload,
        SECTION_HEADINGS[7],
    )

    assert "mempalace://drawer/hermes-123" in links
    assert "gbrain://page/systems/session-bridge" in links
    assert "example.com" not in links


def test_truncation_is_bounded_keeps_newest_turns_and_is_explicit(db: SessionDB):
    messages = [
        _message(
            f"u{index}",
            "user",
            f"turn-{index} " + (chr(96 + index) * 240),
            timestamp=100.0 + index,
        )
        for index in range(1, 9)
    ]
    store = _seed(db, messages)

    pack = ContextPackBuilder(db, store).build(_request(budget=1200))

    assert len(pack.payload) <= 1200
    assert "[context truncated]" in _section(pack.payload, SECTION_HEADINGS[8])
    recent = _section(pack.payload, SECTION_HEADINGS[4])
    assert "turn-8" in recent
    assert "turn-1" not in recent


def test_truncation_reserves_at_least_45_percent_for_recent_raw_turns(
    db: SessionDB,
):
    messages = [
        _message(
            f"u{index}",
            "user",
            (
                f"turn-{index} Decision: keep snapshot {index}. "
                f"TODO: finish work {index}. src/file_{index}.py "
                f"mempalace://drawer/item-{index} " + (chr(96 + index) * 340)
            ),
            timestamp=100.0 + index,
        )
        for index in range(1, 7)
    ]
    store = _seed(db, messages)
    budget = 1800

    payload = ContextPackBuilder(db, store).build(_request(budget=budget)).payload

    identity = _section(payload, SECTION_HEADINGS[0]).strip("\n")
    warnings = _section(payload, SECTION_HEADINGS[8]).strip("\n")
    fixed_bodies = {
        SECTION_HEADINGS[0]: identity,
        SECTION_HEADINGS[8]: warnings,
    }
    fixed = (
        "\n\n".join(
            f"{heading}\n{fixed_bodies.get(heading, '')}"
            for heading in SECTION_HEADINGS
        )
        + "\n"
    )
    available = budget - len(fixed)
    recent = _section(payload, SECTION_HEADINGS[4]).strip("\n")
    assert len(recent) >= math.ceil(available * 0.45) - 1


def test_build_is_repeatable_and_hydrated_pack_is_immutable(db: SessionDB):
    store = _seed(
        db,
        [_message("u1", "user", "Original snapshot", timestamp=101.0)],
    )
    store.upsert_projection(
        SessionProjection(
            provider=Provider.CODEX,
            native_id="target-1",
            title="Target",
            cwd=None,
            started_at=100.0,
            last_active=101.0,
            messages=[_message("target-u1", "user", "Placeholder", timestamp=101.0)],
            native_cursor="target-cursor",
            native_hash="target-hash",
        )
    )
    store.create_link(
        SessionLink(
            id="link-bridge-7",
            from_session_id="claude:source-1",
            to_session_id="codex:target-1",
            relation=Relation.CONTINUES,
            bridge_id="bridge-7",
            source_cursor="cursor-exact",
            source_hash="sha256:exact",
            created_at=110.0,
        )
    )
    builder = ContextPackBuilder(db, store)

    first = builder.build(_request())
    replay = builder.build(_request())
    assert replay == first
    assert first.target_session_id == "codex:target-1"

    store.mark_hydrated(
        "bridge-7",
        source_cursor="cursor-exact",
        source_hash="sha256:exact",
        pack_id=first.id,
    )
    store.upsert_projection(
        _projection(
            [
                _message("u1", "user", "Original snapshot", timestamp=101.0),
                _message("u2", "user", "Unexpected mutation", timestamp=102.0),
            ],
            cursor="cursor-exact",
            source_hash="sha256:exact",
        )
    )

    after_hydration = builder.build(_request())
    assert after_hydration.id == first.id
    assert after_hydration.payload == first.payload
    assert "Unexpected mutation" not in after_hydration.payload
    assert after_hydration.immutable_at == 900.0


def test_immutable_pack_lookup_never_crosses_source_identity(db: SessionDB):
    store = _seed(
        db,
        [_message("u1", "user", "Source one", timestamp=101.0)],
    )
    first = ContextPackBuilder(db, store).build(_request())
    with db._lock:
        conn = db._conn
        assert conn is not None
        conn.execute(
            "UPDATE session_context_packs SET immutable_at = 901.0 WHERE id = ?",
            (first.id,),
        )
    store.upsert_projection(
        _projection(
            [_message("u2", "user", "Source two", timestamp=102.0)],
            native_id="source-2",
        )
    )

    with pytest.raises(ValueError, match="source identity mismatch"):
        ContextPackBuilder(db, store).build(
            replace(_request(), source_session_id="claude:source-2")
        )


def test_secrets_are_redacted_but_ordinary_ids_are_preserved(db: SessionDB):
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    source_id = "claude:source-1"
    secrets = {
        "bearer-secret-value-1234567890",
        "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        "AKIAIOSFODNN7EXAMPLE",
        "correct-horse-battery-staple",
        "generic-token-value-123456",
    }
    content = (
        "Authorization: Bearer bearer-secret-value-1234567890\n"
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456\n"
        "github=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890\n"
        "aws=AKIAIOSFODNN7EXAMPLE\n"
        "password=correct-horse-battery-staple\n"
        "token=generic-token-value-123456\n"
        f"ordinary_uuid={uuid}\nsource_id={source_id}"
    )
    store = _seed(db, [_message("u1", "user", content, timestamp=101.0)])

    payload = ContextPackBuilder(db, store).build(_request()).payload

    for secret in secrets:
        assert secret not in payload
    assert payload.count("[REDACTED]") >= 6
    assert uuid in payload
    assert source_id in payload


def test_different_snapshot_identity_produces_a_different_stable_pack(db: SessionDB):
    store = _seed(
        db,
        [_message("u1", "user", "Snapshot", timestamp=101.0)],
    )
    builder = ContextPackBuilder(db, store)

    first = builder.build(_request())
    second = builder.build(_request(cursor="cursor-next", source_hash="sha256:next"))

    assert first.id != second.id
    assert "Source cursor: cursor-next" in second.payload
    with db._lock:
        conn = db._conn
        assert conn is not None
        count = conn.execute(
            "SELECT COUNT(*) FROM session_context_packs WHERE bridge_id = ?",
            ("bridge-7",),
        ).fetchone()[0]
    assert count == 2
