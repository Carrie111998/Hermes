from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from session_bridge.claude_visibility import (
    CLAUDE_VISIBILITY_EXCLUSION_CODES,
    CLAUDE_VISIBILITY_RETRY_CODES,
    build_claude_registration_prompt,
    build_claude_visibility_candidate,
    derive_claude_visibility_identity,
    evaluate_claude_visibility,
    normalized_claude_visibility_error,
)
from session_bridge.models import OriginKind, ProjectedMessage, Provider, SessionProjection


SECRET = b"claude-visibility-test-secret"


def _projection(
    provider: Provider,
    *,
    native_id: str = "native-1",
    content: str = "Implement deterministic visibility",
    title: str | None = None,
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=title,
        cwd="C:/work/project",
        started_at=10.0,
        last_active=20.0,
        messages=(
            ProjectedMessage(
                native_event_id="event-1",
                ordinal=0,
                role="user",
                content=content,
                timestamp=11.0,
            ),
        ),
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
        git_branch="main",
    )


@pytest.mark.parametrize("provider", [Provider.CODEX, Provider.HERMES])
def test_native_meaningful_codex_and_hermes_are_eligible(provider: Provider) -> None:
    result = evaluate_claude_visibility(_projection(provider))

    assert result == "eligible"


@pytest.mark.parametrize(
    ("projection", "flags", "reason"),
    [
        (_projection(Provider.CLAUDE), {}, "source_claude"),
        (
            _projection(
                Provider.CODEX,
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id="bridge:placeholder",
            ),
            {},
            "bridge_placeholder",
        ),
        (
            _projection(
                Provider.HERMES,
                origin_kind=OriginKind.BRIDGE_CONTINUATION,
                origin_bridge_id="bridge:continuation",
            ),
            {},
            "bridge_continuation",
        ),
        (_projection(Provider.CODEX), {"automation_only": True}, "automation_only"),
        (_projection(Provider.HERMES), {"subagent_only": True}, "subagent_only"),
        (_projection(Provider.CODEX, content="okay"), {}, "acknowledgement_only"),
        (_projection(Provider.HERMES, content="/resume"), {}, "control_only"),
    ],
)
def test_fixed_reverse_loop_and_nonmeaningful_exclusions(
    projection: SessionProjection,
    flags: dict[str, bool],
    reason: str,
) -> None:
    assert reason in CLAUDE_VISIBILITY_EXCLUSION_CODES
    assert evaluate_claude_visibility(projection, **flags) == reason


def test_identity_name_and_marker_are_stable_across_restart_and_frozen() -> None:
    projection = _projection(
        Provider.CODEX,
        title=None,
        content="  Build\n\t the API with token=secret-key and deterministic identity  ",
    )
    first_candidate = build_claude_visibility_candidate(
        projection,
        eligible_at=30.0,
        git_root="C:/work/project",
        git_head="abc123",
        worktree_id="worktree-1",
    )
    restarted_candidate = build_claude_visibility_candidate(
        projection,
        eligible_at=30.0,
        git_root="C:/work/project",
        git_head="abc123",
        worktree_id="worktree-1",
    )
    first = derive_claude_visibility_identity(first_candidate, SECRET)
    restarted = derive_claude_visibility_identity(restarted_candidate, SECRET)

    assert first_candidate == restarted_candidate
    assert first == restarted
    assert first_candidate.native_name.startswith("[Codex] Build the API with ")
    assert "secret-key" not in first_candidate.native_name
    assert len(first_candidate.native_name) <= 120
    assert first.claude_uuid == restarted.claude_uuid
    assert first.bridge_id == restarted.bridge_id
    assert first.idempotency_key == restarted.idempotency_key
    assert first.signed_marker == restarted.signed_marker
    with pytest.raises(FrozenInstanceError):
        first.claude_uuid = "replacement"  # type: ignore[misc]


def test_hermes_name_uses_bounded_normalized_original_request() -> None:
    candidate = build_claude_visibility_candidate(
        _projection(
            Provider.HERMES,
            title="Unrelated provider-generated title",
            content="\u212b" * 300,
        ),
        eligible_at=30.0,
    )

    assert candidate.native_name.startswith("[Hermes] ÅÅÅ")
    assert len(candidate.native_name) == 120


def test_provider_or_source_identity_changes_reserved_uuid() -> None:
    identities = {
        derive_claude_visibility_identity(
            build_claude_visibility_candidate(
                _projection(provider, native_id=native_id), eligible_at=30.0
            ),
            SECRET,
        ).claude_uuid
        for provider, native_id in (
            (Provider.CODEX, "same"),
            (Provider.HERMES, "same"),
            (Provider.CODEX, "different"),
        )
    }

    assert len(identities) == 3


def test_registration_prompt_is_bounded_signed_metadata_without_transcript() -> None:
    secret_body = "SOURCE TRANSCRIPT BODY MUST NEVER APPEAR"
    candidate = build_claude_visibility_candidate(
        _projection(Provider.HERMES, content=secret_body),
        eligible_at=30.0,
        git_root="C:/work/project",
        git_head="abc123",
        worktree_id="worktree-1",
    )
    identity = derive_claude_visibility_identity(candidate, SECRET)

    prompt = build_claude_registration_prompt(candidate, identity)

    assert identity.signed_marker in prompt
    assert candidate.source_session_id in prompt
    assert identity.bridge_id in prompt
    assert candidate.source_provider.value in prompt
    assert candidate.source_cwd in prompt
    assert "reply exactly REGISTERED" in prompt
    assert "session_continue" in prompt
    assert secret_body not in prompt
    assert candidate.native_name not in prompt
    assert len(prompt) <= 8192


def test_registration_prompt_rejects_mismatched_deterministic_identity() -> None:
    candidate = build_claude_visibility_candidate(
        _projection(Provider.CODEX, native_id="source-a"), eligible_at=30.0
    )
    other = build_claude_visibility_candidate(
        _projection(Provider.CODEX, native_id="source-b"), eligible_at=30.0
    )

    with pytest.raises(ValueError, match="does not match candidate"):
        build_claude_registration_prompt(
            candidate, derive_claude_visibility_identity(other, SECRET)
        )


def test_error_codes_are_fixed_and_malformed_values_fail_closed() -> None:
    assert "pty_unavailable" in CLAUDE_VISIBILITY_RETRY_CODES
    assert "pseudoterminal_unavailable" not in CLAUDE_VISIBILITY_RETRY_CODES
    assert normalized_claude_visibility_error(["not", "hashable"]) == (
        "unknown_error_code",
        False,
    )
