from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timezone
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Mapping

import pytest

from hermes_state import SessionDB

from session_bridge.characterize import (
    CharacterizationAuthenticationFailure,
    build_characterization_auth_recovery_prompt,
    _prepare_safe_root,
    _read_characterization_record,
    _validate_characterization_transcript,
    _write_characterization_record,
    characterize_claude_visibility,
    cleanup_characterized_claude_visibility,
    retire_aborted_claude_visibility_characterization,
)
from session_bridge.cli import ProductionBackend, _claude_visibility_preflight
from session_bridge.cli import main
from session_bridge.config import BridgeConfig
from session_bridge.claude_registrar import ClaudeRegistrarOutcome
from session_bridge.claude_visibility import (
    ClaudeVisibilityCandidate,
    ClaudeVisibilityClaim,
    build_claude_visibility_candidate,
    build_claude_registration_prompt,
    derive_claude_visibility_identity,
)
from session_bridge.models import (
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
)
from session_bridge.store import SessionBridgeStore


SECRET = b"characterization-marker-secret"


def _aborted_characterization_state(
    root: Path, operation_id: str
) -> tuple[dict[str, Any], Path]:
    disposable = root / f"claude-visibility-{operation_id}"
    disposable.mkdir()
    (disposable / ".session-bridge-characterization.json").write_text(
        json.dumps(
            {"operation_id": operation_id, "nonce": "abort-nonce"},
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    candidate = ClaudeVisibilityCandidate(
        source_session_id=f"codex:{operation_id}",
        source_provider=Provider.CODEX,
        native_name="[Codex] disposable abort probe",
        source_cwd=str(disposable),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=100.0,
    )
    identity = derive_claude_visibility_identity(candidate, SECRET)
    return (
        {
            "schema_version": 2,
            "operation_id": operation_id,
            "phase": "launching",
            "created_at": 100.0,
            "expires_at": 200.0,
            "source_provider": Provider.CODEX.value,
            "source_session_id": candidate.source_session_id,
            "bridge_id": identity.bridge_id,
            "job_id": identity.job_id,
            "reserved_claude_uuid": identity.claude_uuid,
            "native_name": candidate.native_name,
            "source_cwd": candidate.source_cwd,
            "signed_marker": identity.signed_marker,
            "transcript_path": None,
            "transcript_identity": None,
            "sentinel_nonce": "abort-nonce",
            "cleanup_authorized_at": None,
            "cleanup_capability_hash": "a" * 64,
        },
        disposable,
    )


def test_exact_absence_abort_retires_active_record_and_allows_fresh_reservation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sources"
    projects = tmp_path / "projects"
    root.mkdir()
    projects.mkdir()
    operation_id = "77777777-7777-4777-8777-777777777777"
    state, disposable = _aborted_characterization_state(root, operation_id)
    active = root / ".claude-visibility-operation.json"
    _write_characterization_record(active, state, SECRET)

    first = retire_aborted_claude_visibility_characterization(
        source_root=root,
        marker_secret=SECRET,
        expected_job_id=str(state["job_id"]),
        expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
        expected_operation_id=operation_id,
        now=lambda: 150.0,
    )
    replay = retire_aborted_claude_visibility_characterization(
        source_root=root,
        marker_secret=SECRET,
        expected_job_id=str(state["job_id"]),
        expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
        expected_operation_id=operation_id,
        now=lambda: 160.0,
    )

    assert (
        first
        == replay
        == {
            "status": "retired",
            "job_id": state["job_id"],
            "reserved_claude_uuid": state["reserved_claude_uuid"],
            "active_record_retired": True,
        }
    )
    assert not active.exists()
    assert not disposable.exists()
    archived = root / ".abort-completed" / f"{operation_id}.json"
    assert _read_characterization_record(archived, SECRET)["phase"] == "aborted"

    fresh: list[SessionProjection] = []

    def stop_after_reservation(projection: SessionProjection) -> ClaudeVisibilityClaim:
        fresh.append(projection)
        raise RuntimeError("fresh_reservation_reached")

    with pytest.raises(RuntimeError, match="fresh_reservation_reached"):
        characterize_claude_visibility(
            source_root=root,
            projects_root=projects,
            reserve=stop_after_reservation,
            registrar=object(),
            restarted_source=lambda: pytest.fail("fresh reserve must happen first"),
            marker_secret=SECRET,
            now=lambda: 170.0,
        )
    assert len(fresh) == 1
    assert fresh[0].native_id != operation_id


@pytest.mark.parametrize("move_before_crash", [False, True])
def test_exact_absence_abort_replays_crash_around_active_claim_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    move_before_crash: bool,
) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    operation_id = "99999999-9999-4999-8999-999999999999"
    state, disposable = _aborted_characterization_state(root, operation_id)
    active = root / ".claude-visibility-operation.json"
    claimed = root / ".abort-claims" / f"{operation_id}.json"
    _write_characterization_record(active, state, SECRET)
    actual_replace = os.replace

    class SimulatedCrash(BaseException):
        pass

    def crash_at_claim_rename(source: object, destination: object) -> None:
        if Path(source) == active and Path(destination) == claimed:
            if move_before_crash:
                actual_replace(source, destination)
            raise SimulatedCrash
        actual_replace(source, destination)

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            "session_bridge.characterize.os.replace", crash_at_claim_rename
        )
        with pytest.raises(SimulatedCrash):
            retire_aborted_claude_visibility_characterization(
                source_root=root,
                marker_secret=SECRET,
                expected_job_id=str(state["job_id"]),
                expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
                expected_operation_id=operation_id,
                now=lambda: 150.0,
            )

    replay = retire_aborted_claude_visibility_characterization(
        source_root=root,
        marker_secret=SECRET,
        expected_job_id=str(state["job_id"]),
        expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
        expected_operation_id=operation_id,
        now=lambda: 160.0,
    )

    assert replay["status"] == "retired"
    assert not active.exists()
    assert not claimed.exists()
    assert not disposable.exists()
    assert (
        _read_characterization_record(
            root / ".abort-completed" / f"{operation_id}.json", SECRET
        )["phase"]
        == "aborted"
    )


@pytest.mark.parametrize("move_before_crash", [False, True])
def test_exact_absence_abort_replays_crash_around_disposable_quarantine_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    move_before_crash: bool,
) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    operation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    state, disposable = _aborted_characterization_state(root, operation_id)
    active = root / ".claude-visibility-operation.json"
    quarantine = root / ".abort-quarantine" / operation_id
    _write_characterization_record(active, state, SECRET)
    actual_replace = os.replace

    class SimulatedCrash(BaseException):
        pass

    def crash_at_quarantine_rename(source: object, destination: object) -> None:
        if Path(source) == disposable and Path(destination) == quarantine:
            if move_before_crash:
                actual_replace(source, destination)
            raise SimulatedCrash
        actual_replace(source, destination)

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            "session_bridge.characterize.os.replace", crash_at_quarantine_rename
        )
        with pytest.raises(SimulatedCrash):
            retire_aborted_claude_visibility_characterization(
                source_root=root,
                marker_secret=SECRET,
                expected_job_id=str(state["job_id"]),
                expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
                expected_operation_id=operation_id,
                now=lambda: 150.0,
            )

    replay = retire_aborted_claude_visibility_characterization(
        source_root=root,
        marker_secret=SECRET,
        expected_job_id=str(state["job_id"]),
        expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
        expected_operation_id=operation_id,
        now=lambda: 160.0,
    )

    assert replay["status"] == "retired"
    assert not active.exists()
    assert not disposable.exists()
    assert not quarantine.exists()


def test_exact_absence_abort_replays_after_crash_during_deferred_quarantine_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    operation_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    state, disposable = _aborted_characterization_state(root, operation_id)
    (disposable / "payload.txt").write_text("disposable", encoding="utf-8")
    active = root / ".claude-visibility-operation.json"
    claimed = root / ".abort-claims" / f"{operation_id}.json"
    completed = root / ".abort-completed" / f"{operation_id}.json"
    quarantine = root / ".abort-quarantine" / operation_id
    _write_characterization_record(active, state, SECRET)

    class SimulatedCrash(BaseException):
        pass

    def crash_during_delete(path: Path, _state: Mapping[str, Any]) -> None:
        (path / ".session-bridge-characterization.json").unlink()
        (path / "payload.txt").unlink()
        raise SimulatedCrash

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            "session_bridge.characterize._safe_remove_disposable",
            crash_during_delete,
        )
        with pytest.raises(SimulatedCrash):
            retire_aborted_claude_visibility_characterization(
                source_root=root,
                marker_secret=SECRET,
                expected_job_id=str(state["job_id"]),
                expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
                expected_operation_id=operation_id,
                now=lambda: 150.0,
            )

    assert completed.exists()
    assert not active.exists()
    assert not claimed.exists()
    assert quarantine.exists()

    replay = retire_aborted_claude_visibility_characterization(
        source_root=root,
        marker_secret=SECRET,
        expected_job_id=str(state["job_id"]),
        expected_reserved_claude_uuid=str(state["reserved_claude_uuid"]),
        expected_operation_id=operation_id,
        now=lambda: 160.0,
    )

    assert replay["status"] == "retired"
    assert not active.exists()
    assert not claimed.exists()
    assert completed.exists()
    # Identity loss during best-effort deletion is retained for manual cleanup,
    # never reinterpreted as authority to delete an untrusted directory.
    assert quarantine.exists()


