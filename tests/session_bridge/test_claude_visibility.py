from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib

import pytest

from session_bridge.claude_visibility import (
    CLAUDE_VISIBILITY_EXCLUSION_CODES,
    CLAUDE_VISIBILITY_RETRY_CODES,
    build_claude_registration_prompt,
    build_claude_visibility_candidate,
    derive_claude_visibility_identity,
    evaluate_claude_visibility,
    normalized_claude_visibility_error,
    validate_claude_visibility_identity_binding,
)
from session_bridge.models import (
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
)


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
    "content, expected",
    [
        ("okay", "acknowledgement_only"),
        ("/resume", "control_only"),
        ("", "no_meaningful_request"),
    ],
)
def test_nonmeaningful_exclusion_precedes_source_cwd_validation(
    content: str,
    expected: str,
) -> None:
    projection = replace(_projection(Provider.HERMES, content=content), cwd=None)

    assert evaluate_claude_visibility(projection) == expected


@pytest.mark.parametrize(
    "cwd",
    [None, "", " C:/work/project", "C:/work/project\n", "x" * 4097],
)
def test_meaningful_request_requires_canonical_source_cwd(cwd: str | None) -> None:
    projection = replace(_projection(Provider.HERMES), cwd=cwd)

    assert "source_cwd_missing" in CLAUDE_VISIBILITY_EXCLUSION_CODES
    assert evaluate_claude_visibility(projection) == "source_cwd_missing"


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


def test_injected_internal_events_plus_context_only_retry_are_not_eligible() -> None:
    projection = replace(
        _projection(Provider.HERMES),
        messages=(
            ProjectedMessage(
                "model-switch",
                0,
                "user",
                (
                    "[System: The active model for this chat has changed to model-x "
                    "via provider provider-y.]"
                ),
                11.0,
            ),
            ProjectedMessage(
                "process-complete",
                1,
                "user",
                (
                    "[IMPORTANT: Background process proc_123 completed normally "
                    "(exit code 0). Command: test Output: done]"
                ),
                12.0,
            ),
            ProjectedMessage("retry", 2, "user", "try again", 13.0),
        ),
    )

    assert evaluate_claude_visibility(projection) == "no_meaningful_request"


@pytest.mark.parametrize(
    "content",
    [
        "<recommended_plugins>\nPlugin catalog injected by Codex",
        "# AGENTS.md instructions for C:/work/project\n\n<INSTRUCTIONS>rules",
        "<skill>\n<name>session-sidebar-sync</name>\ninjected skill body",
    ],
)
def test_codex_injected_context_is_not_a_meaningful_user_request(
    content: str,
) -> None:
    assert (
        evaluate_claude_visibility(_projection(Provider.CODEX, content=content))
        == "no_meaningful_request"
    )


@pytest.mark.parametrize(
    "content",
    [
        (
            "Automation: Session Sidebar Sync Canary\n"
            "Automation ID: session-sidebar-sync-canary\n"
            "Automation memory: $CODEX_HOME/automations/canary/memory.md\n"
            "Last run: never\n\nInvoke the worker exactly once."
        ),
        (
            "<heartbeat>\n"
            "  <automation_id>session-sidebar-backfill-rollout</automation_id>\n"
            "  <instructions>Continue the rollout.</instructions>\n"
            "</heartbeat>"
        ),
    ],
)
def test_codex_automation_envelopes_are_structurally_excluded(content: str) -> None:
    assert (
        evaluate_claude_visibility(_projection(Provider.CODEX, content=content))
        == "automation_only"
    )


@pytest.mark.parametrize(
    "registration",
    [
        (
            "Hermes Session Bridge registration only. "
            "Hermes Session Bridge placeholder.\n"
            "Signed marker: HERMES_SESSION_BRIDGE_V1:retired.signature\n"
            "Canonical source session: claude:source-1"
        ),
        (
            "Hermes Session Bridge registration only. Signed marker: "
            "HERMES_SESSION_BRIDGE_V1:retired.signature "
            "Do not perform project work; reply READY."
        ),
        (
            "Hermes registration diagnostic. Hermes Session Bridge diagnostic "
            "placeholder.\nSigned marker: "
            "HERMES_SESSION_BRIDGE_V1:retired.signature\nReply READY."
        ),
    ],
)
def test_legacy_codex_bridge_registration_is_excluded_even_after_key_rotation(
    registration: str,
) -> None:
    projection = replace(
        _projection(Provider.CODEX),
        messages=(
            ProjectedMessage(
                "registration",
                0,
                "user",
                registration,
                11.0,
            ),
            ProjectedMessage(
                "characterization",
                1,
                "user",
                "Hermes Bridge live characterization verification. Reply READY.",
                12.0,
            ),
        ),
    )

    assert evaluate_claude_visibility(projection) == "bridge_placeholder"


