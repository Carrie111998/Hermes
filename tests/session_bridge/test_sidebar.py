from __future__ import annotations

from dataclasses import replace

import pytest

from session_bridge.models import (
    BridgeMarkerPayload,
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    encode_bridge_marker,
)
from session_bridge.sidebar import (
    ACK_OR_CONTROL_ONLY,
    SidebarCandidate,
    build_registration_prompt,
    is_meaningful_user_text,
    is_sidebar_session_eligible,
    normalize_meaningful_user_text,
    sidebar_bridge_id,
    sidebar_idempotency_key,
    sidebar_title,
)


NOW = 1_800_000_000.0
MARKER = encode_bridge_marker(
    BridgeMarkerPayload(
        bridge_id="sidebar:bridge-1",
        source_session_id="claude:source-1",
        target_provider=Provider.CODEX,
        policy_generation=1,
    ),
    b"sidebar-tests-marker-secret",
)


def _message(
    content: str | None,
    *,
    role: str = "user",
    ordinal: int = 0,
) -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=f"event-{ordinal}",
        ordinal=ordinal,
        role=role,
        content=content,
        timestamp=NOW - 10.0 + ordinal,
    )


def _projection(
    provider: Provider,
    *contents: str | None,
    messages: tuple[ProjectedMessage, ...] | None = None,
    last_active: float = NOW,
    origin_kind: OriginKind = OriginKind.NATIVE,
) -> SessionProjection:
    if messages is None:
        messages = tuple(
            _message(content, ordinal=ordinal)
            for ordinal, content in enumerate(contents)
        )
    return SessionProjection(
        provider=provider,
        native_id=f"{provider.value}-native-1",
        title=None,
        cwd=r"C:\Users\diego\repo",
        started_at=NOW - 100.0,
        last_active=last_active,
        messages=messages,
        origin_kind=origin_kind,
    )


@pytest.mark.parametrize("provider", [Provider.CLAUDE, Provider.HERMES])
@pytest.mark.parametrize("content", ["fix it", "build X"])
def test_native_claude_and_hermes_requests_are_eligible(
    provider: Provider,
    content: str,
) -> None:
    assert is_sidebar_session_eligible(_projection(provider, content), now=NOW)


@pytest.mark.parametrize("provider", [Provider.CLAUDE, Provider.HERMES])
@pytest.mark.parametrize(
    "content",
    [
        "ok",
        "okay",
        "yes",
        "y",
        "   \t\n",
        "READY",
        "resume",
        "/resume",
        "clear",
        "/clear",
        "help",
        "/help",
        "quit",
        "/quit",
    ],
)
def test_acknowledgement_and_control_only_sessions_are_ineligible(
    provider: Provider,
    content: str,
) -> None:
    assert not is_sidebar_session_eligible(_projection(provider, content), now=NOW)


def test_acknowledgement_plus_separate_request_is_eligible() -> None:
    projection = _projection(Provider.CLAUDE, "okay", "please fix it")

    assert is_sidebar_session_eligible(projection, now=NOW)


@pytest.mark.parametrize("role", ["tool", "system", "developer"])
@pytest.mark.parametrize("provider", [Provider.CLAUDE, Provider.HERMES])
def test_non_user_messages_never_count(role: str, provider: Provider) -> None:
    projection = _projection(
        provider,
        messages=(_message("build the entire feature", role=role),),
    )

    assert not is_sidebar_session_eligible(projection, now=NOW)


@pytest.mark.parametrize("provider", [Provider.CLAUDE, Provider.HERMES])
def test_signed_registration_message_does_not_count(provider: Provider) -> None:
    registration = (
        "Register this long placeholder session and preserve all metadata. "
        f"Authenticated registration marker: {MARKER}. "
        "Do not start project work during registration, even though this prose is long."
    )

    assert not is_sidebar_session_eligible(
        _projection(provider, registration),
        now=NOW,
    )


def test_registration_message_does_not_hide_separate_meaningful_request() -> None:
    projection = _projection(
        Provider.HERMES,
        f"Registration metadata: {MARKER}",
        "now repair the failing build",
    )

    assert is_sidebar_session_eligible(projection, now=NOW)