def test_characterization_fails_closed_while_authenticated_abort_claim_is_open(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sources"
    projects = tmp_path / "projects"
    root.mkdir()
    projects.mkdir()
    operation_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    state, disposable = _aborted_characterization_state(root, operation_id)
    active = root / ".claude-visibility-operation.json"
    claim_root = root / ".abort-claims"
    claim_root.mkdir()
    claimed = claim_root / f"{operation_id}.json"
    _write_characterization_record(active, state, SECRET)
    os.replace(active, claimed)

    with pytest.raises(RuntimeError, match="characterization_abort_in_progress"):
        characterize_claude_visibility(
            source_root=root,
            projects_root=projects,
            reserve=lambda _projection: pytest.fail(
                "open abort claim must block a fresh reservation"
            ),
            registrar=object(),
            restarted_source=lambda: pytest.fail(
                "open abort claim must block native discovery"
            ),
            marker_secret=SECRET,
            now=lambda: 150.0,
        )

    assert claimed.exists()
    assert disposable.exists()
    assert not active.exists()


def test_characterization_fails_closed_while_authenticated_cleanup_claim_is_open(
    tmp_path: Path,
) -> None:
    pending, state, _registrar, _restarted_source = _pending_characterization(tmp_path)
    operation_id = pending["cleanup_token"]["id"]
    active = state["source_root"] / ".claude-visibility-operation.json"
    claim_root = state["source_root"] / ".cleanup-claims"
    claim_root.mkdir()
    claimed = claim_root / f"{operation_id}.json"
    authorized = _read_characterization_record(active, SECRET)
    authorized["cleanup_authorized_at"] = 11.0
    _write_characterization_record(active, authorized, SECRET)
    os.replace(active, claimed)

    with pytest.raises(RuntimeError, match="characterization_cleanup_in_progress"):
        characterize_claude_visibility(
            source_root=state["source_root"],
            projects_root=state["projects_root"],
            reserve=lambda _projection: pytest.fail(
                "open cleanup claim must block a fresh reservation"
            ),
            registrar=object(),
            restarted_source=lambda: pytest.fail(
                "open cleanup claim must block native discovery"
            ),
            marker_secret=SECRET,
            now=lambda: 11.0,
        )

    assert claimed.exists()
    assert state["transcript"].exists()
    assert Path(state["claim"].source_cwd).exists()


def test_characterization_persists_exact_launching_identity_before_reservation_call(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    state: dict[str, Any] = {}
    reserve_calls: list[ClaudeVisibilityClaim] = []

    class SimulatedCrash(BaseException):
        pass

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        reserve_calls.append(claim)
        state.update(claim=claim, marker=marker)
        if len(reserve_calls) == 1:
            # Models a crash after the store's atomic registration boundary but
            # before the callback can return its exact launch lease.
            raise SimulatedCrash
        transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
        transcript.parent.mkdir(exist_ok=True)
        transcript.write_text("native", encoding="utf-8")
        state["transcript"] = transcript
        return claim

    def restarted_source() -> _RestartedSource:
        claim = state["claim"]
        transcript = state.get(
            "transcript",
            projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl",
        )
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=11.0,
            messages=_successful_characterization_messages(claim, state["marker"]),
            native_path=str(transcript),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(transcript, projection, state["marker"])

    with pytest.raises(SimulatedCrash):
        characterize_claude_visibility(
            source_root=source_root,
            projects_root=projects_root,
            reserve=reserve,
            registrar=_Registrar(),
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: 10.0,
        )

    active = source_root / ".claude-visibility-operation.json"
    persisted = _read_characterization_record(active, SECRET)
    first_claim = reserve_calls[0]
    assert persisted["phase"] == "launching"
    assert persisted["job_id"] == first_claim.job_id
    assert persisted["reserved_claude_uuid"] == first_claim.reserved_claude_uuid
    assert persisted["signed_marker"] == first_claim.signed_marker

    recovered = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        registrar=_Registrar(),
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 11.0,
    )

    assert recovered["reserved_claude_uuid"] == first_claim.reserved_claude_uuid
    assert {claim.job_id for claim in reserve_calls} == {first_claim.job_id}
    assert {claim.reserved_claude_uuid for claim in reserve_calls} == {
        first_claim.reserved_claude_uuid
    }


def test_characterization_serializes_concurrent_root_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    first_uuid_entered = threading.Event()
    release_first_uuid = threading.Event()
    uuid_calls: list[str] = []
    generated = iter([
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    ])

    def controlled_operation_id() -> str:
        value = next(generated)
        uuid_calls.append(value)
        if len(uuid_calls) == 1:
            first_uuid_entered.set()
            assert release_first_uuid.wait(5)
        return value

    monkeypatch.setattr(
        "session_bridge.characterize._new_characterization_operation_id",
        controlled_operation_id,
    )
    registrations: dict[str, tuple[ClaudeVisibilityClaim, str, Path]] = {}
    reserve_calls: list[str] = []
    state_lock = threading.Lock()

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
        transcript.parent.mkdir(exist_ok=True)
        transcript.write_text("native", encoding="utf-8")
        with state_lock:
            reserve_calls.append(claim.job_id or "")
            registrations[claim.reserved_claude_uuid or ""] = (
                claim,
                marker,
                transcript,
            )
        return claim

    def restarted_source() -> _RestartedSource:
        operation = _read_characterization_record(
            source_root / ".claude-visibility-operation.json", SECRET
        )
        reserved_uuid = str(operation["reserved_claude_uuid"])
        claim, marker, transcript = registrations[reserved_uuid]
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=reserved_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=11.0,
            messages=_successful_characterization_messages(claim, marker),
            native_path=str(transcript),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(transcript, projection, marker)

    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(
                characterize_claude_visibility(
                    source_root=source_root,
                    projects_root=projects_root,
                    reserve=reserve,
                    registration_is_visible=lambda _operation: True,
                    registrar=_Registrar(),
                    restarted_source=restarted_source,
                    marker_secret=SECRET,
                    now=lambda: 10.0,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert first_uuid_entered.wait(5)
    second.start()
    time.sleep(0.1)
    release_first_uuid.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(uuid_calls) == 1
    assert len(reserve_calls) == 1
    assert len(results) == 2
    assert {result["reserved_claude_uuid"] for result in results} == {
        results[0]["reserved_claude_uuid"]
    }


def _claude_216_runner(
    calls: list[tuple[list[str], dict[str, Any]]],
    *,
    auth_payload: str = '{"loggedIn":true}',
) -> Any:
    def runner(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        stdout = "2.1.216 (Claude Code)" if argv[-1] == "--version" else auth_payload
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    return runner


def _successful_characterization_messages(
    claim: ClaudeVisibilityClaim, marker: str, timestamp: float = 10.0
) -> list[ProjectedMessage]:
    candidate = ClaudeVisibilityCandidate(
        source_session_id=claim.source_session_id or "",
        source_provider=Provider.CODEX,
        native_name=claim.native_name or "",
        source_cwd=claim.source_cwd or "",
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=0.0,
    )
    identity = derive_claude_visibility_identity(candidate, SECRET)
    assert identity.signed_marker == marker
    return [
        ProjectedMessage(
            "u",
            0,
            "user",
            build_claude_registration_prompt(candidate, identity, SECRET),
            timestamp,
        ),
        ProjectedMessage("a", 0, "assistant", "REGISTERED", timestamp + 1.0),
    ]


@dataclass
class _Parsed:
    projection: SessionProjection


class _RestartedSource:
    def __init__(self, path: Path, projection: SessionProjection, marker: str) -> None:
        self.path = path
        self.projection = projection
        self.marker = marker
        self.lookups: list[str] = []

    def find_native_sessions(self, native_id: str) -> list[Path]:
        self.lookups.append(native_id)
        return [self.path] if self.path.exists() else []

    def parse(self, _path: Path) -> _Parsed:
        return _Parsed(self.projection)

    def projection_has_exact_marker(
        self, projection: SessionProjection, marker: str
    ) -> bool:
        return projection is self.projection and marker == self.marker


class _Registrar:
    def __init__(self) -> None:
        self.claims: list[ClaudeVisibilityClaim] = []

    def process(self, claim: ClaudeVisibilityClaim) -> ClaudeRegistrarOutcome:
        self.claims.append(claim)
        return ClaudeRegistrarOutcome(
            "visible", claim.job_id, claim.reserved_claude_uuid
        )


def test_characterization_rejects_exact_uuid_transcript_with_auth_failure(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    transcript = projects_root / "exact" / "reserved.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("native", encoding="utf-8")
    source_projection = SessionProjection(
        provider=Provider.CODEX,
        native_id="operation",
        title="Claude native visibility characterization",
        cwd=str(tmp_path / "source"),
        started_at=10.0,
        last_active=10.0,
        messages=[ProjectedMessage("source", 0, "user", "request", 10.0)],
        native_path=str(tmp_path / "source" / "source.json"),
        native_hash="0" * 64,
        origin_kind=OriginKind.NATIVE,
    )
    claim, marker = _claim_for(source_projection)
    transcript = transcript.with_name(f"{claim.reserved_claude_uuid}.jsonl")
    transcript.write_text("native", encoding="utf-8")
    projection = SessionProjection(
        provider=Provider.CLAUDE,
        native_id=claim.reserved_claude_uuid,
        title=claim.native_name,
        cwd=claim.source_cwd,
        started_at=10.0,
        last_active=11.0,
        messages=[
            _successful_characterization_messages(claim, marker)[0],
            ProjectedMessage(
                "a",
                0,
                "assistant",
                "Failed to authenticate. API Error: 401 Invalid authentication credentials",
                11.0,
            ),
        ],
        native_path=str(transcript),
        native_hash="b" * 64,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
    )

    with pytest.raises(CharacterizationAuthenticationFailure) as raised:
        _validate_characterization_transcript(
            restarted=_RestartedSource(transcript, projection, marker),
            projects_root=projects_root,
            reserved_uuid=claim.reserved_claude_uuid or "",
            native_name=claim.native_name or "",
            source_cwd=claim.source_cwd or "",
            signed_marker=marker,
            marker_secret=SECRET,
        )
    assert len(raised.value.evidence_digest) == 64

    first_evidence = raised.value.evidence_digest
    projection.messages.extend([
        ProjectedMessage(
            "recovery-user",
            0,
            "user",
            build_characterization_auth_recovery_prompt(
                claim.reserved_claude_uuid or "", marker
            ),
            12.0,
        ),
        ProjectedMessage(
            "recovery-assistant",
            0,
            "assistant",
            "Failed to authenticate. API Error: 401 Invalid authentication credentials",
            13.0,
        ),
    ])
    with pytest.raises(CharacterizationAuthenticationFailure) as repeated:
        _validate_characterization_transcript(
            restarted=_RestartedSource(transcript, projection, marker),
            projects_root=projects_root,
            reserved_uuid=claim.reserved_claude_uuid or "",
            native_name=claim.native_name or "",
            source_cwd=claim.source_cwd or "",
            signed_marker=marker,
            marker_secret=SECRET,
        )
    assert repeated.value.evidence_digest == first_evidence

    over_limit_messages = list(projection.messages[:2])
    recovery_prompt = build_characterization_auth_recovery_prompt(
        claim.reserved_claude_uuid or "", marker
    )
    for index in range(25):
        over_limit_messages.extend([
            ProjectedMessage(
                f"bounded-user-{index}",
                0,
                "user",
                recovery_prompt,
                20.0 + index * 2,
            ),
            ProjectedMessage(
                f"bounded-assistant-{index}",
                0,
                "assistant",
                "Failed to authenticate. API Error: 401 Invalid authentication credentials",
                21.0 + index * 2,
            ),
        ])
    over_limit = replace(projection, messages=over_limit_messages)
    with pytest.raises(
        RuntimeError, match="characterization_identity_mismatch:response"
    ):
        _validate_characterization_transcript(
            restarted=_RestartedSource(transcript, over_limit, marker),
            projects_root=projects_root,
            reserved_uuid=claim.reserved_claude_uuid or "",
            native_name=claim.native_name or "",
            source_cwd=claim.source_cwd or "",
            signed_marker=marker,
            marker_secret=SECRET,
        )

    projection.messages.extend([
        ProjectedMessage(
            "recovery-user-2",
            0,
            "user",
            build_characterization_auth_recovery_prompt(
                claim.reserved_claude_uuid or "", marker
            ),
            14.0,
        ),
        ProjectedMessage("recovery-assistant-2", 0, "assistant", "REGISTERED", 15.0),
    ])
    with pytest.raises(RuntimeError, match="recovery_authority_required"):
        _validate_characterization_transcript(
            restarted=_RestartedSource(transcript, projection, marker),
            projects_root=projects_root,
            reserved_uuid=claim.reserved_claude_uuid or "",
            native_name=claim.native_name or "",
            source_cwd=claim.source_cwd or "",
            signed_marker=marker,
            marker_secret=SECRET,
        )


def test_characterization_accepts_only_exact_2110_resume_scaffold(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    source_projection = SessionProjection(
        provider=Provider.CODEX,
        native_id="operation-scaffold",
        title="Claude native visibility characterization",
        cwd=str(tmp_path / "source"),
        started_at=10.0,
        last_active=10.0,
        messages=[ProjectedMessage("source", 0, "user", "request", 10.0)],
        native_path=str(tmp_path / "source" / "source.json"),
        native_hash="0" * 64,
        origin_kind=OriginKind.NATIVE,
    )
    claim, marker = _claim_for(source_projection)
    transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("native", encoding="utf-8")
    prompt = _successful_characterization_messages(claim, marker)[0]
    recovery_prompt = build_characterization_auth_recovery_prompt(
        claim.reserved_claude_uuid or "", marker
    )
    messages = [
        prompt,
        ProjectedMessage(
            "auth",
            0,
            "assistant",
            "Failed to authenticate. API Error: 401 Invalid authentication credentials",
            11.0,
        ),
        ProjectedMessage("scaffold", 0, "assistant", "No response requested.", 12.0),
        ProjectedMessage("recovery-user", 0, "user", recovery_prompt, 13.0),
        ProjectedMessage("recovery-assistant", 0, "assistant", "REGISTERED", 14.0),
    ]

    def validate(projected_messages: list[ProjectedMessage]) -> Path:
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=14.0,
            messages=projected_messages,
            native_path=str(transcript),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _validate_characterization_transcript(
            restarted=_RestartedSource(transcript, projection, marker),
            projects_root=projects_root,
            reserved_uuid=claim.reserved_claude_uuid or "",
            native_name=claim.native_name or "",
            source_cwd=claim.source_cwd or "",
            signed_marker=marker,
            marker_secret=SECRET,
        )

    with pytest.raises(RuntimeError, match="recovery_authority_required"):
        validate(messages)
    with pytest.raises(
        RuntimeError, match="characterization_identity_mismatch:response"
    ):
        validate([
            *messages[:2],
            replace(messages[2], content="arbitrary"),
            *messages[3:],
        ])


@pytest.mark.parametrize(
    "messages",
    [
        lambda messages: [
            ProjectedMessage("prefix", 0, "user", "prefix", 9.0),
            *messages,
        ],
        lambda messages: [replace(messages[0], ordinal=1), messages[1]],
        lambda messages: [messages[0], replace(messages[1], native_event_id="u")],
        lambda messages: [messages[0], replace(messages[1], tool_name="tool")],
        lambda messages: [messages[0], replace(messages[1], tool_calls=[])],
        lambda messages: [messages[0], replace(messages[1], tool_call_id="call")],
        lambda messages: [messages[0], replace(messages[1], reasoning="hidden")],
    ],
)
def test_characterization_requires_exact_structured_transcript(
    tmp_path: Path, messages: Any
) -> None:
    projects_root = tmp_path / "projects"
    source_projection = SessionProjection(
        provider=Provider.CODEX,
        native_id="operation",
        title="Claude native visibility characterization",
        cwd=str(tmp_path / "source"),
        started_at=10.0,
        last_active=10.0,
        messages=[ProjectedMessage("source", 0, "user", "request", 10.0)],
        native_path=str(tmp_path / "source" / "source.json"),
        native_hash="0" * 64,
        origin_kind=OriginKind.NATIVE,
    )
    claim, marker = _claim_for(source_projection)
    transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("native", encoding="utf-8")
    projection = SessionProjection(
        provider=Provider.CLAUDE,
        native_id=claim.reserved_claude_uuid,
        title=claim.native_name,
        cwd=claim.source_cwd,
        started_at=10.0,
        last_active=11.0,
        messages=messages(_successful_characterization_messages(claim, marker)),
        native_path=str(transcript),
        native_hash="b" * 64,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
    )

    with pytest.raises(
        RuntimeError, match="characterization_identity_mismatch:response"
    ):
        _validate_characterization_transcript(
            restarted=_RestartedSource(transcript, projection, marker),
            projects_root=projects_root,
            reserved_uuid=claim.reserved_claude_uuid or "",
            native_name=claim.native_name or "",
            source_cwd=claim.source_cwd or "",
            signed_marker=marker,
            marker_secret=SECRET,
        )


def _claim_for(projection: SessionProjection) -> tuple[ClaudeVisibilityClaim, str]:
    from session_bridge.claude_visibility import build_claude_visibility_candidate

    candidate = build_claude_visibility_candidate(
        projection, eligible_at=projection.last_active
    )
    identity = derive_claude_visibility_identity(candidate, SECRET)
    return ClaudeVisibilityClaim(
        status="claimed",
        lease_kind="launch",
        job_id=identity.job_id,
        source_session_id=candidate.source_session_id,
        source_provider=candidate.source_provider,
        reserved_claude_uuid=identity.claude_uuid,
        native_name=candidate.native_name,
        source_cwd=candidate.source_cwd,
        signed_marker=identity.signed_marker,
        lease_digest="a" * 64,
        attempt_ordinal=1,
        registration_reserved=True,
        launch_permitted=True,
    ), identity.signed_marker


def _pending_characterization(
    tmp_path: Path,
    *,
    now: float = 10.0,
    source_root: Path | None = None,
    projects_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], _Registrar, Any]:
    source_root = source_root or tmp_path / "sources"
    projects_root = projects_root or tmp_path / "projects"
    projects_root.mkdir()
    state: dict[str, Any] = {}
    registrar = _Registrar()

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
        transcript.parent.mkdir()
        transcript.write_text("native", encoding="utf-8")
        state.update(
            claim=claim,
            marker=marker,
            transcript=transcript,
            messages=_successful_characterization_messages(claim, marker),
        )
        return claim

    def restarted_source() -> _RestartedSource:
        claim = state["claim"]
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=11.0,
            messages=state["messages"],
            native_path=str(state["transcript"]),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(state["transcript"], projection, state["marker"])

    pending = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        registrar=registrar,
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: now,
    )
    state.update(source_root=source_root, projects_root=projects_root)
    return pending, state, registrar, restarted_source


def test_characterization_leaves_transcript_for_operator_then_cleans_on_explicit_second_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    reserved: list[SessionProjection] = []
    state: dict[str, Any] = {}
    registrar = _Registrar()
    restarts = 0

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        reserved.append(projection)
        claim, marker = _claim_for(projection)
        transcript = (
            projects_root / "exact-project" / f"{claim.reserved_claude_uuid}.jsonl"
        )
        transcript.parent.mkdir()
        transcript.write_text("native", encoding="utf-8")
        state.update(claim=claim, marker=marker, transcript=transcript)
        return claim

    def restarted_source() -> _RestartedSource:
        nonlocal restarts
        restarts += 1
        claim = state["claim"]
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=11.0,
            messages=_successful_characterization_messages(claim, state["marker"]),
            native_path=str(state["transcript"]),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(state["transcript"], projection, state["marker"])

    result = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        registrar=registrar,
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 10.0,
    )

    assert len(reserved) == 1
    assert len(registrar.claims) == 1
    assert restarts == 1
    assert reserved[0].cwd == registrar.claims[0].source_cwd
    assert Path(reserved[0].cwd or "").parent == source_root.resolve()
    assert result["reserved_claude_uuid"] == registrar.claims[0].reserved_claude_uuid
    assert result["restart_exact_id_verified"] is True
    assert result["operator_checks"][:3] == [
        "Run /resume in Claude Code and select the deterministic characterization name.",
        "Press Ctrl+A in /resume to verify the exact session across all projects.",
        f"Resume the exact ID with: claude --resume {registrar.claims[0].reserved_claude_uuid}",
    ]
    assert "--cleanup-token" in result["operator_checks"][3]
    assert result["verification"] == "pending_operator_checks"
    assert result["cleanup"] == "pending_explicit_confirmation"
    assert set(result["cleanup_token"]) == {"id", "capability"}
    assert state["transcript"].exists()
    assert Path(reserved[0].cwd or "").exists()

    failed_checkpoint = False

    def fail_after_disposable_checkpoint(
        path: Path, payload: dict[str, Any], secret: bytes
    ) -> None:
        nonlocal failed_checkpoint
        _write_characterization_record(path, payload, secret)
        if payload.get("phase") == "disposable_quarantined" and not failed_checkpoint:
            failed_checkpoint = True
            raise OSError("synthetic cleanup checkpoint interruption")

    monkeypatch.setattr(
        "session_bridge.characterize._write_characterization_record",
        fail_after_disposable_checkpoint,
    )
    with pytest.raises(OSError, match="checkpoint interruption"):
        cleanup_characterized_claude_visibility(
            cleanup_token=result["cleanup_token"],
            source_root=source_root,
            projects_root=projects_root,
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: 11.0,
        )
    monkeypatch.setattr(
        "session_bridge.characterize._write_characterization_record",
        _write_characterization_record,
    )
    cleaned = cleanup_characterized_claude_visibility(
        cleanup_token=result["cleanup_token"],
        source_root=source_root,
        projects_root=projects_root,
        restarted_source=lambda: (_ for _ in ()).throw(
            AssertionError("resumed cleanup must not require provider rediscovery")
        ),
        marker_secret=SECRET,
        now=lambda: 12.0,
    )

    assert cleaned["verification"] == "operator_confirmed"
    assert cleaned["cleanup"] == "removed_exact_characterization"
    assert not state["transcript"].exists()
    assert not Path(reserved[0].cwd or "").exists()


@pytest.mark.parametrize("move_before_crash", [False, True])
def test_cleanup_replays_crash_around_disposable_quarantine_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    move_before_crash: bool,
) -> None:
    pending, state, _registrar, restarted_source = _pending_characterization(tmp_path)
    operation_id = pending["cleanup_token"]["id"]
    disposable = Path(state["claim"].source_cwd)
    quarantine = state["source_root"] / ".cleanup-quarantine" / operation_id
    actual_replace = os.replace

    class SimulatedCrash(BaseException):
        pass

    def crash_at_quarantine_rename(source: object, destination: object) -> None:
        if Path(source) == disposable and Path(destination) == quarantine:
            if move_before_crash:
                actual_replace(source, destination)
            raise SimulatedCrash
        actual_replace(source, destination)

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            "session_bridge.characterize.os.replace", crash_at_quarantine_rename
        )
        with pytest.raises(SimulatedCrash):
            cleanup_characterized_claude_visibility(
                cleanup_token=pending["cleanup_token"],
                source_root=state["source_root"],
                projects_root=state["projects_root"],
                restarted_source=restarted_source,
                marker_secret=SECRET,
                now=lambda: 11.0,
            )

    replay = cleanup_characterized_claude_visibility(
        cleanup_token=pending["cleanup_token"],
        source_root=state["source_root"],
        projects_root=state["projects_root"],
        restarted_source=lambda: pytest.fail(
            "quarantine replay must not rediscover the removed transcript"
        ),
        marker_secret=SECRET,
        now=lambda: 12.0,
    )

    assert replay["cleanup"] == "removed_exact_characterization"
    assert not disposable.exists()
    assert not quarantine.exists()


def test_cleanup_replays_after_crash_during_deferred_quarantine_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending, state, _registrar, restarted_source = _pending_characterization(tmp_path)
    operation_id = pending["cleanup_token"]["id"]
    disposable = Path(state["claim"].source_cwd)
    (disposable / "payload.txt").write_text("disposable", encoding="utf-8")
    claim = state["source_root"] / ".cleanup-claims" / f"{operation_id}.json"
    completed = state["source_root"] / ".cleanup-completed" / f"{operation_id}.json"
    quarantine = state["source_root"] / ".cleanup-quarantine" / operation_id

    class SimulatedCrash(BaseException):
        pass

    def crash_during_delete(path: Path, _state: Mapping[str, Any]) -> None:
        (path / ".session-bridge-characterization.json").unlink()
        (path / "payload.txt").unlink()
        raise SimulatedCrash

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(
            "session_bridge.characterize._safe_remove_disposable",
            crash_during_delete,
        )
        with pytest.raises(SimulatedCrash):
            cleanup_characterized_claude_visibility(
                cleanup_token=pending["cleanup_token"],
                source_root=state["source_root"],
                projects_root=state["projects_root"],
                restarted_source=restarted_source,
                marker_secret=SECRET,
                now=lambda: 11.0,
            )

    assert completed.exists()
    assert not claim.exists()
    assert quarantine.exists()

    replay = cleanup_characterized_claude_visibility(
        cleanup_token=pending["cleanup_token"],
        source_root=state["source_root"],
        projects_root=state["projects_root"],
        restarted_source=lambda: pytest.fail(
            "terminal cleanup replay must not rediscover native state"
        ),
        marker_secret=SECRET,
        now=lambda: 12.0,
    )

    assert replay["cleanup"] == "removed_exact_characterization"
    assert completed.exists()
    assert not claim.exists()
    assert quarantine.exists()


def test_cleanup_terminal_replay_removes_stale_authenticated_claim(
    tmp_path: Path,
) -> None:
    pending, state, _registrar, restarted_source = _pending_characterization(tmp_path)
    operation_id = pending["cleanup_token"]["id"]
    cleanup_characterized_claude_visibility(
        cleanup_token=pending["cleanup_token"],
        source_root=state["source_root"],
        projects_root=state["projects_root"],
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 11.0,
    )
    done = state["source_root"] / ".cleanup-completed" / f"{operation_id}.json"
    claimed_root = state["source_root"] / ".cleanup-claims"
    claimed = claimed_root / f"{operation_id}.json"
    stale = _read_characterization_record(done, SECRET)
    stale["phase"] = "disposable_quarantined"
    _write_characterization_record(claimed, stale, SECRET)

    replay = cleanup_characterized_claude_visibility(
        cleanup_token=pending["cleanup_token"],
        source_root=state["source_root"],
        projects_root=state["projects_root"],
        restarted_source=lambda: pytest.fail("terminal replay must not rediscover"),
        marker_secret=SECRET,
        now=lambda: 12.0,
    )

    assert replay["cleanup"] == "removed_exact_characterization"
    assert not claimed.exists()


def test_ready_cleanup_accepts_complete_strict_operator_continuation_pair(
    tmp_path: Path,
) -> None:
    pending, state, _registrar, restarted_source = _pending_characterization(tmp_path)
    state["messages"].extend([
        ProjectedMessage(
            "operator-user", 0, "user", "verify this exact resumed session", 12.0
        ),
        ProjectedMessage(
            "operator-assistant", 0, "assistant", "verification complete", 13.0
        ),
    ])

    with pytest.raises(
        RuntimeError, match="characterization_identity_mismatch:response"
    ):
        _validate_characterization_transcript(
            restarted=restarted_source(),
            projects_root=state["projects_root"],
            reserved_uuid=state["claim"].reserved_claude_uuid or "",
            native_name=state["claim"].native_name or "",
            source_cwd=state["claim"].source_cwd or "",
            signed_marker=state["marker"],
            marker_secret=SECRET,
            allow_recovered=True,
        )

    cleaned = cleanup_characterized_claude_visibility(
        cleanup_token=pending["cleanup_token"],
        source_root=state["source_root"],
        projects_root=state["projects_root"],
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 11.0,
    )

    assert cleaned["cleanup"] == "removed_exact_characterization"
    assert not state["transcript"].exists()


@pytest.mark.parametrize(
    "suffix",
    [
        [ProjectedMessage("operator-user", 0, "user", "request", 12.0)],
        [
            ProjectedMessage("operator-user", 0, "user", "", 12.0),
            ProjectedMessage("operator-assistant", 0, "assistant", "done", 13.0),
        ],
        [
            ProjectedMessage("operator-user", 1, "user", "request", 12.0),
            ProjectedMessage("operator-assistant", 0, "assistant", "done", 13.0),
        ],
        [
            ProjectedMessage("u", 0, "user", "request", 12.0),
            ProjectedMessage("operator-assistant", 0, "assistant", "done", 13.0),
        ],
        [
            ProjectedMessage("operator-user", 0, "user", "request", 12.0),
            ProjectedMessage(
                "operator-assistant",
                0,
                "assistant",
                "done",
                13.0,
                tool_name="tool",
            ),
        ],
    ],
)
def test_ready_continuation_rejects_incomplete_or_unstructured_suffix(
    tmp_path: Path, suffix: list[ProjectedMessage]
) -> None:
    _pending, state, _registrar, restarted_source = _pending_characterization(tmp_path)
    state["messages"].extend(suffix)

    with pytest.raises(
        RuntimeError, match="characterization_identity_mismatch:response"
    ):
        _validate_characterization_transcript(
            restarted=restarted_source(),
            projects_root=state["projects_root"],
            reserved_uuid=state["claim"].reserved_claude_uuid or "",
            native_name=state["claim"].native_name or "",
            source_cwd=state["claim"].source_cwd or "",
            signed_marker=state["marker"],
            marker_secret=SECRET,
            allow_recovered=True,
            allow_post_ready_continuations=True,
        )


@pytest.mark.parametrize(
    ("auth_payload", "expected"),
    [
        (
            '{"loggedIn": true}',
            {
                "version": "2.1.216",
                "authentication": "available",
                "theme": "light",
            },
        ),
        (
            '{"authenticated": true}',
            None,
        ),
        ('{"loggedIn": false, "authenticated": true}', None),
        ('{"loggedIn": false}', None),
        ('{"authenticated": false}', None),
        ('{"loggedIn": "true"}', None),
        ("not-json", None),
    ],
)
def test_claude_preflight_requires_explicit_true_auth_without_registration(
    tmp_path: Path, auth_payload: str, expected: dict[str, str] | None
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text(
        json.dumps({
            "hasCompletedOnboarding": True,
            "oauthAccount": {"accessToken": "must-not-leak"},
            "hooks": {"SessionStart": [{"command": "must-not-load"}]},
        }),
        encoding="utf-8",
    )
    user_settings = tmp_path / "settings.json"
    user_settings.write_text(
        json.dumps({
            "theme": "light",
            "hooks": {"SessionStart": [{"command": "must-not-load-settings"}]},
        }),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(argv)
        stdout = "2.1.216" if argv[-1] == "--version" else auth_payload
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    result = _claude_visibility_preflight(
        ("claude",),
        runner=runner,
        global_config_path=global_config,
        user_settings_path=user_settings,
    )

    assert result == expected
    assert "must-not-leak" not in repr(result)
    assert "must-not-load" not in repr(result)
    assert calls == [
        ["claude", "--version"],
        ["claude", "auth", "status", "--json"],
    ]
    assert all("--session-id" not in call and "--print" not in call for call in calls)


def test_claude_216_preflight_reads_onboarding_and_user_theme_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text(
        json.dumps({
            "hasCompletedOnboarding": True,
            "lastOnboardingVersion": "2.1.110",
            "theme": "future-global-theme-must-be-ignored",
            "oauthAccount": {"accessToken": "must-not-leak"},
        }),
        encoding="utf-8",
    )
    user_settings = tmp_path / ".claude" / "settings.json"
    user_settings.parent.mkdir()
    user_settings.write_text(
        json.dumps({
            "theme": "light",
            "hooks": {"SessionStart": [{"command": "must-not-load"}]},
            "enabledPlugins": {"private-plugin": True},
            "apiKeyHelper": "must-not-return",
        }),
        encoding="utf-8",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_POWERUP_ONBOARDING", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_TEAM_ONBOARDING", raising=False)
    monkeypatch.delenv("DISABLE_UPDATES", raising=False)

    result = _claude_visibility_preflight(
        ("claude",),
        runner=_claude_216_runner(calls),
        global_config_path=global_config,
        user_settings_path=user_settings,
    )

    assert result == {
        "version": "2.1.216",
        "authentication": "available",
        "theme": "light",
    }
    assert "must-not" not in repr(result)
    assert "private-plugin" not in repr(result)
    assert [call for call, _kwargs in calls] == [
        ["claude", "--version"],
        ["claude", "auth", "status", "--json"],
    ]
    assert all(kwargs["env"]["DISABLE_UPDATES"] == "1" for _call, kwargs in calls)
    assert "DISABLE_UPDATES" not in os.environ


@pytest.mark.parametrize(
    "environment_name",
    ["CLAUDE_CODE_POWERUP_ONBOARDING", "CLAUDE_CODE_TEAM_ONBOARDING"],
)
@pytest.mark.parametrize("forced_value", ["banner", "step"])
def test_claude_216_preflight_rejects_forced_onboarding_before_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    forced_value: str,
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setenv(environment_name, forced_value)
    other = (
        "CLAUDE_CODE_TEAM_ONBOARDING"
        if environment_name == "CLAUDE_CODE_POWERUP_ONBOARDING"
        else "CLAUDE_CODE_POWERUP_ONBOARDING"
    )
    monkeypatch.delenv(other, raising=False)

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=_claude_216_runner(calls),
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )
    assert calls == []


@pytest.mark.parametrize(
    "settings_payload",
    [
        "not-json",
        "[]",
        "{}",
        '{"theme":null}',
        '{"theme":true}',
        '{"theme":"auto"}',
        '{"theme":"Light"}',
        '{"theme":"future-theme"}',
        '{"theme":"dark","theme":"light"}',
    ],
)
def test_claude_216_preflight_user_settings_theme_fails_closed(
    tmp_path: Path, settings_payload: str
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text(settings_payload, encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=_claude_216_runner(calls),
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )


def test_claude_216_preflight_deep_user_settings_json_fails_closed(
    tmp_path: Path,
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    nested = "[" * 3_000 + "null" + "]" * 3_000
    user_settings = tmp_path / "settings.json"
    user_settings.write_text(
        '{"theme":"light","nested":' + nested + "}", encoding="utf-8"
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=_claude_216_runner(calls),
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )


@pytest.mark.parametrize("invalid_target", ["global", "settings"])
@pytest.mark.parametrize("invalid_kind", ["missing", "directory", "oversized"])
def test_claude_216_preflight_requires_bounded_regular_state_files(
    tmp_path: Path, invalid_target: str, invalid_kind: str
) -> None:
    global_config = tmp_path / ".claude.json"
    user_settings = tmp_path / "settings.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")
    invalid_path = global_config if invalid_target == "global" else user_settings
    invalid_path.unlink()
    if invalid_kind == "directory":
        invalid_path.mkdir()
    elif invalid_kind == "oversized":
        invalid_path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    calls: list[tuple[list[str], dict[str, Any]]] = []

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=_claude_216_runner(calls),
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )


@pytest.mark.parametrize("failed_call", ["version", "auth"])
def test_claude_preflight_command_failure_fails_before_spending_slot(
    tmp_path: Path,
    failed_call: str,
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(argv)
        is_version = argv[-1] == "--version"
        failed = failed_call == ("version" if is_version else "auth")
        stdout = "2.1.216" if is_version else '{"loggedIn": true}'
        return type(
            "Result",
            (),
            {"returncode": 1 if failed else 0, "stdout": stdout, "stderr": ""},
        )()

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=runner,
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )
    assert all("--session-id" not in call and "--print" not in call for call in calls)


@pytest.mark.parametrize(
    "global_payload",
    [
        "not-json",
        "[]",
        "{}",
        '{"hasCompletedOnboarding":false}',
        '{"hasCompletedOnboarding":"true"}',
        '{"hasCompletedOnboarding":true,"hasCompletedOnboarding":true}',
    ],
)
def test_claude_preflight_malformed_or_unknown_global_config_fails_closed(
    tmp_path: Path, global_payload: str
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text(global_payload, encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        stdout = "2.1.216" if argv[-1] == "--version" else '{"loggedIn":true}'
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=runner,
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )


def test_claude_preflight_missing_global_config_fails_closed(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(argv)
        stdout = "2.1.216" if argv[-1] == "--version" else '{"loggedIn":true}'
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=runner,
            global_config_path=tmp_path / ".claude.json",
            user_settings_path=user_settings,
        )
        is None
    )
    assert calls == []


def test_claude_preflight_surrogate_auth_output_fails_closed(tmp_path: Path) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        stdout = "2.1.216" if argv[-1] == "--version" else '{"loggedIn":true}\ud800'
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=runner,
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )


def test_claude_preflight_deeply_nested_auth_json_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")
    nested = "[" * 3_000 + "null" + "]" * 3_000
    auth_payload = f'{{"loggedIn":true,"nested":{nested}}}'
    assert len(auth_payload.encode("utf-8")) < 16_384
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        stdout = "2.1.216" if argv[-1] == "--version" else auth_payload
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=runner,
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )


def test_claude_preflight_deeply_nested_global_json_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config = tmp_path / ".claude.json"
    nested = "[" * 3_000 + "null" + "]" * 3_000
    global_payload = f'{{"hasCompletedOnboarding":true,"nested":{nested}}}'
    assert len(global_payload.encode("utf-8")) < 4 * 1024 * 1024
    global_config.write_text(global_payload, encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        stdout = "2.1.216" if argv[-1] == "--version" else '{"loggedIn":true}'
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=runner,
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )


def test_claude_preflight_does_not_treat_global_theme_as_user_setting(
    tmp_path: Path,
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text(
        '{"hasCompletedOnboarding":true,"theme":"light"}', encoding="utf-8"
    )
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"theme":null}', encoding="utf-8")

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        stdout = "2.1.216" if argv[-1] == "--version" else '{"loggedIn":true}'
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=runner,
            global_config_path=global_config,
            user_settings_path=settings,
        )
        is None
    )


@pytest.mark.parametrize(
    "version_output",
    [
        "2.1.215 (Claude Code)",
        "2.1.217 (Claude Code)",
        "2.1.216-beta (Claude Code)",
        "2.1.216 (Claude Code) extra",
        "Claude Code 2.1.216",
    ],
)
def test_claude_preflight_rejects_every_unpinned_version(
    tmp_path: Path, version_output: str
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        stdout = version_output if argv[-1] == "--version" else '{"loggedIn":true}'
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=runner,
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )


@pytest.mark.parametrize("team_onboarding", ["banner", "step"])
def test_claude_preflight_rejects_forced_team_onboarding_before_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    team_onboarding: str,
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setenv("CLAUDE_CODE_TEAM_ONBOARDING", team_onboarding)

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(argv)
        stdout = "2.1.216" if argv[-1] == "--version" else '{"loggedIn":true}'
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=runner,
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )
    assert calls == []


@pytest.mark.parametrize(
    "config_dir",
    [
        "",
        "relative/.claude",
        str(Path.home() / ".claude"),
        "配置/claudé",
        str(Path.home() / "custom-claude-root"),
    ],
    ids=["empty", "relative", "explicit-default", "unicode", "custom"],
)
def test_claude_preflight_rejects_any_claude_config_dir_before_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_dir: str,
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", config_dir)

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(argv)
        stdout = "2.1.216" if argv[-1] == "--version" else '{"loggedIn":true}'
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=runner,
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )
    assert calls == []


@pytest.mark.parametrize("modern_config", [True, False])
def test_claude_preflight_resolves_default_custom_oauth_global_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    modern_config: bool,
) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    selected = (
        claude_dir / ".config.json"
        if modern_config
        else tmp_path / ".claude-custom-oauth.json"
    )
    selected.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    if modern_config:
        (tmp_path / ".claude-custom-oauth.json").write_text(
            '{"hasCompletedOnboarding":false}', encoding="utf-8"
        )
    else:
        (tmp_path / ".claude.json").write_text(
            '{"hasCompletedOnboarding":false}', encoding="utf-8"
        )
    (claude_dir / "settings.json").write_text('{"theme":"dark-ansi"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", "https://oauth.example.test")
    monkeypatch.delenv("CLAUDE_CODE_POWERUP_ONBOARDING", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_TEAM_ONBOARDING", raising=False)

    def runner(argv: list[str], **kwargs: Any) -> Any:
        assert kwargs["env"]["DISABLE_UPDATES"] == "1"
        stdout = "2.1.216" if argv[-1] == "--version" else '{"loggedIn":true}'
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert _claude_visibility_preflight(("claude",), runner=runner) == {
        "version": "2.1.216",
        "authentication": "available",
        "theme": "dark-ansi",
    }


def test_characterize_claude_visibility_json_dispatches_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    class Backend:
        def characterize_claude_visibility(self) -> dict[str, Any]:
            calls.append("characterize")
            return {
                "passed": True,
                "restart_exact_id_verified": True,
                "verification": "pending_operator_checks",
                "cleanup": "pending_explicit_confirmation",
                "cleanup_token": {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "capability": "opaque-cleanup-capability",
                },
            }

        def close(self) -> None:
            calls.append("close")

    assert (
        main(
            ["characterize-claude-visibility", "--json"],
            config_loader=BridgeConfig,
            backend_factory=lambda _config: Backend(),  # type: ignore[arg-type]
        )
        == 0
    )
    assert calls == ["characterize", "close"]
    assert json.loads(capsys.readouterr().out) == {
        "cleanup": "pending_explicit_confirmation",
        "cleanup_token": {
            "id": "11111111-1111-4111-8111-111111111111",
            "capability": "opaque-cleanup-capability",
        },
        "passed": True,
        "restart_exact_id_verified": True,
        "verification": "pending_operator_checks",
    }


def test_characterize_cli_explicit_cleanup_dispatches_token_without_registration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = {
        "id": "11111111-1111-4111-8111-111111111111",
        "capability": "opaque-cleanup-capability",
    }
    calls: list[Mapping[str, Any] | None] = []

    class Backend:
        def characterize_claude_visibility(
            self, cleanup_token: Mapping[str, Any] | None = None
        ) -> dict[str, Any]:
            calls.append(cleanup_token)
            return {
                "verification": "operator_confirmed",
                "cleanup": "removed_exact_characterization",
            }

        def close(self) -> None:
            pass

    assert (
        main(
            [
                "characterize-claude-visibility",
                "--json",
                "--cleanup-token",
                json.dumps(token),
            ],
            config_loader=BridgeConfig,
            backend_factory=lambda _config: Backend(),  # type: ignore[arg-type]
        )
        == 0
    )
    assert calls == [token]
    assert (
        json.loads(capsys.readouterr().out)["cleanup"]
        == "removed_exact_characterization"
    )


@pytest.mark.parametrize("mismatch", ["uuid", "marker", "path", "cwd", "name"])
def test_characterization_cleanup_aborts_on_identity_mismatch(
    tmp_path: Path, mismatch: str
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    state: dict[str, Any] = {}

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        transcript = (
            projects_root / "exact-project" / f"{claim.reserved_claude_uuid}.jsonl"
        )
        transcript.parent.mkdir()
        transcript.write_text("native", encoding="utf-8")
        state.update(claim=claim, marker=marker, transcript=transcript)
        return claim

    def restarted_source() -> _RestartedSource:
        claim = state["claim"]
        native_id = (
            "00000000-0000-4000-8000-000000000000"
            if mismatch == "uuid"
            else claim.reserved_claude_uuid
        )
        name = "wrong" if mismatch == "name" else claim.native_name
        cwd = "C:/wrong" if mismatch == "cwd" else claim.source_cwd
        native_path = (
            str(projects_root / "other" / f"{claim.reserved_claude_uuid}.jsonl")
            if mismatch == "path"
            else str(state["transcript"])
        )
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=native_id,
            title=name,
            cwd=cwd,
            started_at=10.0,
            last_active=11.0,
            messages=_successful_characterization_messages(claim, state["marker"]),
            native_path=native_path,
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        marker = "wrong" if mismatch == "marker" else state["marker"]
        return _RestartedSource(state["transcript"], projection, marker)

    with pytest.raises(RuntimeError, match="characterization_identity_mismatch"):
        characterize_claude_visibility(
            source_root=source_root,
            projects_root=projects_root,
            reserve=reserve,
            registrar=_Registrar(),
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: 10.0,
        )
    assert state["transcript"].exists()


def test_explicit_cleanup_revalidates_identity_and_aborts_after_operator_phase(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    state: dict[str, Any] = {}

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
        transcript.parent.mkdir()
        transcript.write_text("native", encoding="utf-8")
        state.update(
            claim=claim,
            marker=marker,
            transcript=transcript,
            messages=_successful_characterization_messages(claim, marker),
        )
        return claim

    def restarted_source() -> _RestartedSource:
        claim = state["claim"]
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=11.0,
            messages=state["messages"],
            native_path=str(state["transcript"]),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(state["transcript"], projection, state["marker"])

    pending = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        registrar=_Registrar(),
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 10.0,
    )
    state["claim"] = state["claim"].__class__(**{
        **state["claim"].__dict__,
        "native_name": "changed-after-verification",
    })

    with pytest.raises(RuntimeError, match="characterization_identity_mismatch"):
        cleanup_characterized_claude_visibility(
            cleanup_token=pending["cleanup_token"],
            source_root=source_root,
            projects_root=projects_root,
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: 11.0,
        )
    assert state["transcript"].exists()


@pytest.mark.parametrize("failure_point", ["after_transcript", "after_commit"])
def test_characterization_rerun_reconciles_same_operation_without_second_launch(
    tmp_path: Path, failure_point: str
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    claims: list[ClaudeVisibilityClaim] = []
    launches: list[str] = []
    processes: list[str | None] = []
    state: dict[str, Any] = {}

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        if claims:
            claim = replace(
                claim,
                lease_kind="reconciliation",
                lease_digest="b" * 64,
                registration_reserved=False,
                launch_permitted=False,
                requires_exact_id_reconciliation=True,
                prior_error_code="lease_expired",
            )
        claims.append(claim)
        state.update(claim=claim, marker=marker)
        return claim

    class FailingAfterLaunchRegistrar:
        def process(self, claim: ClaudeVisibilityClaim) -> ClaudeRegistrarOutcome:
            processes.append(claim.lease_kind)
            if claim.lease_kind == "reconciliation":
                return ClaudeRegistrarOutcome(
                    "visible", claim.job_id, claim.reserved_claude_uuid
                )
            launches.append(claim.reserved_claude_uuid or "")
            transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
            transcript.parent.mkdir(exist_ok=True)
            transcript.write_text("native", encoding="utf-8")
            state["transcript"] = transcript
            raise RuntimeError(failure_point)

    def restarted_source() -> _RestartedSource:
        claim = state["claim"]
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=11.0,
            messages=_successful_characterization_messages(claim, state["marker"]),
            native_path=str(state["transcript"]),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(state["transcript"], projection, state["marker"])

    with pytest.raises(RuntimeError, match=failure_point):
        characterize_claude_visibility(
            source_root=source_root,
            projects_root=projects_root,
            reserve=reserve,
            registrar=FailingAfterLaunchRegistrar(),
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: 10.0,
        )

    recovered = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        registrar=FailingAfterLaunchRegistrar(),
        restarted_source=restarted_source,
        marker_secret=SECRET,
        reconcile_existing=reserve,
        registration_is_visible=lambda _operation: failure_point == "after_commit",
        now=lambda: 11.0,
    )

    assert len(launches) == 1
    assert processes == (
        ["launch"] if failure_point == "after_commit" else ["launch", "reconciliation"]
    )
    assert len(claims) == (1 if failure_point == "after_commit" else 2)
    assert recovered["reserved_claude_uuid"] == launches[0]


def test_characterization_commit_failure_reconciles_existing_transcript_into_store(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    database = SessionDB(tmp_path / "state.db")
    clock = [100.0]
    store = SessionBridgeStore(
        database,
        clock=lambda: clock[0],
        local_timezone=timezone.utc,
    )
    state: dict[str, Any] = {}
    claims: list[ClaudeVisibilityClaim] = []
    native_launches: list[str] = []

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        candidate = build_claude_visibility_candidate(
            projection, eligible_at=projection.last_active
        )
        identity = derive_claude_visibility_identity(candidate, SECRET)
        store.enqueue_claude_visibility_characterization(
            candidate,
            identity,
            SECRET,
            operation_id=projection.native_id,
            evidence_digest="a" * 64,
        )
        claim = store.claim_claude_visibility_job(
            clock[0],
            10.0,
            25,
            "1.00",
            "0.02",
            expected_job_id=identity.job_id,
        )
        claims.append(claim)
        state.update(claim=claim, marker=identity.signed_marker)
        return claim

    def reconcile(projection: SessionProjection) -> ClaudeVisibilityClaim:
        candidate = build_claude_visibility_candidate(
            projection, eligible_at=projection.last_active
        )
        identity = derive_claude_visibility_identity(candidate, SECRET)
        claim = store.claim_claude_visibility_reconciliation(
            clock[0], 10.0, expected_job_id=identity.job_id
        )
        claims.append(claim)
        state["claim"] = claim
        return claim

    class CommitFailingRegistrar:
        def process(self, claim: ClaudeVisibilityClaim) -> ClaudeRegistrarOutcome:
            if claim.lease_kind == "launch":
                native_launches.append(claim.reserved_claude_uuid or "")
                transcript = (
                    projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
                )
                transcript.parent.mkdir(exist_ok=True)
                transcript.write_text("native", encoding="utf-8")
                state["transcript"] = transcript
                # Mirrors ClaudeNativeRegistrar's commit-exception outcome: the
                # exact transcript exists while the launch lease remains durable.
                return ClaudeRegistrarOutcome(
                    "retry",
                    claim.job_id,
                    claim.reserved_claude_uuid,
                    "session_bridge_unavailable",
                )
            store.commit_claude_visibility_job(
                claim.job_id or "",
                claim.lease_digest or "",
                "b" * 64,
                clock[0],
            )
            return ClaudeRegistrarOutcome(
                "visible", claim.job_id, claim.reserved_claude_uuid
            )

    def restarted_source() -> _RestartedSource:
        claim = state["claim"]
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=100.0,
            last_active=101.0,
            messages=_successful_characterization_messages(
                claim, state["marker"], timestamp=100.0
            ),
            native_path=str(state["transcript"]),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(state["transcript"], projection, state["marker"])

    registrar = CommitFailingRegistrar()
    with pytest.raises(RuntimeError, match="characterization_registration_failed"):
        characterize_claude_visibility(
            source_root=source_root,
            projects_root=projects_root,
            reserve=reserve,
            registrar=registrar,
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: clock[0],
        )

    clock[0] = 111.0

    def registration_is_visible(operation: Mapping[str, Any]) -> bool:
        rows = store.claude_visibility_status(clock[0])["characterizations"]
        return rows == [{"job_id": operation["job_id"], "state": "claude_visible"}]

    recovered = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        reconcile_existing=reconcile,
        registration_is_visible=registration_is_visible,
        registrar=registrar,
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: clock[0],
    )

    assert recovered["reserved_claude_uuid"] == native_launches[0]
    assert native_launches == [claims[0].reserved_claude_uuid]
    assert [claim.lease_kind for claim in claims] == ["launch", "reconciliation"]
    assert store.claude_visibility_status(clock[0])["characterizations"] == [
        {"job_id": claims[0].job_id, "state": "claude_visible"}
    ]
    database.close()


def test_ready_visible_characterization_renews_without_claude_auth_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    state: dict[str, Any] = {}

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
        transcript.parent.mkdir()
        transcript.write_text("native", encoding="utf-8")
        state.update(claim=claim, marker=marker, transcript=transcript)
        return claim

    def restarted_source() -> _RestartedSource:
        claim = state["claim"]
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=11.0,
            messages=_successful_characterization_messages(claim, state["marker"]),
            native_path=str(state["transcript"]),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(state["transcript"], projection, state["marker"])

    original = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        registrar=_Registrar(),
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 10.0,
    )

    class Store:
        def enqueue_claude_visibility_characterization(self, *_args, **_kwargs):
            return {"status": "registered"}

        def claude_visibility_status(self, _now):
            return {
                "counts": {
                    "claude_pending": 0,
                    "claude_leased": 0,
                    "claude_retry": 0,
                    "claude_visible": 1,
                    "claude_failed": 0,
                },
                "retry_codes": {},
                "failed_codes": {},
                "fatal": [],
                "lineage": {
                    "unlinked_visible": 0,
                    "repairable": 0,
                    "blocked": 0,
                    "blocker_codes": {},
                },
                "characterizations": [
                    {"job_id": state["claim"].job_id, "state": "claude_visible"}
                ],
            }

        def claim_claude_visibility_job(self, *_args, **_kwargs):
            pytest.fail("visible ready renewal must not claim launch authority")

        def claim_claude_visibility_reconciliation(self, *_args, **_kwargs):
            pytest.fail("visible ready renewal must not claim reconciliation")

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("session_bridge.cli._CLAUDE_PROJECTS_ROOT", projects_root)
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: SECRET)
    monkeypatch.setattr(backend, "_require_store", lambda: Store())
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable",
        lambda _name: pytest.fail("ready visible renewal must not resolve Claude"),
    )
    monkeypatch.setattr(
        "session_bridge.cli._claude_visibility_preflight",
        lambda _command: pytest.fail("ready visible renewal must not require auth"),
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeSourceAdapter",
        lambda *_args, **_kwargs: restarted_source(),
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeNativeRegistrar",
        lambda *_args, **_kwargs: pytest.fail(
            "ready visible renewal must not construct a native registrar"
        ),
    )

    renewed = backend.characterize_claude_visibility()

    assert renewed["reserved_claude_uuid"] == original["reserved_claude_uuid"]
    assert renewed["cleanup_token"]["id"] == original["cleanup_token"]["id"]
    assert (
        renewed["cleanup_token"]["capability"]
        != original["cleanup_token"]["capability"]
    )


