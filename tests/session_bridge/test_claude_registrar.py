from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from hermes_state import SessionDB

from session_bridge.claude_registrar import ClaudeNativeRegistrar, _WinPtyProcess
from session_bridge.claude_visibility import (
    ClaudeVisibilityCandidate,
    ClaudeVisibilityClaim,
    build_claude_registration_prompt,
    derive_claude_visibility_identity,
)
from session_bridge.models import OriginKind, ProjectedMessage, Provider, SessionProjection
from session_bridge.store import SessionBridgeStore


SECRET = b"registrar-test-marker-secret"


def candidate() -> ClaudeVisibilityCandidate:
    return ClaudeVisibilityCandidate(
        source_session_id="codex:source-1",
        source_provider=Provider.CODEX,
        native_name="[Codex] Explain the registrar",
        source_cwd="C:/exact/project/subdir",
        git_root="C:/exact/project",
        git_branch="main",
        git_head="abc123",
        worktree_id="worktree-1",
        eligible_at=10.0,
    )


def claim(**changes: Any) -> ClaudeVisibilityClaim:
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    base = ClaudeVisibilityClaim(
        status="claimed",
        lease_kind="launch",
        job_id=identity.job_id,
        source_session_id=value.source_session_id,
        source_provider=value.source_provider,
        reserved_claude_uuid=identity.claude_uuid,
        native_name=value.native_name,
        source_cwd=value.source_cwd,
        git_root=value.git_root,
        git_branch=value.git_branch,
        git_head=value.git_head,
        worktree_id=value.worktree_id,
        signed_marker=identity.signed_marker,
        lease_digest="a" * 64,
        attempt_ordinal=1,
        registration_reserved=True,
        launch_permitted=True,
    )
    return replace(base, **changes)


@dataclass
class FakeParse:
    projection: SessionProjection


class FakeSource:
    def __init__(self, projections: list[SessionProjection | None] | None = None):
        self.projections = list(projections or [None])
        self.lookups: list[str] = []

    def find_native_session(self, native_id: str) -> Path | None:
        self.lookups.append(native_id)
        item = self.projections.pop(0) if len(self.projections) > 1 else self.projections[0]
        self.current = item
        return None if item is None else Path(f"/{native_id}.jsonl")

    def parse(self, path: Path) -> FakeParse:
        assert self.current is not None
        return FakeParse(self.current)

    def projection_has_exact_marker(self, projection: SessionProjection, marker: str) -> bool:
        return any(marker in (message.content or "") for message in projection.messages)


class FakeStore:
    def __init__(self):
        self.calls: list[tuple[Any, ...]] = []

    def commit_claude_visibility_job(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("commit", *args))
        return {"state": "claude_visible"}

    def retry_claude_visibility_job(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("retry", *args))
        return {"state": "claude_retry"}

    def fail_claude_visibility_job(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("fail", *args))
        return {"state": "claude_failed"}

    def record_claude_visibility_exact_id_absent(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("absent", *args))
        return {"state": "claude_retry"}


class FakePty:
    def __init__(self, output: str = "REGISTERED\r\n", exit_code: int = 0, read_error: Exception | None = None):
        self.output = output
        self.exit_code = exit_code
        self.read_error = read_error
        self.writes: list[str] = []
        self.waits: list[float] = []
        self.terminated = False
        self.closed = False

    def read_until(self, timeout: float, *, prompt: str | None = None) -> str:
        if self.read_error:
            raise self.read_error
        return self.output

    def write(self, data: str) -> None:
        self.writes.append(data)

    def wait(self, timeout: float) -> int:
        self.waits.append(timeout)
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, process: FakePty | None = None, error: Exception | None = None):
        self.process = process or FakePty()
        self.error = error
        self.spawns: list[tuple[list[str], str]] = []

    def spawn(self, argv: list[str], *, cwd: str):
        self.spawns.append((argv, cwd))
        if self.error:
            raise self.error
        return self.process


def projection_for(item: ClaudeVisibilityClaim, *, response: str = "REGISTERED", **changes: Any) -> SessionProjection:
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    prompt = build_claude_registration_prompt(value, identity, SECRET)
    base = SessionProjection(
        provider=Provider.CLAUDE,
        native_id=item.reserved_claude_uuid or "",
        title=item.native_name,
        cwd=item.source_cwd,
        started_at=10.0,
        last_active=11.0,
        messages=[
            ProjectedMessage("u1", 0, "user", prompt, 10.0),
            ProjectedMessage("a1", 0, "assistant", response, 11.0),
        ],
        native_path=f"/{item.reserved_claude_uuid}.jsonl",
        native_hash="b" * 64,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=identity.bridge_id,
    )
    return replace(base, **changes)


