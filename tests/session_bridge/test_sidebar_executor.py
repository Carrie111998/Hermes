from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from session_bridge.models import BridgeMarkerPayload, Provider
from session_bridge.sidebar import SidebarCandidate, VerifiedSidebarThread, sidebar_bridge_id
from session_bridge.sidebar_executor import (
    NativeCreateAmbiguous,
    SidebarExecutionResult,
    SidebarExecutor,
)


SOURCE_1 = "claude:source-1"
SOURCE_2 = "claude:source-2"
THREAD_1 = "11111111-1111-4111-8111-111111111111"
THREAD_2 = "22222222-2222-4222-8222-222222222222"
SECRET = b"sidebar-executor-test-secret"


@dataclass
class FakeClock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _candidate(source: str) -> SidebarCandidate:
    return SidebarCandidate(
        source_session_id=source,
        provider=Provider.CLAUDE,
        bridge_id=sidebar_bridge_id(source),
        title=f"[Claude] {source}",
        cwd="C:/source",
        git_root="C:/source",
        git_branch="main",
        git_head="a" * 40,
        worktree_id=None,
        eligible_at=10.0,
    )


def _job(source: str, *, thread_id: str | None = None) -> dict[str, Any]:
    return {
        "id": f"sidebar-job:{source}",
        "source_session_id": source,
        "bridge_id": sidebar_bridge_id(source),
        "state": "sidebar_pending",
        "codex_thread_id": thread_id,
    }


class FakeStore:
    def __init__(
        self,
        events: list[tuple[Any, ...]],
        jobs: list[dict[str, Any]],
        *,
        bind_error: Exception | None = None,
        commit_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.jobs = jobs
        self.bind_error = bind_error
        self.commit_error = commit_error
        self.candidates = {
            job["source_session_id"]: _candidate(job["source_session_id"])
            for job in jobs
        }
        self.failures: list[str] = []

    def claim_sidebar_jobs(
        self, *, now: float, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        self.events.append(("claim", limit, lease_seconds, now))
        claimed = self.jobs[:limit]
        self.jobs = self.jobs[limit:]
        return [
            {
                **job,
                "state": "sidebar_leased",
                "lease_token": f"lease:{job['id']}",
                "lease_expires_at": now + lease_seconds,
            }
            for job in claimed
        ]

    def get_sidebar_candidate_for_delivery(self, source_session_id: str) -> SidebarCandidate:
        self.events.append(("candidate", source_session_id))
        return self.candidates[source_session_id]

    def bind_sidebar_thread(
        self, *, lease_token: str, codex_thread_id: str, now: float
    ) -> dict[str, Any]:
        self.events.append(("bind", codex_thread_id))
        if self.bind_error is not None:
            raise self.bind_error
        return {
            "state": "sidebar_leased",
            "codex_thread_id": codex_thread_id,
            "lease_token": lease_token,
            "updated_at": now,
        }

    def commit_sidebar_job_with_lineage(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        source_session_id: str,
        bridge_id: str,
        now: float,
    ) -> dict[str, Any]:
        self.events.append(("commit", codex_thread_id))
        if self.commit_error is not None:
            raise self.commit_error
        return {
            "state": "sidebar_visible",
            "codex_thread_id": codex_thread_id,
            "source_session_id": source_session_id,
            "bridge_id": bridge_id,
            "lease_token": lease_token,
            "updated_at": now,
        }

    def fail_sidebar_job(
        self, *, lease_token: str, error_code: str, now: float
    ) -> dict[str, Any]:
        del lease_token, now
        self.events.append(("fail", error_code))
        self.failures.append(error_code)
        state = (
            "sidebar_failed"
            if error_code == "native_create_ambiguous"
            else "sidebar_retry"
        )
        return {"state": state, "error_code": error_code}


class FakeVerifier:
    def __init__(
        self,
        events: list[tuple[Any, ...]],
        *,
        find_result: VerifiedSidebarThread | None = None,
    ) -> None:
        self.events = events
        self.find_result = find_result

    def find_by_marker(
        self, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread | None:
        self.events.append(("find", expected.source_session_id))
        return self.find_result

    def verify_thread(
        self, *, thread_id: str, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread:
        self.events.append(("verify", thread_id))
        return VerifiedSidebarThread(
            thread_id=thread_id,
            source_session_id=expected.source_session_id,
            bridge_id=expected.bridge_id,
        )


class FakeNative:
    def __init__(
        self,
        events: list[tuple[Any, ...]],
        *,
        create_result: str | Exception = THREAD_1,
        statuses: list[str | None] | None = None,
        rename_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.create_result = create_result
        self.statuses = list(statuses or ["idle"])
        self.rename_error = rename_error
        self.create_calls = 0

    def create_thread(self, *, prompt: str, candidate: SidebarCandidate) -> str:
        assert candidate.cwd == "C:/source"
        assert "Signed marker:" in prompt
        self.events.append(("create", candidate.source_session_id))
        self.create_calls += 1
        if isinstance(self.create_result, Exception):
            raise self.create_result
        return self.create_result

    def read_thread_status(self, *, thread_id: str) -> str | None:
        self.events.append(("read", thread_id))
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]

    def rename_thread(self, *, thread_id: str, title: str) -> None:
        assert title.startswith("[Claude]")
        self.events.append(("rename", thread_id))
        if self.rename_error is not None:
            raise self.rename_error


def _executor(
    store: FakeStore,
    verifier: FakeVerifier,
    native: FakeNative,
    clock: FakeClock,
    *,
    read_timeout_seconds: float = 60.0,
) -> SidebarExecutor:
    return SidebarExecutor(
        store=store,
        verifier=verifier,
        native=native,
        marker_secret=SECRET,
        clock=clock,
        monotonic=clock,
        sleep=clock.sleep,
        read_timeout_seconds=read_timeout_seconds,
        poll_interval=1.0,
    )


def test_recovered_exact_id_is_verified_renamed_and_committed_without_create() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(events)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result == SidebarExecutionResult(
        status="visible",
        job_id=f"sidebar-job:{SOURCE_1}",
        thread_id=THREAD_1,
    )
    assert native.create_calls == 0
    assert [event[0] for event in events] == [
        "claim",
        "candidate",
        "bind",
        "read",
        "verify",
        "rename",
        "commit",
    ]


def test_zero_marker_candidates_creates_once_and_binds_before_any_read() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(events, create_result=THREAD_1)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.status == "visible"
    assert result.thread_id == THREAD_1
    assert [event[0] for event in events] == [
        "claim",
        "candidate",
        "find",
        "create",
        "bind",
        "read",
        "verify",
        "rename",
        "commit",
    ]


def test_ambiguous_create_is_quarantined_and_never_replaced() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)])
    native = FakeNative(events, create_result=NativeCreateAmbiguous())
    executor = _executor(store, FakeVerifier(events), native, clock)

    first = executor.run_once()
    second = executor.run_once()

    assert first == SidebarExecutionResult(
        status="failed",
        job_id=f"sidebar-job:{SOURCE_1}",
        error_code="native_create_ambiguous",
    )
    assert second == SidebarExecutionResult(status="idle")
    assert native.create_calls == 1
    assert store.failures == ["native_create_ambiguous"]
    assert not any(event[0] in {"bind", "read", "rename", "commit"} for event in events)


def test_bind_ambiguity_releases_once_without_read_rename_or_commit() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1)], bind_error=TimeoutError())
    native = FakeNative(events)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.error_code == "bridge_temporarily_unavailable"
    assert result.status == "retry"
    assert store.failures == ["bridge_temporarily_unavailable"]
    assert [event[0] for event in events] == [
        "claim",
        "candidate",
        "find",
        "create",
        "bind",
        "fail",
    ]