@pytest.mark.parametrize(
    ("recovery_phase", "job_state", "claim_status"),
    [
        ("launched", "claude_retry", "claimed"),
        ("ready", "claude_retry", "claimed"),
        ("ready", "claude_pending", "claimed"),
        ("ready", "claude_leased", "claimed"),
        ("ready", "claude_leased", "no_due_job"),
    ],
)
def test_ready_retry_characterization_uses_exact_backend_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_phase: str,
    job_state: str,
    claim_status: str,
) -> None:
    source_root = (
        tmp_path / "session-bridge" / "characterization" / "claude-visibility-sources"
    )
    projects_root = tmp_path / "projects"
    _pending, state, launch_registrar, restarted_source = _pending_characterization(
        tmp_path,
        source_root=source_root,
        projects_root=projects_root,
    )
    active_path = source_root / ".claude-visibility-operation.json"
    active = _read_characterization_record(active_path, SECRET)
    active["phase"] = recovery_phase
    _write_characterization_record(active_path, active, SECRET)
    reconciliation_claims: list[str] = []
    recovery_registrar = _Registrar()

    class Store:
        def enqueue_claude_visibility_characterization(self, *_args, **_kwargs):
            return {"status": "registered"}

        def claude_visibility_status(self, _now):
            return {
                "counts": {
                    "claude_pending": int(job_state == "claude_pending"),
                    "claude_leased": int(job_state == "claude_leased"),
                    "claude_retry": int(job_state == "claude_retry"),
                    "claude_visible": 0,
                    "claude_failed": 0,
                },
                "retry_codes": (
                    {"session_bridge_unavailable": 1}
                    if job_state == "claude_retry"
                    else {}
                ),
                "failed_codes": {},
                "fatal": [],
                "lineage": {
                    "unlinked_visible": 0,
                    "repairable": 0,
                    "blocked": 0,
                    "blocker_codes": {},
                },
                "characterizations": [
                    {"job_id": state["claim"].job_id, "state": job_state}
                ],
            }

        def claim_claude_visibility_job(self, *_args, **_kwargs):
            pytest.fail("ready retry recovery must not claim launch authority")

        def claim_claude_visibility_reconciliation(
            self, *_args, expected_job_id, **_kwargs
        ):
            reconciliation_claims.append(expected_job_id)
            return replace(
                state["claim"],
                status=claim_status,
                lease_kind="reconciliation",
                lease_digest="c" * 64,
                registration_reserved=False,
                launch_permitted=False,
                requires_exact_id_reconciliation=True,
                prior_error_code="session_bridge_unavailable",
            )

    backend = ProductionBackend(BridgeConfig())
    monkeypatch.setenv("HERMES_SESSION_BRIDGE_LIVE_TESTS", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("session_bridge.cli._CLAUDE_PROJECTS_ROOT", projects_root)
    monkeypatch.setattr("session_bridge.cli.resolve_marker_key", lambda: SECRET)
    monkeypatch.setattr(backend, "_require_store", lambda: Store())
    monkeypatch.setattr(
        "session_bridge.cli.resolve_cli_executable",
        lambda _name: pytest.fail(
            "ready retry exact reconciliation must not resolve Claude"
        ),
    )
    monkeypatch.setattr(
        "session_bridge.cli._claude_visibility_preflight",
        lambda _command: pytest.fail(
            "ready retry exact reconciliation must not require OAuth"
        ),
    )
    monkeypatch.setattr(
        "session_bridge.cli.ClaudeSourceAdapter",
        lambda *_args, **_kwargs: restarted_source(),
    )

    def recovery_registrar_factory(*_args, **kwargs):
        assert kwargs["startup_theme"] == "light"
        assert kwargs["claude_command"] == ()
        return recovery_registrar

    monkeypatch.setattr(
        "session_bridge.cli.ClaudeNativeRegistrar", recovery_registrar_factory
    )

    if claim_status == "no_due_job":
        with pytest.raises(RuntimeError, match="characterization_reservation_invalid"):
            backend.characterize_claude_visibility()
    else:
        recovered = backend.characterize_claude_visibility()
        assert recovered["reserved_claude_uuid"] == state["claim"].reserved_claude_uuid
    assert len(launch_registrar.claims) == 1
    assert [claim.lease_kind for claim in recovery_registrar.claims] == (
        ["reconciliation"] if claim_status == "claimed" else []
    )
    assert reconciliation_claims == [state["claim"].job_id]


def test_ready_characterization_refuses_success_when_store_is_not_visible(
    tmp_path: Path,
) -> None:
    _pending, state, registrar, restarted_source = _pending_characterization(tmp_path)
    recovery_registrar = _Registrar()

    with pytest.raises(RuntimeError, match="characterization_reservation_invalid"):
        characterize_claude_visibility(
            source_root=state["source_root"],
            projects_root=state["projects_root"],
            reserve=lambda _projection: pytest.fail(
                "ready recovery must never request launch authority"
            ),
            reconcile_existing=lambda _projection: ClaudeVisibilityClaim(
                status="no_due_job"
            ),
            registration_is_visible=lambda _operation: False,
            registrar=recovery_registrar,
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: 11.0,
        )

    assert len(registrar.claims) == 1
    assert recovery_registrar.claims == []


def test_ready_characterization_reconciles_exact_retry_before_success(
    tmp_path: Path,
) -> None:
    _pending, state, registrar, restarted_source = _pending_characterization(tmp_path)
    recovery_registrar = _Registrar()
    original_claim = state["claim"]

    def reconcile(_projection: SessionProjection) -> ClaudeVisibilityClaim:
        return replace(
            original_claim,
            lease_kind="reconciliation",
            lease_digest="c" * 64,
            registration_reserved=False,
            launch_permitted=False,
            requires_exact_id_reconciliation=True,
            prior_error_code="session_bridge_unavailable",
        )

    recovered = characterize_claude_visibility(
        source_root=state["source_root"],
        projects_root=state["projects_root"],
        reserve=lambda _projection: pytest.fail(
            "ready recovery must never request launch authority"
        ),
        reconcile_existing=reconcile,
        registration_is_visible=lambda _operation: False,
        registrar=recovery_registrar,
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 11.0,
    )

    assert recovered["reserved_claude_uuid"] == original_claim.reserved_claude_uuid
    assert len(registrar.claims) == 1
    assert [claim.lease_kind for claim in recovery_registrar.claims] == [
        "reconciliation"
    ]


def test_characterization_rerun_reconciles_absence_then_relaunches_reserved_uuid(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    state: dict[str, Any] = {}
    claims: list[ClaudeVisibilityClaim] = []

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        if not claims:
            claim, marker = _claim_for(projection)
            state.update(claim=claim, marker=marker)
        elif len(claims) == 1:
            claim = replace(
                state["claim"],
                lease_kind="reconciliation",
                lease_digest="b" * 64,
                registration_reserved=False,
                launch_permitted=False,
                requires_exact_id_reconciliation=True,
                prior_error_code="creation_ambiguous",
            )
        else:
            claim = replace(
                state["claim"],
                lease_digest="c" * 64,
                attempt_ordinal=2,
                prior_error_code="creation_ambiguous",
            )
        claims.append(claim)
        return claim

    class AmbiguousThenRecoveredRegistrar:
        def process(self, claim: ClaudeVisibilityClaim) -> ClaudeRegistrarOutcome:
            if len(claims) == 1:
                return ClaudeRegistrarOutcome(
                    "retry",
                    claim.job_id,
                    claim.reserved_claude_uuid,
                    "creation_ambiguous",
                )
            if claim.lease_kind == "reconciliation":
                return ClaudeRegistrarOutcome(
                    "absent", claim.job_id, claim.reserved_claude_uuid
                )
            transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
            transcript.parent.mkdir(exist_ok=True)
            transcript.write_text("native", encoding="utf-8")
            state["transcript"] = transcript
            return ClaudeRegistrarOutcome(
                "visible", claim.job_id, claim.reserved_claude_uuid
            )

    def restarted_source() -> _RestartedSource:
        claim = state["claim"]
        transcript = state.get(
            "transcript",
            projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl",
        )
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=11.0,
            messages=_successful_characterization_messages(claim, state["marker"]),
            native_path=str(transcript),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(transcript, projection, state["marker"])

    registrar = AmbiguousThenRecoveredRegistrar()
    with pytest.raises(RuntimeError, match="characterization_registration_failed"):
        characterize_claude_visibility(
            source_root=source_root,
            projects_root=projects_root,
            reserve=reserve,
            registrar=registrar,
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: 10.0,
        )

    recovered = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        registrar=registrar,
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 11.0,
    )

    assert [claim.lease_kind for claim in claims] == [
        "launch",
        "reconciliation",
        "launch",
    ]
    assert {claim.reserved_claude_uuid for claim in claims} == {
        recovered["reserved_claude_uuid"]
    }
    assert claims[2].attempt_ordinal == 2


def test_characterization_salvages_bounded_auth_failure_by_resuming_same_uuid(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    state: dict[str, Any] = {"messages": []}

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
        transcript.parent.mkdir()
        transcript.write_text("native", encoding="utf-8")
        state.update(claim=claim, marker=marker, transcript=transcript)
        state["messages"] = [
            _successful_characterization_messages(claim, marker)[0],
            ProjectedMessage(
                "a",
                0,
                "assistant",
                "Failed to authenticate. API Error: 401 Invalid authentication credentials",
                11.0,
            ),
        ]
        return claim

    class FailedRegistrar:
        def process(self, claim: ClaudeVisibilityClaim) -> ClaudeRegistrarOutcome:
            return ClaudeRegistrarOutcome(
                "failed", claim.job_id, claim.reserved_claude_uuid, "bridge_conflict"
            )

    def restarted_source() -> _RestartedSource:
        claim = state["claim"]
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=13.0,
            messages=list(state["messages"]),
            native_path=str(state["transcript"]),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(state["transcript"], projection, state["marker"])

    with pytest.raises(RuntimeError, match="characterization_registration_failed"):
        characterize_claude_visibility(
            source_root=source_root,
            projects_root=projects_root,
            reserve=reserve,
            registrar=FailedRegistrar(),
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: 10.0,
        )

    completed: list[tuple[Mapping[str, Any], str]] = []

    def recover(
        operation: Mapping[str, Any], evidence_digest: str, prompt: str
    ) -> Mapping[str, Any]:
        assert operation["reserved_claude_uuid"] == state["claim"].reserved_claude_uuid
        assert len(evidence_digest) == 64
        state["messages"].extend([
            ProjectedMessage("ru", 0, "user", prompt, 12.0),
            ProjectedMessage("ra", 0, "assistant", "REGISTERED", 13.0),
        ])
        return {
            "status": "recovered",
            "job_id": operation["job_id"],
            "reserved_claude_uuid": operation["reserved_claude_uuid"],
            "lease_digest": "c" * 64,
        }

    def crash_after_resume(recovery: Mapping[str, Any], digest: str) -> None:
        completed.append((recovery, digest))
        raise RuntimeError("simulated_post_resume_crash")

    with pytest.raises(RuntimeError, match="simulated_post_resume_crash"):
        characterize_claude_visibility(
            source_root=source_root,
            projects_root=projects_root,
            reserve=reserve,
            registrar=FailedRegistrar(),
            restarted_source=restarted_source,
            marker_secret=SECRET,
            recover_auth_failure=recover,
            complete_auth_recovery=crash_after_resume,
            now=lambda: 14.0,
        )

    reconciled: list[tuple[str, str, str]] = []
    result = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        registrar=FailedRegistrar(),
        restarted_source=restarted_source,
        marker_secret=SECRET,
        reconcile_auth_recovery=lambda operation, evidence, prompt, transcript: (
            reconciled.append((evidence, prompt, transcript))
        ),
        now=lambda: 14.0,
    )

    assert result["reserved_claude_uuid"] == state["claim"].reserved_claude_uuid
    assert len(completed) == 1 and len(completed[0][1]) == 64
    assert len(reconciled) == 1
    assert all(len(digest) == 64 for digest in reconciled[0])


def test_characterization_recovers_same_cleanup_capability_after_ready_write_error(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    state: dict[str, Any] = {}
    launches = 0
    failed_ready_write = False

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        state.update(claim=claim, marker=marker)
        return claim

    class Registrar:
        def process(self, claim: ClaudeVisibilityClaim) -> ClaudeRegistrarOutcome:
            nonlocal launches
            launches += 1
            transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
            transcript.parent.mkdir(exist_ok=True)
            transcript.write_text("native", encoding="utf-8")
            state["transcript"] = transcript
            return ClaudeRegistrarOutcome(
                "visible", claim.job_id, claim.reserved_claude_uuid
            )

    def restarted_source() -> _RestartedSource:
        claim = state["claim"]
        projection = SessionProjection(
            provider=Provider.CLAUDE,
            native_id=claim.reserved_claude_uuid,
            title=claim.native_name,
            cwd=claim.source_cwd,
            started_at=10.0,
            last_active=11.0,
            messages=_successful_characterization_messages(claim, state["marker"]),
            native_path=str(state["transcript"]),
            native_hash="b" * 64,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        )
        return _RestartedSource(state["transcript"], projection, state["marker"])

    def writer(path: Path, payload: dict[str, Any], secret: bytes) -> None:
        nonlocal failed_ready_write
        _write_characterization_record(path, payload, secret)
        if payload.get("phase") == "ready" and not failed_ready_write:
            failed_ready_write = True
            raise OSError("synthetic post-token-write failure")

    with pytest.raises(OSError, match="post-token-write"):
        characterize_claude_visibility(
            source_root=source_root,
            projects_root=projects_root,
            reserve=reserve,
            registrar=Registrar(),
            restarted_source=restarted_source,
            marker_secret=SECRET,
            record_writer=writer,
            now=lambda: 10.0,
        )
    recovered = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        registration_is_visible=lambda _operation: True,
        registrar=Registrar(),
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 11.0,
    )
    assert launches == 1
    assert recovered["cleanup_token"]["id"]
    assert recovered["cleanup_token"]["capability"]


def test_expired_ready_operation_revalidates_identity_and_renews_same_operation(
    tmp_path: Path,
) -> None:
    pending, state, registrar, restarted_source = _pending_characterization(tmp_path)
    first_record = _read_characterization_record(
        state["source_root"] / ".claude-visibility-operation.json", SECRET
    )

    renewed = characterize_claude_visibility(
        source_root=state["source_root"],
        projects_root=state["projects_root"],
        reserve=lambda _projection: (_ for _ in ()).throw(
            AssertionError("renewal must not reserve a second operation")
        ),
        registrar=_Registrar(),
        restarted_source=restarted_source,
        marker_secret=SECRET,
        registration_is_visible=lambda _operation: True,
        now=lambda: float(first_record["expires_at"]) + 1.0,
    )

    renewed_record = _read_characterization_record(
        state["source_root"] / ".claude-visibility-operation.json", SECRET
    )
    assert renewed["cleanup_token"]["id"] == pending["cleanup_token"]["id"]
    assert renewed["reserved_claude_uuid"] == pending["reserved_claude_uuid"]
    assert (
        renewed["cleanup_token"]["capability"] != pending["cleanup_token"]["capability"]
    )
    assert renewed["cleanup_expires_at"] == renewed_record["expires_at"]
    assert renewed_record["expires_at"] > first_record["expires_at"]
    assert len(registrar.claims) == 1


def test_expired_ready_operation_renews_after_native_resume_appends_transcript(
    tmp_path: Path,
) -> None:
    pending, state, registrar, restarted_source = _pending_characterization(tmp_path)
    active = state["source_root"] / ".claude-visibility-operation.json"
    first_record = _read_characterization_record(active, SECRET)
    transcript = state["transcript"]
    before = transcript.stat()
    resume_record = {
        "type": "user",
        "sessionId": pending["reserved_claude_uuid"],
        "uuid": "12121212-1212-4212-8212-121212121212",
        "timestamp": "2026-07-17T12:00:00Z",
        "cwd": state["claim"].source_cwd,
        "gitBranch": "",
        "isSidechain": False,
        "message": {"role": "user", "content": "resume verification"},
    }
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("\n" + json.dumps(resume_record, separators=(",", ":")))
    after = transcript.stat()
    assert after.st_size > before.st_size
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)

    renewal_registrar = _Registrar()
    renewed = characterize_claude_visibility(
        source_root=state["source_root"],
        projects_root=state["projects_root"],
        reserve=lambda _projection: (_ for _ in ()).throw(
            AssertionError("renewal must not reserve a second operation")
        ),
        registrar=renewal_registrar,
        restarted_source=restarted_source,
        marker_secret=SECRET,
        registration_is_visible=lambda _operation: True,
        now=lambda: float(first_record["expires_at"]) + 1.0,
    )

    assert renewed["cleanup_token"]["id"] == pending["cleanup_token"]["id"]
    assert renewed["reserved_claude_uuid"] == pending["reserved_claude_uuid"]
    assert (
        renewed["cleanup_token"]["capability"] != pending["cleanup_token"]["capability"]
    )
    assert len(registrar.claims) == 1
    assert renewal_registrar.claims == []