def registrar(source: FakeSource, factory: FakeFactory, store: FakeStore | None = None, **kwargs: Any):
    return ClaudeNativeRegistrar(
        store or FakeStore(), source, marker_secret=SECRET, pty_factory=factory,
        clock=lambda: 100.0, monotonic=lambda: 1.0, sleep=lambda _value: None,
        process_timeout=2.0, exit_timeout=1.0, discovery_timeout=0.0,
        retry_delay=5.0, **kwargs,
    )


def test_launch_uses_exact_interactive_argv_cwd_and_one_redacted_prompt() -> None:
    item = claim()
    process = FakePty(output="\x1b[?2004hClaude>\x1b[0m REGISTERED\r\n")
    factory = FakeFactory(process)
    source = FakeSource([None, projection_for(item)])
    result = registrar(source, factory).process(item)

    assert result.status == "visible"
    assert factory.spawns == [([
        "claude", "--session-id", item.reserved_claude_uuid, "--name", item.native_name,
        "--model", "haiku", "--tools", "", "--permission-mode", "dontAsk",
    ], item.source_cwd)]
    argv = factory.spawns[0][0]
    assert "--print" not in argv and "-p" not in argv
    assert len(process.writes) == 2
    expected = build_claude_registration_prompt(candidate(), derive_claude_visibility_identity(candidate(), SECRET), SECRET)
    assert process.writes[0] == f"\x1b[200~{expected}\x1b[201~\r"
    assert process.writes[1] == "/exit\r"
    assert "tool_calls" not in process.writes[0]
    assert process.closed and process.waits == [1.0]


def test_terminal_echo_and_ansi_are_removed_before_exact_response_check() -> None:
    item = claim()
    expected = build_claude_registration_prompt(
        candidate(), derive_claude_visibility_identity(candidate(), SECRET), SECRET
    )
    echoed = "\r\n".join(
        [f"\x1b[32mClaude>\x1b[0m {expected.splitlines()[0]}", *expected.splitlines()[1:]]
    )
    process = FakePty(output=f"{echoed}\r\n\x1b[32mREGISTERED\x1b[0m\r\n")
    result = registrar(
        FakeSource([None, projection_for(item)]), FakeFactory(process)
    ).process(item)
    assert result.status == "visible"


@pytest.mark.parametrize("output", ["NOT REGISTERED", "REGISTERED later", "xREGISTERED", "REGISTERED\nextra"])
def test_registration_response_requires_exact_bounded_token(output: str) -> None:
    item = claim()
    store = FakeStore()
    process = FakePty(output=output)
    result = registrar(FakeSource(), FakeFactory(process), store).process(item)
    assert result.status == "failed"
    assert result.error_code == "bridge_conflict"
    assert store.calls[0][0] == "fail"
    assert process.closed and process.terminated


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"lease_kind": "reconciliation", "registration_reserved": True}, "bridge_conflict"),
        ({"lease_kind": "launch", "launch_permitted": False}, "bridge_conflict"),
        ({"lease_kind": None}, "bridge_conflict"),
    ],
)
def test_inconsistent_claim_authority_is_rejected_before_spawn(changes: dict[str, Any], code: str) -> None:
    factory = FakeFactory()
    result = registrar(FakeSource(), factory).process(claim(**changes))
    assert result.status == "failed" and result.error_code == code
    assert factory.spawns == []


def test_reconciliation_exact_match_commits_without_spawn() -> None:
    item = claim(lease_kind="reconciliation", launch_permitted=False, registration_reserved=False,
                 requires_exact_id_reconciliation=True)
    store = FakeStore()
    factory = FakeFactory()
    result = registrar(FakeSource([projection_for(item)]), factory, store).process(item)
    assert result.status == "visible"
    assert factory.spawns == []
    assert store.calls[0][0] == "commit"


def test_reconciliation_absence_is_recorded_and_never_launches_same_cycle() -> None:
    item = claim(lease_kind="reconciliation", launch_permitted=False, registration_reserved=False,
                 requires_exact_id_reconciliation=True)
    store = FakeStore()
    factory = FakeFactory()
    result = registrar(FakeSource([None]), factory, store).process(item)
    assert result.status == "absent"
    assert store.calls[0][0] == "absent"
    assert factory.spawns == []


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"native_id": "00000000-0000-4000-8000-000000000000"}, "uuid_conflict"),
        ({"title": "wrong"}, "name_conflict"),
        ({"cwd": "C:/wrong"}, "cwd_conflict"),
        ({"origin_bridge_id": "wrong"}, "bridge_conflict"),
    ],
)
def test_reconciliation_conflicts_fail(changes: dict[str, Any], code: str) -> None:
    item = claim(lease_kind="reconciliation", launch_permitted=False, registration_reserved=False,
                 requires_exact_id_reconciliation=True)
    store = FakeStore()
    result = registrar(FakeSource([projection_for(item, **changes)]), FakeFactory(), store).process(item)
    assert result.status == "failed" and result.error_code == code
    assert store.calls[0][0] == "fail"


