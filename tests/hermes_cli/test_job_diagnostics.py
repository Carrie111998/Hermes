from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from hermes_cli.job_diagnostics import (
    BlockerKind,
    HeartbeatReporter,
    JobNotFoundError,
    JobRun,
    JobStateStore,
    LaneStatus,
    RepositoryDriftError,
    TimingCategory,
    classify_blocker,
    effective_lane_status,
    evidence_drift_reasons,
    timing_breakdown,
)


class FakeClock:
    def __init__(self, value: float = 100.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


def _non_repo_identity(path) -> dict:
    resolved = str(Path(path).resolve()) if path else None
    return {
        "available": True,
        "worktree": resolved,
        "repo_root": resolved,
        "branch": "test",
        "head": "a" * 40,
        "dirty": False,
        "status_digest": "b" * 64,
    }


def _store(tmp_path: Path, clock: FakeClock | None = None) -> JobStateStore:
    return JobStateStore(
        tmp_path / "state",
        clock=clock or FakeClock(),
        repository_probe=_non_repo_identity,
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "Hermes Tests")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "base")
    return path


def test_timing_accuracy_covers_every_bucket(tmp_path):
    clock = FakeClock()
    store = _store(tmp_path, clock)
    store.start_lane("job-1", "lane-a", worktree=tmp_path)

    cursor = 100.0
    for category, duration in (
        (TimingCategory.MODEL_WAIT, 7.5),
        (TimingCategory.TOOL_EXECUTION, 2.0),
        (TimingCategory.TEST, 4.5),
        (TimingCategory.REVIEW, 3.0),
        (TimingCategory.EVIDENCE_GENERATION, 1.5),
        (TimingCategory.COMPRESSION, 2.5),
    ):
        store.record_span("job-1", "lane-a", category, cursor, cursor + duration)
        cursor += duration

    clock.value = cursor
    store.touch_lane(
        "job-1",
        "lane-a",
        status=LaneStatus.BLOCKED,
        current_step="waiting for operator",
    )
    clock.advance(5)
    metrics = timing_breakdown(store.load("job-1"), now=clock())

    assert metrics["model_wait"] == pytest.approx(7.5)
    assert metrics["tool_execution"] == pytest.approx(2.0)
    assert metrics["test"] == pytest.approx(4.5)
    assert metrics["review"] == pytest.approx(3.0)
    assert metrics["evidence_generation"] == pytest.approx(1.5)
    assert metrics["compression"] == pytest.approx(2.5)
    assert metrics["blocked_idle"] == pytest.approx(5.0)
    assert metrics["total_elapsed"] == pytest.approx(cursor + 5 - 100)


def test_heartbeat_suppresses_duplicate_noise_and_emits_transitions():
    reporter = HeartbeatReporter(
        interval_seconds=10,
        meaningful_output_warning_seconds=20,
        idle_after_seconds=15,
        repeat_after_seconds=30,
    )

    first = reporter.evaluate(
        started_at=100,
        current_step="running tests",
        last_activity_at=108,
        last_meaningful_output_at=108,
        now=110,
    )
    duplicate = reporter.evaluate(
        started_at=100,
        current_step="running tests",
        last_activity_at=113,
        last_meaningful_output_at=108,
        now=115,
    )
    waiting = reporter.evaluate(
        started_at=100,
        current_step="waiting for provider response",
        last_activity_at=118,
        last_meaningful_output_at=108,
        now=120,
    )
    stale_output = reporter.evaluate(
        started_at=100,
        current_step="waiting for provider response",
        last_activity_at=135,
        last_meaningful_output_at=108,
        now=140,
    )
    blocked = reporter.evaluate(
        started_at=100,
        current_step="awaiting approval",
        last_activity_at=140,
        last_meaningful_output_at=108,
        blocker={"kind": "missing_authorization"},
        now=141,
    )
    idle = reporter.evaluate(
        started_at=100,
        current_step="running tests",
        last_activity_at=110,
        last_meaningful_output_at=108,
        now=142,
    )
    dead = reporter.evaluate(
        started_at=100,
        current_step="waiting for provider response",
        last_activity_at=140,
        last_meaningful_output_at=108,
        process_alive=False,
        now=143,
    )

    assert first is not None and first.status == "working"
    assert duplicate is None
    assert waiting is not None and waiting.status == "waiting"
    assert stale_output is not None
    assert "no meaningful output" in stale_output.text
    assert blocked is not None and blocked.status == "blocked"
    assert idle is not None and idle.status == "idle"
    assert dead is not None and dead.status == "dead"


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("unit assertion failed", BlockerKind.CODE_FAILURE),
        ("missing credentials for provider", BlockerKind.MISSING_AUTHORIZATION),
        ("waiting for user input", BlockerKind.OPERATOR_PRESENCE_REQUIREMENT),
        ("another process already running", BlockerKind.EXTERNAL_PROCESS_CONFLICT),
        ("wrong worktree and branch mismatch", BlockerKind.WRONG_WORKTREE_OR_BRANCH),
        ("sha256 hash mismatch", BlockerKind.HASH_MISMATCH),
        ("provider 503 overloaded", BlockerKind.REMOTE_PROVIDER_FAILURE),
        ("stale session heartbeat", BlockerKind.STALE_SESSION),
        ("disk full infrastructure error", BlockerKind.INFRASTRUCTURE_ISSUE),
    ],
)
def test_blocker_classification(detail, expected):
    assert classify_blocker(detail) is expected


