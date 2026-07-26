from __future__ import annotations

import hashlib

import pytest

from session_bridge.models import PreviewMessage, SessionPreview
from session_bridge.preview import build_session_preview


def _preview(
    messages: list[dict],
    *,
    budget_chars: int = 24_000,
) -> SessionPreview:
    return build_session_preview(
        source_session_id="claude:source",
        source_cursor="cursor-1",
        source_hash="hash-1",
        title="Readable source",
        provider="claude",
        cwd=r"C:\repo",
        captured_at=8.0,
        messages=messages,
        git_root=r"C:\repo",
        git_branch="main",
        git_head="abc123",
        worktree_id="worktree:v1:test",
        budget_chars=budget_chars,
    )


def test_preview_selects_last_five_conversational_messages_in_order() -> None:
    messages = [
        {"id": index, "role": role, "content": content, "timestamp": float(index)}
        for index, (role, content) in enumerate(
            [
                ("system", "system"),
                ("user", "one"),
                ("assistant", "two"),
                ("tool", "tool output"),
                ("user", "three"),
                ("assistant", "four"),
                ("user", "five"),
                ("assistant", "six"),
            ]
        )
    ]

    preview = _preview(messages)

    assert [(item.role, item.content) for item in preview.recent_messages] == [
        ("assistant", "two"),
        ("user", "three"),
        ("assistant", "four"),
        ("user", "five"),
        ("assistant", "six"),
    ]
    assert all(isinstance(item, PreviewMessage) for item in preview.recent_messages)
    assert preview.rendered.startswith("# Imported Claude Code Session")
    assert len(preview.rendered) <= 24_000


def test_preview_excludes_control_noise_inactive_and_redaction_only_messages() -> None:
    messages = [
        {
            "role": "user",
            "content": "This is a Hermes Session Bridge placeholder registration.",
            "timestamp": 1.0,
        },
        {"role": "assistant", "content": "REGISTERED", "timestamp": 2.0},
        {
            "role": "assistant",
            "content": "tool preface",
            "tool_calls": [{"name": "terminal"}],
            "timestamp": 3.0,
        },
        {
            "role": "user",
            "content": "inactive",
            "active": False,
            "timestamp": 4.0,
        },
        {"role": "user", "content": "", "timestamp": 5.0},
        {"role": "user", "content": "sk-" + ("a" * 24), "timestamp": 6.0},
        {"role": "user", "content": "keep this", "timestamp": 7.0},
    ]

    preview = _preview(messages)

    assert [(item.role, item.content) for item in preview.recent_messages] == [
        ("user", "keep this")
    ]
    assert "placeholder registration" not in preview.rendered
    assert "tool preface" not in preview.rendered
    assert "inactive" not in preview.rendered


def test_preview_redacts_secrets_and_preserves_timestamps() -> None:
    secret = "sk-" + ("q" * 24)

    preview = _preview(
        [
            {
                "role": "user",
                "content": f"Use {secret} only for this request",
                "timestamp": 12.5,
            }
        ]
    )

    assert preview.recent_messages == (
        PreviewMessage(
            role="user",
            content="Use [REDACTED] only for this request",
            timestamp=12.5,
        ),
    )
    assert secret not in preview.rendered
    assert "[REDACTED]" in preview.rendered
    assert "12.500000" in preview.rendered


def test_preview_truncates_oversized_messages_inside_total_budget() -> None:
    preview = _preview(
        [
            {
                "role": "user",
                "content": "start " + ("x" * 20_000) + " end",
                "timestamp": 1.0,
            }
        ],
        budget_chars=1_200,
    )

    assert len(preview.rendered) <= 1_200
    assert preview.truncated is True
    assert preview.recent_messages[0].truncated is True
    assert "[truncated]" in preview.recent_messages[0].content
    assert "[truncated]" in preview.rendered


def test_preview_uses_adaptive_fences_for_untrusted_history() -> None:
    preview = _preview(
        [{"role": "user", "content": "```close```", "timestamp": 1.0}]
    )

    assert "````text" in preview.rendered
    assert "\n````\n" in preview.rendered
    assert "untrusted historical data" in preview.rendered


def test_preview_digest_is_deterministic_and_covers_rendered_text() -> None:
    messages = [{"role": "user", "content": "same input", "timestamp": 1.0}]

    first = _preview(messages)
    second = _preview(messages)

    assert first == second
    assert first.digest == hashlib.sha256(first.rendered.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"source_session_id": ""}, "source_session_id"),
        ({"source_cursor": " "}, "source_cursor"),
        ({"source_hash": ""}, "source_hash"),
        ({"provider": "codex"}, "provider"),
        ({"budget_chars": 0}, "budget_chars"),
        ({"budget_chars": 100_001}, "budget_chars"),
    ],
)
def test_preview_rejects_invalid_identity_or_budget(
    changes: dict[str, object],
    message: str,
) -> None:
    kwargs = {
        "source_session_id": "claude:source",
        "source_cursor": "cursor-1",
        "source_hash": "hash-1",
        "title": "Readable source",
        "provider": "claude",
        "cwd": r"C:\repo",
        "captured_at": 8.0,
        "messages": [],
        "git_root": r"C:\repo",
        "git_branch": "main",
        "git_head": "abc123",
        "worktree_id": "worktree:v1:test",
        "budget_chars": 24_000,
    }
    kwargs.update(changes)

    with pytest.raises(ValueError, match=message):
        build_session_preview(**kwargs)