def test_expired_ready_operation_rejects_replaced_transcript_with_same_content(
    tmp_path: Path,
) -> None:
    _pending, state, registrar, restarted_source = _pending_characterization(tmp_path)
    active = state["source_root"] / ".claude-visibility-operation.json"
    first_record = _read_characterization_record(active, SECRET)
    transcript = state["transcript"]
    original = transcript.read_bytes()
    replacement = transcript.with_name(f".{transcript.name}.replacement")
    replacement.write_bytes(original)
    os.replace(replacement, transcript)

    renewal_registrar = _Registrar()
    with pytest.raises(RuntimeError, match="identity_mismatch:path_changed"):
        characterize_claude_visibility(
            source_root=state["source_root"],
            projects_root=state["projects_root"],
            reserve=lambda _projection: (_ for _ in ()).throw(
                AssertionError("renewal must not reserve a second operation")
            ),
            registration_is_visible=lambda _operation: True,
            registrar=renewal_registrar,
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: float(first_record["expires_at"]) + 1.0,
        )

    assert len(registrar.claims) == 1
    assert renewal_registrar.claims == []


def test_direct_cleanup_rejects_replaced_transcript_with_same_content(
    tmp_path: Path,
) -> None:
    pending, state, _registrar, restarted_source = _pending_characterization(tmp_path)
    active = state["source_root"] / ".claude-visibility-operation.json"
    original_record = _read_characterization_record(active, SECRET)
    original_identity = original_record["transcript_identity"]
    transcript = state["transcript"]
    original = transcript.read_bytes()
    replacement = transcript.with_name(f".{transcript.name}.replacement")
    replacement.write_bytes(original)
    os.replace(replacement, transcript)
    replacement_identity = transcript.stat()
    assert (replacement_identity.st_dev, replacement_identity.st_ino) != tuple(
        original_identity[:2]
    )

    with pytest.raises(RuntimeError, match="identity_mismatch:path_changed"):
        cleanup_characterized_claude_visibility(
            cleanup_token=pending["cleanup_token"],
            source_root=state["source_root"],
            projects_root=state["projects_root"],
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: 11.0,
        )

    claimed = (
        state["source_root"]
        / ".cleanup-claims"
        / f"{pending['cleanup_token']['id']}.json"
    )
    claimed_record = _read_characterization_record(claimed, SECRET)
    assert claimed_record["transcript_identity"] == original_identity
    assert transcript.exists()
    assert transcript.read_bytes() == original
    assert Path(state["claim"].source_cwd).exists()