def test_reconciliation_fails_an_exact_uuid_with_wrong_authenticated_marker() -> None:
    item = claim(
        lease_kind="reconciliation",
        launch_permitted=False,
        registration_reserved=False,
        requires_exact_id_reconciliation=True,
    )
    projection = projection_for(item)
    messages = list(projection.messages)
    messages[0] = replace(messages[0], content="forged registration prompt")
    store = FakeStore()
    result = registrar(
        FakeSource([replace(projection, messages=messages)]), FakeFactory(), store
    ).process(item)
    assert result.status == "failed" and result.error_code == "marker_conflict"


def test_delayed_exact_transcript_is_polled_without_replacement() -> None:
    item = claim()
    source = FakeSource([None, projection_for(item)])
    factory = FakeFactory()
    ticks = iter([0.0, 0.0, 0.1])
    reg = ClaudeNativeRegistrar(FakeStore(), source, marker_secret=SECRET, pty_factory=factory,
        clock=lambda: 100.0, monotonic=lambda: next(ticks), sleep=lambda _: None,
        process_timeout=2, exit_timeout=1, discovery_timeout=1, retry_delay=5)
    result = reg.process(item)
    assert result.status == "visible"
    assert source.lookups == [item.reserved_claude_uuid, item.reserved_claude_uuid]
    assert len(factory.spawns) == 1


@pytest.mark.parametrize(
    ("factory", "process", "code"),
    [
        (FakeFactory(error=FileNotFoundError()), None, "claude_executable_unavailable"),
        (FakeFactory(error=RuntimeError("pty unavailable")), None, "pty_unavailable"),
        (None, FakePty(output="Authentication required"), "claude_authentication_unavailable"),
        (None, FakePty(exit_code=7), "clean_exit_not_observed"),
        (None, FakePty(read_error=TimeoutError()), "creation_ambiguous"),
    ],
)
def test_fixed_launch_failure_codes_and_cleanup(factory: FakeFactory | None, process: FakePty | None, code: str) -> None:
    item = claim()
    factory = factory or FakeFactory(process)
    result = registrar(FakeSource(), factory).process(item)
    assert result.error_code == code
    if process is not None:
        assert process.closed and process.terminated
    assert result.detail not in {"pty unavailable", "Authentication required"}


def test_winpty_wrapper_uses_real_read_signature_without_unbounded_keyword() -> None:
    class Process:
        def __init__(self):
            self.calls = 0

        def read(self, size: int = 1024) -> str:
            self.calls += 1
            return "REGISTERED\r\n"

    process = Process()
    assert _WinPtyProcess(process).read_until(0.2).strip() == "REGISTERED"
    assert process.calls == 1


def test_winpty_reader_does_not_stop_on_registered_text_inside_prompt_echo() -> None:
    value = candidate()
    prompt = build_claude_registration_prompt(
        value, derive_claude_visibility_identity(value, SECRET), SECRET
    )

    class Process:
        def __init__(self):
            self.chunks = iter([prompt + "\r\n", "REGISTERED\r\n"])
            self.calls = 0

        def read(self, size: int = 1024) -> str:
            self.calls += 1
            return next(self.chunks)

    process = Process()
    output = _WinPtyProcess(process).read_until(0.2, prompt=prompt)
    assert output.strip() == "REGISTERED"
    assert process.calls == 2


def test_winpty_reader_ignores_startup_chrome_and_wrapped_prompt_fragments() -> None:
    value = candidate()
    prompt = build_claude_registration_prompt(
        value, derive_claude_visibility_identity(value, SECRET), SECRET
    )

    class Process:
        def __init__(self):
            self.chunks = iter([
                "Claude Code ready\r\nstatus: connected\r\n",
                "Signed marker: wrapped-fragment\r\nmetadata continuation\r\n",
                "\x1b[32mClaude>\x1b[0m REGISTERED\r\n",
            ])
            self.calls = 0

        def read(self, size: int = 1024) -> str:
            self.calls += 1
            return next(self.chunks)

    process = Process()
    output = _WinPtyProcess(process).read_until(0.2, prompt=prompt)
    assert output.strip() == "REGISTERED"
    assert process.calls == 3


def test_winpty_reader_never_treats_authentication_words_in_echo_as_failure() -> None:
    class Process:
        def __init__(self):
            self.chunks = iter([
                "Bounded metadata: authentication required\r\n",
                "REGISTERED\r\n",
            ])
            self.calls = 0

        def read(self, size: int = 1024) -> str:
            self.calls += 1
            return next(self.chunks)

    process = Process()
    output = _WinPtyProcess(process).read_until(
        0.2, prompt="Bounded metadata: authentication required"
    )
    assert output.strip() == "REGISTERED"
    assert process.calls == 2