def test_test_failure_has_explicit_priority():
    assert (
        classify_blocker(
            "assertion failed",
            test_command=True,
        )
        is BlockerKind.TEST_FAILURE
    )


def test_blocked_state_detection(tmp_path):
    clock = FakeClock()
    store = _store(tmp_path, clock)
    store.start_lane("job", "lane", worktree=tmp_path)
    store.mark_blocked(
        "job",
        "lane",
        blocker=BlockerKind.MISSING_AUTHORIZATION,
        detail="approval required",
        next_action="obtain approval",
    )
    lane = store.load("job")["lanes"]["lane"]

    assert effective_lane_status(lane, now=clock()) == "blocked"
    assert lane["blocker"]["kind"] == "missing_authorization"
    assert lane["next_expected_action"] == "obtain approval"


def test_stale_and_dead_process_detection(tmp_path):
    clock = FakeClock(1_000)
    store = _store(tmp_path, clock)
    store.start_lane("job", "lane", worktree=tmp_path)
    lane = store.load("job")["lanes"]["lane"]
    lane["heartbeat_at"] = 100

    assert (
        effective_lane_status(
            lane,
            now=1_000,
            stale_after=300,
            process_status=lambda _identity: "alive",
        )
        == "stale"
    )
    assert (
        effective_lane_status(
            lane,
            now=1_000,
            stale_after=300,
            process_status=lambda _identity: "dead",
        )
        == "dead"
    )