def test_direct_cleanup_allows_legitimate_in_place_transcript_append(
    tmp_path: Path,
) -> None:
    pending, state, _registrar, restarted_source = _pending_characterization(tmp_path)
    transcript = state["transcript"]
    before = transcript.stat()
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("\nlegitimate native resume append")
    after = transcript.stat()
    assert after.st_size > before.st_size
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)

    cleaned = cleanup_characterized_claude_visibility(
        cleanup_token=pending["cleanup_token"],
        source_root=state["source_root"],
        projects_root=state["projects_root"],
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 11.0,
    )

    assert cleaned["cleanup"] == "removed_exact_characterization"
    assert not transcript.exists()
    assert not Path(state["claim"].source_cwd).exists()


def test_claimed_cleanup_remains_authorized_across_expiry_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending, state, registrar, restarted_source = _pending_characterization(tmp_path)
    active = state["source_root"] / ".claude-visibility-operation.json"
    expiry = float(_read_characterization_record(active, SECRET)["expires_at"])
    interrupted = False

    def interrupt_after_authorized_checkpoint(
        path: Path, payload: dict[str, Any], secret: bytes
    ) -> None:
        nonlocal interrupted
        _write_characterization_record(path, payload, secret)
        if payload.get("phase") == "transcript_removing" and not interrupted:
            interrupted = True
            raise OSError("synthetic claimed cleanup interruption")

    monkeypatch.setattr(
        "session_bridge.characterize._write_characterization_record",
        interrupt_after_authorized_checkpoint,
    )
    with pytest.raises(OSError, match="claimed cleanup interruption"):
        cleanup_characterized_claude_visibility(
            cleanup_token=pending["cleanup_token"],
            source_root=state["source_root"],
            projects_root=state["projects_root"],
            restarted_source=restarted_source,
            marker_secret=SECRET,
            now=lambda: expiry - 1.0,
        )
    monkeypatch.setattr(
        "session_bridge.characterize._write_characterization_record",
        _write_characterization_record,
    )

    cleaned = cleanup_characterized_claude_visibility(
        cleanup_token=pending["cleanup_token"],
        source_root=state["source_root"],
        projects_root=state["projects_root"],
        restarted_source=lambda: (_ for _ in ()).throw(
            AssertionError("claimed cleanup must resume from its durable checkpoint")
        ),
        marker_secret=SECRET,
        now=lambda: expiry + 1_000.0,
    )

    assert cleaned["reserved_claude_uuid"] == pending["reserved_claude_uuid"]
    assert len(registrar.claims) == 1


def test_concurrent_cleanup_callers_serialize_without_replaying_checkpoints(
    tmp_path: Path,
) -> None:
    pending, state, _registrar, restarted_source = _pending_characterization(tmp_path)
    first_inside_restart = threading.Event()
    release_restart = threading.Event()
    restart_calls = 0
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def blocking_restarted_source() -> _RestartedSource:
        nonlocal restart_calls
        restart_calls += 1
        first_inside_restart.set()
        assert release_restart.wait(timeout=5)
        return restarted_source()

    def cleanup() -> None:
        try:
            results.append(
                cleanup_characterized_claude_visibility(
                    cleanup_token=pending["cleanup_token"],
                    source_root=state["source_root"],
                    projects_root=state["projects_root"],
                    restarted_source=blocking_restarted_source,
                    marker_secret=SECRET,
                    now=lambda: 11.0,
                )
            )
        except BaseException as exc:  # asserted below with both threads joined
            errors.append(exc)

    first = threading.Thread(target=cleanup)
    second = threading.Thread(target=cleanup)
    first.start()
    assert first_inside_restart.wait(timeout=5)
    second.start()
    time.sleep(0.1)
    release_restart.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert restart_calls == 1
    assert {result["reserved_claude_uuid"] for result in results} == {
        pending["reserved_claude_uuid"]
    }