@pytest.mark.parametrize(
    ("flag", "kwargs"),
    [
        ("automation", {"automation_only": True}),
        ("subagent", {"subagent_only": True}),
    ],
)
def test_structural_run_flags_exclude_meaningful_session(
    flag: str,
    kwargs: dict[str, bool],
) -> None:
    del flag
    projection = _projection(Provider.CLAUDE, "build the release")

    assert not is_sidebar_session_eligible(projection, now=NOW, **kwargs)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("  e\u0301中２  ", "é中2"),
        ("  déjà vu  ", "déjà vu"),
        ("\t\n", None),
        (None, None),
        (42, None),
    ],
)
def test_meaningful_text_normalization_is_nfkc_without_accent_stripping(
    content: object,
    expected: str | None,
) -> None:
    assert normalize_meaningful_user_text(content) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [("é中2", True), ("é中", False), ("１２3", True), ("a-2", False)],
)
def test_meaningful_text_requires_three_unicode_alphanumerics(
    content: object,
    expected: bool,
) -> None:
    assert is_meaningful_user_text(content) is expected


def test_acknowledgement_control_vocabulary_is_frozen() -> None:
    assert ACK_OR_CONTROL_ONLY == frozenset({
        "ok",
        "okay",
        "yes",
        "y",
        "ready",
        "resume",
        "/resume",
        "clear",
        "/clear",
        "help",
        "/help",
        "quit",
        "/quit",
    })


@pytest.mark.parametrize(
    ("last_active", "expected"),
    [
        (NOW - 30 * 86_400, True),
        (NOW - 30 * 86_400 - 0.001, False),
    ],
)
def test_backfill_boundary_is_inclusive(last_active: float, expected: bool) -> None:
    projection = _projection(
        Provider.HERMES,
        "fix the issue",
        last_active=last_active,
    )

    assert is_sidebar_session_eligible(projection, now=NOW) is expected


@pytest.mark.parametrize(
    ("provider", "origin_kind"),
    [
        (Provider.CODEX, OriginKind.NATIVE),
        (Provider.CLAUDE, OriginKind.BRIDGE_PLACEHOLDER),
        (Provider.HERMES, OriginKind.BRIDGE_CONTINUATION),
    ],
)
def test_non_source_and_bridge_lineage_sessions_are_ineligible(
    provider: Provider,
    origin_kind: OriginKind,
) -> None:
    projection = _projection(
        provider,
        "build the feature",
        origin_kind=origin_kind,
    )

    assert not is_sidebar_session_eligible(projection, now=NOW)