def test_safe_checkpoint_persists_repo_command_and_evidence(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    store = JobStateStore(tmp_path / "state")
    run = JobRun.start(
        store,
        job_id="job",
        lane_id="build",
        title="Build",
        worktree=repo,
    ).define_phases([
        {
            "phase_id": "evidence",
            "category": TimingCategory.EVIDENCE_GENERATION,
            "command": "python scripts/build-evidence.py",
        }
    ])
    evidence = repo / "evidence.txt"

    result = run.run_phase(
        "evidence",
        lambda: evidence.write_text("proof\n", encoding="utf-8"),
        evidence_paths=[evidence],
        summary="evidence created",
    )
    state = store.load("job")
    lane = state["lanes"]["build"]
    phase = lane["phases"][0]
    checkpoint = lane["checkpoint"]

    assert result.executed is True
    assert phase["status"] == "completed"
    assert phase["command"] == "python scripts/build-evidence.py"
    assert checkpoint["repository"]["branch"] == _git(repo, "branch", "--show-current")
    assert checkpoint["repository"]["head"] == _git(repo, "rev-parse", "HEAD")
    assert checkpoint["repository"]["worktree"] == str(repo.resolve())
    assert checkpoint["evidence"][0]["path"] == str(evidence.resolve())
    assert len(checkpoint["evidence"][0]["sha256"]) == 64
    assert evidence_drift_reasons(checkpoint["evidence"]) == []


def test_resume_skips_completed_phase_without_duplicate_action(tmp_path):
    store = _store(tmp_path)
    run = JobRun.start(
        store,
        job_id="job",
        lane_id="lane",
        title="Idempotent",
        worktree=tmp_path,
    ).define_phases([{"phase_id": "once", "command": "do-once"}])
    calls = []

    first = run.run_phase("once", lambda: calls.append("ran"))
    resumed = JobRun(store, "job", "lane")
    second = resumed.run_phase("once", lambda: calls.append("duplicate"))

    assert first.executed is True
    assert second.executed is False
    assert calls == ["ran"]


def test_repository_drift_refuses_resume_before_action(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    store = JobStateStore(tmp_path / "state")
    run = JobRun.start(
        store,
        job_id="job",
        lane_id="lane",
        title="Drift guard",
        worktree=repo,
    ).define_phases([
        {"phase_id": "one", "command": "phase-one"},
        {"phase_id": "two", "command": "phase-two"},
    ])
    run.run_phase("one", lambda: "done")
    (repo / "tracked.txt").write_text("drifted\n", encoding="utf-8")
    called = False

    def action():
        nonlocal called
        called = True

    with pytest.raises(RepositoryDriftError) as exc:
        run.run_phase("two", action)

    assert called is False
    assert exc.value.blocker is BlockerKind.WRONG_WORKTREE_OR_BRANCH
    plan = store.resume_plan("job", "lane")
    assert plan.safe is False
    assert plan.blocker == "wrong_worktree_or_branch"


def test_evidence_hash_mismatch_refuses_resume(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    store = JobStateStore(tmp_path / "state")
    evidence = repo / "proof.txt"
    run = JobRun.start(
        store,
        job_id="job",
        lane_id="lane",
        title="Hash guard",
        worktree=repo,
    ).define_phases([
        {"phase_id": "one", "command": "phase-one"},
        {"phase_id": "two", "command": "phase-two"},
    ])
    run.run_phase(
        "one",
        lambda: evidence.write_text("original\n", encoding="utf-8"),
        evidence_paths=[evidence],
    )
    # Keep the repository fingerprint unchanged while altering only the
    # evidence hash by storing evidence outside git's tracked set.
    before = store.load("job")["lanes"]["lane"]["checkpoint"]["repository"]
    evidence.write_text("tampered\n", encoding="utf-8")
    after = store.repository_probe(repo)
    assert before["status_digest"] == after["status_digest"]

    plan = store.resume_plan("job", "lane")
    assert plan.safe is False
    assert plan.blocker == "hash_mismatch"


def test_concurrent_lane_accounting_unions_overlap_and_keeps_updates(tmp_path):
    clock = FakeClock(100)
    store = _store(tmp_path, clock)
    store.start_lane("job", "a", worktree=tmp_path)
    store.start_lane("job", "b", worktree=tmp_path)

    barrier = threading.Barrier(3)

    def record(lane_id: str, start: float, end: float):
        barrier.wait()
        store.record_span(
            "job",
            lane_id,
            TimingCategory.MODEL_WAIT,
            start,
            end,
        )

    one = threading.Thread(target=record, args=("a", 100, 110))
    two = threading.Thread(target=record, args=("b", 105, 115))
    one.start()
    two.start()
    barrier.wait()
    one.join()
    two.join()

    clock.value = 120
    state = store.load("job")
    metrics = timing_breakdown(state, now=clock())
    assert len(state["spans"]) == 2
    assert metrics["model_wait"] == pytest.approx(15)
    assert metrics["busy_wall"] == pytest.approx(15)
    assert metrics["parallel_overlap"] == pytest.approx(5)
    assert timing_breakdown(state, now=clock(), lane_id="a")["model_wait"] == 10
    assert timing_breakdown(state, now=clock(), lane_id="b")["model_wait"] == 10


def test_missing_and_malformed_state_files_are_nonfatal(tmp_path):
    store = _store(tmp_path)
    states, issues = store.list_states()
    assert states == []
    assert issues == []
    with pytest.raises(JobNotFoundError):
        store.load("missing")

    store.root.mkdir(parents=True)
    (store.root / "broken.json").write_text("{not-json", encoding="utf-8")
    (store.root / "wrong-shape.json").write_text(
        json.dumps({"schema_version": 1, "job_id": "x"}),
        encoding="utf-8",
    )
    (store.root / "bad-lane.json").write_text(
        json.dumps({
            "schema_version": 1,
            "job_id": "bad-lane",
            "lanes": {"lane": "not-an-object"},
            "spans": [],
        }),
        encoding="utf-8",
    )
    states, issues = store.list_states()

    assert states == []
    assert {issue.kind for issue in issues} == {"malformed_json", "invalid_state"}
    assert len(issues) == 3
