from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import pytest

from session_bridge.characterize import characterize_claude_visibility
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


def test_characterization_reserves_once_registers_once_restarts_and_cleans_exact_transcript(
    tmp_path: Path,
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
        now=lambda: 10.0,
    )

    assert len(reserved) == 1
    assert len(registrar.claims) == 1
    assert restarts == 1
    assert reserved[0].cwd == registrar.claims[0].source_cwd
    assert Path(reserved[0].cwd or "").parent == source_root.resolve()
    assert result["reserved_claude_uuid"] == registrar.claims[0].reserved_claude_uuid
    assert result["restart_exact_id_verified"] is True
    assert result["operator_checks"] == [
        "Run /resume in Claude Code and select the deterministic characterization name.",
        "Press Ctrl+A in /resume to verify the exact session across all projects.",
        f"Resume the exact ID with: claude --resume {registrar.claims[0].reserved_claude_uuid}",
    ]
    assert result["cleanup"] == "removed_exact_characterization"
    assert not state["transcript"].exists()
    assert not Path(reserved[0].cwd or "").exists()


def test_claude_preflight_reads_only_version_and_auth_without_registration() -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "2.1.110"
        stderr = ""

    def runner(argv: list[str], **_kwargs: Any) -> Result:
        calls.append(argv)
        return Result()

    assert _claude_visibility_preflight(("claude",), runner=runner) == {
        "version": "2.1.110",
        "authentication": "available",
    }
    assert calls == [
        ["claude", "--version"],
        ["claude", "auth", "status", "--json"],
    ]
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
                "cleanup": "removed_exact_characterization",
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
        "cleanup": "removed_exact_characterization",
        "passed": True,
        "restart_exact_id_verified": True,
    }


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
            now=lambda: 10.0,
        )
    assert state["transcript"].exists()