def test_prepare_safe_root_rejects_reparse_point_in_existing_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestor = tmp_path / "ancestor"
    root = ancestor / "safe" / "root"
    root.mkdir(parents=True)
    real_lstat = os.lstat

    class _ReparseMetadata:
        def __init__(self, metadata: os.stat_result) -> None:
            self._metadata = metadata
            self.st_file_attributes = 0x400

        def __getattr__(self, name: str) -> Any:
            return getattr(self._metadata, name)

    def injected_lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        metadata = real_lstat(path, *args, **kwargs)
        if Path(path) == ancestor:
            return _ReparseMetadata(metadata)
        return metadata

    monkeypatch.setattr(os, "lstat", injected_lstat)
    with pytest.raises(RuntimeError, match="unsafe_characterization_root"):
        _prepare_safe_root(root, create=False)


def test_cleanup_rejects_forged_capability_and_leaves_exact_artifacts(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    state: dict[str, Any] = {}

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        transcript = projects_root / "exact" / f"{claim.reserved_claude_uuid}.jsonl"
        transcript.parent.mkdir()
        transcript.write_text("native", encoding="utf-8")
        state.update(claim=claim, marker=marker, transcript=transcript)
        return claim

    pending = characterize_claude_visibility(
        source_root=source_root,
        projects_root=projects_root,
        reserve=reserve,
        registrar=_Registrar(),
        restarted_source=lambda: _RestartedSource(
            state["transcript"],
            SessionProjection(
                provider=Provider.CLAUDE,
                native_id=state["claim"].reserved_claude_uuid,
                title=state["claim"].native_name,
                cwd=state["claim"].source_cwd,
                started_at=10.0,
                last_active=11.0,
                messages=_successful_characterization_messages(
                    state["claim"], state["marker"]
                ),
                native_path=str(state["transcript"]),
                native_hash="b" * 64,
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            ),
            state["marker"],
        ),
        marker_secret=SECRET,
        now=lambda: 10.0,
    )
    forged = {**pending["cleanup_token"], "capability": "forged"}
    with pytest.raises(RuntimeError, match="cleanup_token_invalid"):
        cleanup_characterized_claude_visibility(
            cleanup_token=forged,
            source_root=source_root,
            projects_root=projects_root,
            restarted_source=lambda: _RestartedSource(
                state["transcript"],
                SessionProjection(
                    provider=Provider.CLAUDE,
                    native_id=state["claim"].reserved_claude_uuid,
                    title=state["claim"].native_name,
                    cwd=state["claim"].source_cwd,
                    started_at=10.0,
                    last_active=11.0,
                    messages=_successful_characterization_messages(
                        state["claim"], state["marker"]
                    ),
                    native_path=str(state["transcript"]),
                    native_hash="b" * 64,
                    origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                ),
                state["marker"],
            ),
            marker_secret=SECRET,
            now=lambda: 11.0,
        )
    assert state["transcript"].exists()
    assert Path(state["claim"].source_cwd).exists()

    # Even a correctly re-signed durable record cannot turn a malformed bridge
    # marker or a non-child directory into cleanup authority.
    active = source_root / ".claude-visibility-operation.json"
    durable = _read_characterization_record(active, SECRET)
    original_marker = durable["signed_marker"]
    durable["signed_marker"] = original_marker[:-1] + (
        "A" if original_marker[-1] != "A" else "B"
    )
    _write_characterization_record(active, durable, SECRET)
    with pytest.raises(RuntimeError, match="identity_mismatch:marker"):
        cleanup_characterized_claude_visibility(
            cleanup_token=pending["cleanup_token"],
            source_root=source_root,
            projects_root=projects_root,
            restarted_source=lambda: (_ for _ in ()).throw(
                AssertionError("malformed marker must fail before adapter trust")
            ),
            marker_secret=SECRET,
            now=lambda: 11.0,
        )

    claimed = source_root / ".cleanup-claims" / f"{pending['cleanup_token']['id']}.json"
    durable = _read_characterization_record(claimed, SECRET)
    durable["signed_marker"] = original_marker
    durable["source_cwd"] = str(projects_root)
    _write_characterization_record(claimed, durable, SECRET)
    with pytest.raises(RuntimeError, match="identity_mismatch:disposable"):
        cleanup_characterized_claude_visibility(
            cleanup_token=pending["cleanup_token"],
            source_root=source_root,
            projects_root=projects_root,
            restarted_source=lambda: (_ for _ in ()).throw(AssertionError),
            marker_secret=SECRET,
            now=lambda: 11.0,
        )


# ---------------------------------------------------------------------------
# Per-gate preflight failure codes.
#
# _claude_visibility_preflight has many independent reasons to refuse and used
# to collapse all of them into a bare ``None``, so the only thing that reached
# the log was one undifferentiated ProviderDegraded(
# "claude_visibility_preflight_failed"). Telling "CLI missing" from "wrong
# version" from "not logged in" required a bespoke probe. These tests pin a
# fixed, declared code per gate.
# ---------------------------------------------------------------------------


def _preflight_state(tmp_path: Path, *, theme: str | None = "light") -> tuple[Path, Path]:
    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":true}', encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text(
        "{}" if theme is None else json.dumps({"theme": theme}), encoding="utf-8"
    )
    return global_config, user_settings


def _preflight_runner(
    *,
    version_output: str = "2.1.216",
    version_returncode: int = 0,
    auth_output: str = '{"loggedIn":true}',
    auth_returncode: int = 0,
):
    def runner(argv: list[str], **_kwargs: Any) -> Any:
        if argv[-1] == "--version":
            stdout, returncode = version_output, version_returncode
        else:
            stdout, returncode = auth_output, auth_returncode
        return type(
            "Result", (), {"returncode": returncode, "stdout": stdout, "stderr": ""}
        )()

    return runner


def _detail(tmp_path: Path, *, theme: str | None = "light", **runner_kwargs: Any):
    from session_bridge.cli import _claude_visibility_preflight_detail

    global_config, user_settings = _preflight_state(tmp_path, theme=theme)
    return _claude_visibility_preflight_detail(
        ("claude",),
        runner=_preflight_runner(**runner_kwargs),
        global_config_path=global_config,
        user_settings_path=user_settings,
    )


def test_preflight_detail_reports_no_failure_code_on_success(tmp_path: Path) -> None:
    detail = _detail(tmp_path)

    assert detail.failure_code is None
    assert detail.startup == {
        "version": "2.1.216",
        "authentication": "available",
        "theme": "light",
    }


def test_preflight_detail_names_auth_unavailable_when_command_exits_nonzero(
    tmp_path: Path,
) -> None:
    """The live 2026-08-19 failure: version fine, `claude auth status` exits 1."""
    detail = _detail(
        tmp_path,
        auth_returncode=1,
        auth_output='{"loggedIn":false,"authMethod":"none"}',
    )

    assert detail.startup is None
    assert detail.failure_code == "claude_visibility_preflight_failed_auth_unavailable"


def test_preflight_detail_names_not_logged_in_when_auth_succeeds_but_denies(
    tmp_path: Path,
) -> None:
    detail = _detail(tmp_path, auth_output='{"loggedIn":false}')

    assert detail.startup is None
    assert detail.failure_code == "claude_visibility_preflight_failed_not_logged_in"


def test_preflight_detail_names_version_unpinned(tmp_path: Path) -> None:
    detail = _detail(tmp_path, version_output="2.1.999")

    assert detail.startup is None
    assert detail.failure_code == "claude_visibility_preflight_failed_version_unpinned"


def test_preflight_detail_names_theme_unavailable(tmp_path: Path) -> None:
    detail = _detail(tmp_path, theme=None)

    assert detail.startup is None
    assert detail.failure_code == "claude_visibility_preflight_failed_theme_unavailable"


def test_preflight_detail_names_onboarding_incomplete(tmp_path: Path) -> None:
    from session_bridge.cli import _claude_visibility_preflight_detail

    global_config = tmp_path / ".claude.json"
    global_config.write_text('{"hasCompletedOnboarding":false}', encoding="utf-8")
    user_settings = tmp_path / "settings.json"
    user_settings.write_text('{"theme":"light"}', encoding="utf-8")

    detail = _claude_visibility_preflight_detail(
        ("claude",),
        runner=_preflight_runner(),
        global_config_path=global_config,
        user_settings_path=user_settings,
    )

    assert detail.startup is None
    assert (
        detail.failure_code == "claude_visibility_preflight_failed_onboarding_incomplete"
    )


def test_preflight_detail_names_config_dir_override_before_running_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge.cli import _claude_visibility_preflight_detail

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    def runner(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no command may run once the config dir is overridden")

    detail = _claude_visibility_preflight_detail(("claude",), runner=runner)

    assert detail.startup is None
    assert detail.failure_code == "claude_visibility_preflight_failed_config_dir_override"


@pytest.mark.parametrize(
    "variable", ["CLAUDE_CODE_POWERUP_ONBOARDING", "CLAUDE_CODE_TEAM_ONBOARDING"]
)
def test_preflight_detail_names_forced_onboarding_before_running_commands(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_bridge.cli import _claude_visibility_preflight_detail

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv(variable, "banner")

    def runner(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no command may run under forced onboarding")

    detail = _claude_visibility_preflight_detail(("claude",), runner=runner)

    assert detail.startup is None
    assert detail.failure_code == "claude_visibility_preflight_failed_forced_onboarding"


def test_preflight_detail_names_command_error(tmp_path: Path) -> None:
    from session_bridge.cli import _claude_visibility_preflight_detail

    global_config, user_settings = _preflight_state(tmp_path)

    def runner(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("claude is not installed")

    detail = _claude_visibility_preflight_detail(
        ("claude",),
        runner=runner,
        global_config_path=global_config,
        user_settings_path=user_settings,
    )

    assert detail.startup is None
    assert detail.failure_code == "claude_visibility_preflight_failed_command_error"


@pytest.mark.parametrize(
    ("timed_out_argv", "expected_stage"),
    [
        (("claude", "--version"), "version"),
        (("claude", "auth", "status", "--json"), "auth_status"),
    ],
)
def test_preflight_detail_logs_the_timed_out_command_without_output(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    timed_out_argv: tuple[str, ...],
    expected_stage: str,
) -> None:
    from session_bridge.cli import _claude_visibility_preflight_detail

    global_config, user_settings = _preflight_state(tmp_path)
    secret = "auth-output-must-not-reach-the-log"

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        if tuple(argv) == timed_out_argv:
            raise subprocess.TimeoutExpired(argv, 15.0, output=secret)
        return _preflight_runner()(argv)

    detail = _claude_visibility_preflight_detail(
        ("claude",),
        runner=runner,
        global_config_path=global_config,
        user_settings_path=user_settings,
    )

    assert detail.startup is None
    assert detail.failure_code == "claude_visibility_preflight_failed_command_error"
    assert f"stage={expected_stage}" in caplog.text
    assert "kind=timeout" in caplog.text
    assert secret not in caplog.text


def test_preflight_detail_names_auth_output_too_large(tmp_path: Path) -> None:
    padded = json.dumps({"loggedIn": True, "pad": "x" * 20_000})
    detail = _detail(tmp_path, auth_output=padded)

    assert detail.startup is None
    assert detail.failure_code == "claude_visibility_preflight_failed_auth_output_too_large"


def test_preflight_detail_names_auth_output_invalid(tmp_path: Path) -> None:
    detail = _detail(tmp_path, auth_output="not json at all")

    assert detail.startup is None
    assert detail.failure_code == "claude_visibility_preflight_failed_auth_output_invalid"


def test_every_preflight_failure_code_is_declared(tmp_path: Path) -> None:
    """No gate may invent a code outside the declared contract."""
    from session_bridge.claude_visibility_codes import (
        CLAUDE_VISIBILITY_PREFLIGHT_FAILURE_CODES,
    )

    observed = {
        _detail(tmp_path, auth_returncode=1).failure_code,
        _detail(tmp_path, auth_output='{"loggedIn":false}').failure_code,
        _detail(tmp_path, version_output="2.1.999").failure_code,
        _detail(tmp_path, theme=None).failure_code,
        _detail(tmp_path, auth_output="not json").failure_code,
    }

    assert None not in observed
    assert observed <= CLAUDE_VISIBILITY_PREFLIGHT_FAILURE_CODES
    assert all(
        code.startswith("claude_visibility_preflight_failed")
        for code in CLAUDE_VISIBILITY_PREFLIGHT_FAILURE_CODES
    )


def test_preflight_wrapper_still_returns_none_on_failure(tmp_path: Path) -> None:
    """The existing callers/tests keep the plain dict|None contract."""
    global_config, user_settings = _preflight_state(tmp_path)

    assert (
        _claude_visibility_preflight(
            ("claude",),
            runner=_preflight_runner(auth_returncode=1),
            global_config_path=global_config,
            user_settings_path=user_settings,
        )
        is None
    )
