from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from session_bridge.characterize import (
    _read_characterization_record,
    _write_characterization_record,
    characterize_claude_visibility,
    cleanup_characterized_claude_visibility,
)
from session_bridge.cli import _claude_visibility_preflight
from session_bridge.cli import main
from session_bridge.config import BridgeConfig
from session_bridge.claude_registrar import ClaudeRegistrarOutcome
from session_bridge.claude_visibility import (
    ClaudeVisibilityClaim,
    derive_claude_visibility_identity,
)
from session_bridge.models import (
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
)


SECRET = b"characterization-marker-secret"


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
            messages=[ProjectedMessage("m", 0, "user", state["marker"], 10.0)],
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
        if payload.get("phase") == "disposable_removed" and not failed_checkpoint:
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


@pytest.mark.parametrize(
    ("auth_payload", "expected"),
    [
        ('{"loggedIn": true}', {"version": "2.1.110", "authentication": "available"}),
        (
            '{"authenticated": true}',
            {"version": "2.1.110", "authentication": "available"},
        ),
        ('{"loggedIn": false}', None),
        ('{"authenticated": false}', None),
        ('{"loggedIn": "true"}', None),
        ("not-json", None),
    ],
)
def test_claude_preflight_requires_explicit_true_auth_without_registration(
    auth_payload: str, expected: dict[str, str] | None
) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(argv)
        stdout = "2.1.110" if argv[-1] == "--version" else auth_payload
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    assert _claude_visibility_preflight(("claude",), runner=runner) == expected
    assert calls == [
        ["claude", "--version"],
        ["claude", "auth", "status", "--json"],
    ]
    assert all("--session-id" not in call and "--print" not in call for call in calls)


@pytest.mark.parametrize("failed_call", ["version", "auth"])
def test_claude_preflight_command_failure_fails_before_spending_slot(
    failed_call: str,
) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(argv)
        is_version = argv[-1] == "--version"
        failed = failed_call == ("version" if is_version else "auth")
        stdout = "2.1.110" if is_version else '{"loggedIn": true}'
        return type(
            "Result",
            (),
            {"returncode": 1 if failed else 0, "stdout": stdout, "stderr": ""},
        )()

    assert _claude_visibility_preflight(("claude",), runner=runner) is None
    assert all("--session-id" not in call and "--print" not in call for call in calls)


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
            messages=[ProjectedMessage("m", 0, "user", state["marker"], 10.0)],
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
            messages=[ProjectedMessage("m", 0, "user", state["marker"], 10.0)],
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
    state: dict[str, Any] = {}

    def reserve(projection: SessionProjection) -> ClaudeVisibilityClaim:
        claim, marker = _claim_for(projection)
        claims.append(claim)
        state.update(claim=claim, marker=marker)
        return claim

    class FailingAfterLaunchRegistrar:
        def process(self, claim: ClaudeVisibilityClaim) -> ClaudeRegistrarOutcome:
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
            messages=[ProjectedMessage("m", 0, "user", state["marker"], 10.0)],
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
        now=lambda: 11.0,
    )

    assert len(launches) == 1
    assert len(claims) == 1
    assert recovered["reserved_claude_uuid"] == launches[0]


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
            messages=[ProjectedMessage("m", 0, "user", state["marker"], 10.0)],
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
        registrar=Registrar(),
        restarted_source=restarted_source,
        marker_secret=SECRET,
        now=lambda: 11.0,
    )
    assert launches == 1
    assert recovered["cleanup_token"]["id"]
    assert recovered["cleanup_token"]["capability"]


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
                messages=[ProjectedMessage("m", 0, "user", state["marker"], 10.0)],
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
                    messages=[ProjectedMessage("m", 0, "user", state["marker"], 10.0)],
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