def test_paid_launch_exact_reconciles_existing_uuid_before_any_spawn() -> None:
    item = claim()
    store = FakeStore()
    factory = FakeFactory()
    result = registrar(FakeSource([projection_for(item)]), factory, store).process(item)
    assert result.status == "visible"
    assert factory.spawns == []
    assert store.calls[0][0] == "commit"


def test_restart_reconciliation_commits_exact_uuid_without_second_spawn_or_usage(tmp_path: Path) -> None:
    now = [100.0]
    database = SessionDB(tmp_path / "state.db")
    first_store = SessionBridgeStore(database, clock=lambda: now[0], local_timezone=timezone.utc)
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    first_store.enqueue_claude_visibility_job(value, identity, SECRET)
    first = first_store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    assert first.lease_kind == "launch"
    first_factory = FakeFactory(FakePty(read_error=TimeoutError()))
    ambiguous = registrar(FakeSource(), first_factory, first_store).process(first)
    assert ambiguous.error_code == "creation_ambiguous"

    now[0] = 105.0
    restarted_store = SessionBridgeStore(database, clock=lambda: now[0], local_timezone=timezone.utc)
    reconciliation = restarted_store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    assert reconciliation.lease_kind == "reconciliation"
    restarted_factory = FakeFactory()
    visible = registrar(
        FakeSource([projection_for(reconciliation)]), restarted_factory, restarted_store
    ).process(reconciliation)

    assert visible.status == "visible"
    assert len(first_factory.spawns) == 1 and restarted_factory.spawns == []
    assert restarted_store.claude_visibility_status(now[0])["usage"]["attempts"] == 1
    gate = restarted_store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    assert gate.status == "no_due_job" and gate.lease_kind is None
    database.close()


def test_zero_result_ambiguity_records_absence_then_authorizes_same_uuid_only(tmp_path: Path) -> None:
    now = [100.0]
    database = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(database, clock=lambda: now[0], local_timezone=timezone.utc)
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    store.enqueue_claude_visibility_job(value, identity, SECRET)
    first = store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    first_factory = FakeFactory(FakePty(read_error=TimeoutError()))
    registrar(FakeSource(), first_factory, store).process(first)

    now[0] = 105.0
    reconciliation = store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    reconciliation_factory = FakeFactory()
    absent = registrar(FakeSource([None]), reconciliation_factory, store).process(reconciliation)
    assert absent.status == "absent" and reconciliation_factory.spawns == []
    assert store.claude_visibility_status(now[0])["usage"]["attempts"] == 1

    second = store.claim_claude_visibility_job(now[0], 60, 25, "0.50", "0.02")
    assert second.lease_kind == "launch"
    assert second.reserved_claude_uuid == first.reserved_claude_uuid == identity.claude_uuid
    assert second.attempt_ordinal == 2
    assert store.claude_visibility_status(now[0])["usage"]["attempts"] == 2
    assert len(first_factory.spawns) == 1
    database.close()


def test_nonlease_store_gate_has_no_lease_kind(tmp_path: Path) -> None:
    database = SessionDB(tmp_path / "state.db")
    store = SessionBridgeStore(database, clock=lambda: 100.0, local_timezone=timezone.utc)
    value = candidate()
    identity = derive_claude_visibility_identity(value, SECRET)
    store.enqueue_claude_visibility_job(value, identity, SECRET)
    gated = store.claim_claude_visibility_job(100.0, 60, 25, "0.01", "0.02")
    assert gated.status == "cost_limit" and gated.lease_kind is None
    database.close()


def test_offline_interactive_fixture_records_frames_exit_and_delayed_index(tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    fixture = Path(__file__).parent / "fixtures" / "fake_interactive_claude.py"
    env = {
        **os.environ,
        "FAKE_CLAUDE_RECORD": str(record),
        "FAKE_CLAUDE_SCENARIO": "delayed_transcript_indexing",
        "FAKE_CLAUDE_INDEX_DELAY": "0.01",
    }
    process = subprocess.Popen(
        [sys.executable, str(fixture), "--session-id", "offline-uuid"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b"\x1b[200~offline prompt\x1b[201~\r")
    process.stdin.flush()
    assert b"REGISTERED" in process.stdout.readline()
    process.stdin.write(b"/exit\n")
    process.stdin.flush()
    assert process.wait(timeout=2) == 0

    events = json.loads(record.read_text(encoding="utf-8"))
    assert events[0] == {
        "argv": ["--session-id", "offline-uuid"],
        "cwd": str(tmp_path),
        "event": "spawn",
    }
    assert [event["event"] for event in events] == [
        "spawn", "stdin", "native_created", "index_ready", "stdin", "exit"
    ]