def test_read_until_idle_timeout_is_retryable_and_never_renames() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(events, statuses=["active"])
    executor = _executor(
        store,
        FakeVerifier(events),
        native,
        clock,
        read_timeout_seconds=2.0,
    )

    result = executor.run_once()

    assert result.error_code == "native_task_not_indexed"
    assert result.status == "retry"
    assert sum(event[0] == "read" for event in events) == 3
    assert not any(event[0] in {"verify", "rename", "commit"} for event in events)


def test_rename_failure_retries_without_commit_or_replacement() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)])
    native = FakeNative(events, rename_error=RuntimeError())
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.error_code == "rename_failed"
    assert result.status == "retry"
    assert native.create_calls == 0
    assert [event[0] for event in events][-2:] == ["rename", "fail"]
    assert not any(event[0] == "commit" for event in events)


def test_commit_ambiguity_releases_once_without_replacement() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1, thread_id=THREAD_1)], commit_error=TimeoutError())
    native = FakeNative(events)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.error_code == "bridge_temporarily_unavailable"
    assert result.status == "retry"
    assert native.create_calls == 0
    assert [event[0] for event in events][-2:] == ["commit", "fail"]
    assert store.failures == ["bridge_temporarily_unavailable"]


def test_run_once_claims_and_processes_only_one_job_serially() -> None:
    events: list[tuple[Any, ...]] = []
    clock = FakeClock()
    store = FakeStore(events, [_job(SOURCE_1), _job(SOURCE_2)])
    native = FakeNative(events, create_result=THREAD_1)
    executor = _executor(store, FakeVerifier(events), native, clock)

    result = executor.run_once()

    assert result.job_id == f"sidebar-job:{SOURCE_1}"
    assert [job["source_session_id"] for job in store.jobs] == [SOURCE_2]
    assert [event for event in events if event[0] == "claim"] == [
        ("claim", 1, 300, 100.0)
    ]
    assert native.create_calls == 1
    assert not any(SOURCE_2 in map(str, event) for event in events)