def test_current_codex_registration_is_excluded_after_provenance_and_key_loss() -> None:
    source = _projection(
        Provider.CODEX,
        native_id="original-source",
        content="Implement the original substantive request",
    )
    candidate = build_claude_visibility_candidate(source, eligible_at=30.0)
    identity = derive_claude_visibility_identity(candidate, SECRET)
    prompt = build_claude_registration_prompt(candidate, identity, SECRET)
    imported = _projection(
        Provider.CODEX,
        native_id="imported-registration",
        content=prompt,
        origin_kind=OriginKind.NATIVE,
        origin_bridge_id=None,
    )

    assert prompt.startswith(
        "This is a Hermes Session Bridge Claude visibility registration.\n"
        "Do not perform project work or use tools.\n"
        "Signed marker: HERMES_SESSION_BRIDGE_V1:"
    )
    assert evaluate_claude_visibility(imported) == "bridge_placeholder"


@pytest.mark.parametrize(
    "content",
    [
        (
            "This is a Hermes Session Bridge Claude visibility registration.\n"
            "Do perform project work or use tools.\n"
            "Signed marker: HERMES_SESSION_BRIDGE_V1:not-an-envelope"
        ),
        (
            "This is a Hermes Session Bridge Claude visibility registration. "
            "Do not perform project work or use tools.\n"
            "Signed marker: HERMES_SESSION_BRIDGE_V1:not-an-envelope"
        ),
        (
            "This is a Hermes Session Bridge Claude visibility registration.\n"
            "Do not perform project work or use tools.\n"
            "Unsigned marker: HERMES_SESSION_BRIDGE_V1:not-an-envelope"
        ),
    ],
)
def test_current_registration_structural_near_misses_remain_meaningful(
    content: str,
) -> None:
    assert evaluate_claude_visibility(_projection(Provider.CODEX, content=content)) == (
        "eligible"
    )


def test_codex_injected_context_does_not_hide_a_real_user_request() -> None:
    projection = replace(
        _projection(Provider.CODEX),
        messages=(
            ProjectedMessage(
                "plugins",
                0,
                "user",
                "<recommended_plugins>\nPlugin catalog injected by Codex",
                11.0,
            ),
            ProjectedMessage(
                "request",
                1,
                "user",
                "Repair the failing production deployment",
                12.0,
            ),
        ),
    )

    assert evaluate_claude_visibility(projection) == "eligible"


@pytest.mark.parametrize(
    "content",
    [
        (
            " [System: The active model for this chat has changed to model-x via "
            "provider provider-y.]"
        ),
        (
            "［System: The active model for this chat has changed to model-x via "
            "provider provider-y.]"
        ),
    ],
)
def test_internal_event_lookalikes_remain_meaningful_through_visibility(
    content: str,
) -> None:
    assert (
        evaluate_claude_visibility(_projection(Provider.HERMES, content=content))
        == "eligible"
    )


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


def test_registration_prompt_bytes_are_stable() -> None:
    candidate = build_claude_visibility_candidate(
        _projection(
            Provider.CODEX,
            native_id="prompt-byte-contract",
            content="Implement deterministic visibility",
        ),
        eligible_at=30.0,
    )
    identity = derive_claude_visibility_identity(
        candidate, b"prompt-byte-contract-secret"
    )

    prompt = build_claude_registration_prompt(
        candidate, identity, b"prompt-byte-contract-secret"
    )

    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
        "c0ad27b2ea817eb6eb4a2ba2c9e6c7825647a0d628af7aa0780b9f4db50eddc8"
    )


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

    prompt = build_claude_registration_prompt(candidate, identity, SECRET)

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
            candidate, derive_claude_visibility_identity(other, SECRET), SECRET
        )


def test_identity_validation_and_prompt_reject_canonical_forged_signature() -> None:
    candidate = build_claude_visibility_candidate(
        _projection(Provider.CODEX), eligible_at=30.0
    )
    identity = derive_claude_visibility_identity(candidate, SECRET)
    replacement = "A" if identity.signed_marker[-1] != "A" else "B"
    forged = replace(identity, signed_marker=identity.signed_marker[:-1] + replacement)

    with pytest.raises(ValueError, match="signed marker"):
        validate_claude_visibility_identity_binding(candidate, forged, SECRET)
    with pytest.raises(ValueError, match="signed marker"):
        build_claude_registration_prompt(candidate, forged, SECRET)


def test_registration_prompt_redacts_secrets_and_ascii_escapes_line_separators() -> (
    None
):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    candidate = build_claude_visibility_candidate(
        _projection(Provider.HERMES, content="safe request"),
        eligible_at=30.0,
        git_root=f"C:/work/{secret}",
        git_head="abc123",
        worktree_id="tree\u2028injected\u2029line",
    )
    candidate = replace(candidate, git_branch=f"feature/token={secret}\x1enext")
    identity = derive_claude_visibility_identity(candidate, SECRET)

    prompt = build_claude_registration_prompt(candidate, identity, SECRET)

    assert secret not in prompt
    assert "\\u2028" in prompt and "\\u2029" in prompt and "\\u001e" in prompt
    assert "\u2028" not in prompt and "\u2029" not in prompt and "\x1e" not in prompt
    assert len(prompt.splitlines()) == 7


def test_error_codes_are_fixed_and_malformed_values_fail_closed() -> None:
    assert "pty_unavailable" in CLAUDE_VISIBILITY_RETRY_CODES
    assert "pseudoterminal_unavailable" not in CLAUDE_VISIBILITY_RETRY_CODES
    assert normalized_claude_visibility_error(["not", "hashable"]) == (
        "unknown_error_code",
        False,
    )