@pytest.mark.parametrize(
    ("projection", "kwargs", "message"),
    [
        (
            _projection(Provider.CODEX, "build the feature"),
            {"now": "not-a-timestamp"},
            "now must be a finite timestamp",
        ),
        (
            _projection(
                Provider.CLAUDE,
                "build the feature",
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            ),
            {"now": float("nan")},
            "now must be a finite timestamp",
        ),
        (
            _projection(Provider.CLAUDE, "build the feature"),
            {"now": NOW, "backfill_days": -1, "automation_only": True},
            "backfill days must be a non-negative integer",
        ),
        (
            _projection(Provider.HERMES, "build the feature"),
            {"now": NOW, "backfill_days": True, "subagent_only": True},
            "backfill days must be a non-negative integer",
        ),
    ],
)
def test_numeric_arguments_are_validated_before_structural_exclusions(
    projection: SessionProjection,
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        is_sidebar_session_eligible(projection, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("last_active", "provider", "origin_kind"),
    [
        ("not-a-timestamp", Provider.CODEX, OriginKind.NATIVE),
        (True, Provider.CLAUDE, OriginKind.BRIDGE_PLACEHOLDER),
        (float("nan"), Provider.HERMES, OriginKind.NATIVE),
        (float("inf"), Provider.CLAUDE, OriginKind.NATIVE),
        (float("-inf"), Provider.HERMES, OriginKind.BRIDGE_CONTINUATION),
    ],
)
def test_projection_activity_must_be_a_finite_non_boolean_number_before_exclusion(
    last_active: object,
    provider: Provider,
    origin_kind: OriginKind,
) -> None:
    projection = replace(
        _projection(provider, "build the feature", origin_kind=origin_kind),
        last_active=last_active,
    )

    with pytest.raises(
        ValueError,
        match="^projection last_active must be a finite timestamp$",
    ):
        is_sidebar_session_eligible(projection, now=NOW)


def test_sidebar_delivery_identity_is_exact_stable_and_source_sensitive() -> None:
    source = "claude:source-session-id"
    expected_key = "codex-sidebar:claude:source-session-id:v1"

    assert sidebar_idempotency_key(source) == expected_key
    assert sidebar_bridge_id(source) == (
        "sidebar:"
        "c32bbc152a5308a637e7fbadad3afb6069b98994b32bc948b2f3e1ed7b9f85c4"
    )
    assert sidebar_bridge_id(source) == sidebar_bridge_id(source)
    assert sidebar_bridge_id(source) != sidebar_bridge_id("claude:other-source")


@pytest.mark.parametrize(
    "source_session_id",
    ["claude:source-1", "hermes-source-1", "hermes:source-1"],
)
def test_sidebar_delivery_identity_accepts_only_canonical_native_sources(
    source_session_id: str,
) -> None:
    assert sidebar_idempotency_key(source_session_id) == (
        f"codex-sidebar:{source_session_id}:v1"
    )


@pytest.mark.parametrize(
    ("source_session_id", "message"),
    [
        (None, "source session ID must not be empty"),
        ("", "source session ID must not be empty"),
        ("   ", "source session ID must not be empty"),
        (" claude:source-1", "source session ID must be canonical"),
        ("claude:source-1 ", "source session ID must be canonical"),
        ("claude:", "source session ID must identify native Claude or Hermes"),
        (
            "codex:source-1",
            "source session ID must identify native Claude or Hermes",
        ),
        ("claude: source-1", "source session ID must be canonical"),
    ],
)
def test_sidebar_delivery_identity_rejects_noncanonical_sources(
    source_session_id: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        sidebar_idempotency_key(source_session_id)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=f"^{message}$"):
        sidebar_bridge_id(source_session_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("line_break", ["\r", "\n", "\x85", "\u2028", "\u2029"])
def test_sidebar_source_identity_rejects_all_unicode_line_injection(
    line_break: str,
) -> None:
    source = f"claude:source{line_break}injected"

    with pytest.raises(ValueError, match="^source session ID must be canonical$"):
        sidebar_idempotency_key(source)
    with pytest.raises(ValueError, match="^source session ID must be canonical$"):
        sidebar_bridge_id(source)


@pytest.mark.parametrize(
    ("provider", "expected_prefix"),
    [(Provider.CLAUDE, "[Claude] "), (Provider.HERMES, "[Hermes] ")],
)
def test_sidebar_title_uses_provider_prefix_compacts_redacts_and_bounds(
    provider: Provider,
    expected_prefix: str,
) -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    title = f"  Ship\n\t{secret}  " + "z" * 200

    result = sidebar_title(provider, title, "unused first request")

    assert result.startswith(expected_prefix + "Ship [REDACTED] ")
    assert secret not in result
    assert "\n" not in result
    assert "\t" not in result
    assert len(result) == 120


@pytest.mark.parametrize(
    ("title", "first_request", "expected"),
    [
        (None, "  fix\n  the build  ", "[Claude] fix the build"),
        (" \t\n ", "  build X  ", "[Claude] build X"),
    ],
)
def test_sidebar_title_falls_back_to_first_meaningful_request(
    title: str | None,
    first_request: str,
    expected: str,
) -> None:
    assert sidebar_title(Provider.CLAUDE, title, first_request) == expected


def test_sidebar_title_rejects_unsupported_provider_and_empty_source() -> None:
    with pytest.raises(
        ValueError,
        match="^sidebar title provider must be Claude or Hermes$",
    ):
        sidebar_title(Provider.CODEX, "title", "request")
    with pytest.raises(ValueError, match="^sidebar title source must not be empty$"):
        sidebar_title(Provider.HERMES, None, "  ")


def _candidate(**changes: object) -> SidebarCandidate:
    candidate = SidebarCandidate(
        source_session_id="claude:source-1",
        provider=Provider.CLAUDE,
        bridge_id=sidebar_bridge_id("claude:source-1"),
        title="[Claude] raw title must stay out",
        cwd=r"C:\Users\diego\repo",
        git_root=r"C:\Users\diego\repo",
        git_branch="feature/sidebar",
        git_head="0123456789abcdef0123456789abcdef01234567",
        worktree_id="worktree-1",
        eligible_at=NOW,
    )
    return replace(candidate, **changes)


def test_registration_prompt_has_exact_minimal_field_order_and_instructions() -> None:
    prompt = build_registration_prompt(_candidate(), MARKER)

    assert prompt == (
        "This is a Hermes Session Bridge placeholder registration.\n"
        "Do not perform project work during registration.\n"
        f"Signed marker: {MARKER}\n"
        "Source session ID: claude:source-1\n"
        "Source provider: claude\n"
        "Source cwd: C:\\Users\\diego\\repo\n"
        "Git root: C:\\Users\\diego\\repo\n"
        "Git branch: feature/sidebar\n"
        "Git HEAD: 0123456789abcdef0123456789abcdef01234567\n"
        "Worktree ID: worktree-1\n"
        "Before substantive work, call "
        'session_continue(session_id=claude:source-1, target_provider="codex").\n'
        "Wait for the first substantive user message before doing anything else."
    )
    assert "raw title must stay out" not in prompt
    assert "first request" not in prompt
    assert "transcript" not in prompt.casefold()


def test_registration_prompt_represents_missing_git_metadata_stably() -> None:
    prompt = build_registration_prompt(
        _candidate(
            provider=Provider.HERMES,
            source_session_id="hermes-source-1",
            bridge_id=sidebar_bridge_id("hermes-source-1"),
            title="[Hermes] source",
            git_root=None,
            git_branch=None,
            git_head=None,
            worktree_id=None,
        ),
        MARKER,
    )

    assert "Source provider: hermes" in prompt
    assert "Git root: (none)" in prompt
    assert "Git branch: (none)" in prompt
    assert "Git HEAD: (none)" in prompt
    assert "Worktree ID: (none)" in prompt


def test_registration_prompt_redacts_metadata_but_preserves_marker_exactly() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    prompt = build_registration_prompt(
        _candidate(git_branch=f"feature/{secret}"),
        MARKER,
    )

    assert prompt.count(MARKER) == 1
    assert secret not in prompt
    assert "Git branch: feature/[REDACTED]" in prompt


@pytest.mark.parametrize("line_break", ["\r", "\n", "\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("cwd", "sidebar candidate cwd must be a single line"),
        ("git_root", "sidebar candidate git root must be a single line"),
        ("git_branch", "sidebar candidate git branch must be a single line"),
        ("git_head", "sidebar candidate git HEAD must be a single line"),
        ("worktree_id", "sidebar candidate worktree ID must be a single line"),
    ],
)
def test_registration_metadata_rejects_all_unicode_line_injection(
    field: str,
    message: str,
    line_break: str,
) -> None:
    candidate = _candidate(**{field: f"value{line_break}injected"})

    with pytest.raises(ValueError, match=f"^{message}$"):
        build_registration_prompt(candidate, MARKER)


@pytest.mark.parametrize(
    ("candidate", "marker", "message"),
    [
        (_candidate(source_session_id=""), MARKER, "source session ID must not be empty"),
        (
            _candidate(source_session_id=" claude:source-1"),
            MARKER,
            "source session ID must be canonical",
        ),
        (
            _candidate(provider=Provider.CODEX),
            MARKER,
            "sidebar candidate provider must be Claude or Hermes",
        ),
        (
            _candidate(provider="claude"),
            MARKER,
            "sidebar candidate provider must be Claude or Hermes",
        ),
        (_candidate(cwd=""), MARKER, "sidebar candidate cwd must not be empty"),
        (_candidate(cwd=None), MARKER, "sidebar candidate cwd must not be empty"),
        (
            _candidate(worktree_id=""),
            MARKER,
            "sidebar candidate worktree ID must not be empty",
        ),
        (_candidate(), "", "registration marker is malformed"),
        (
            _candidate(),
            "HERMES_SESSION_BRIDGE_V1:body.signature.extra",
            "registration marker is malformed",
        ),
        (
            _candidate(bridge_id="sidebar:wrong"),
            MARKER,
            "candidate bridge ID must match source session ID",
        ),
    ],
)
def test_registration_prompt_rejects_invalid_identity_fields(
    candidate: SidebarCandidate,
    marker: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        build_registration_prompt(candidate, marker)
