from __future__ import annotations

from types import SimpleNamespace

from agent.message_content import (
    build_cli_handoff_notice,
    build_resume_recovery_note,
    flatten_message_text,
    has_non_text_content,
    is_internal_user_scaffolding_text,
)
from tools.todo_tool import TODO_INJECTION_HEADER


def test_flatten_message_text_accepts_chat_and_responses_text_parts():
    content = [
        {"type": "text", "text": "chat text"},
        {"type": "input_text", "text": "user text"},
        {"type": "output_text", "text": "assistant text"},
        {"type": "summary_text", "text": "summary text"},
    ]

    assert flatten_message_text(content) == "chat text\nuser text\nassistant text\nsummary text"


def test_flatten_message_text_accepts_object_parts():
    content = [
        SimpleNamespace(type="output_text", text="object text"),
        {"content": "legacy content"},
    ]

    assert flatten_message_text(content) == "object text\nlegacy content"


def test_has_non_text_content_accepts_media_and_structured_parts():
    assert has_non_text_content(
        [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]
    )
    assert has_non_text_content(
        [SimpleNamespace(type="input_audio", input_audio={"data": "AA=="})]
    )
    assert has_non_text_content(
        [{"type": "document", "document": {"name": "notes.pdf"}}]
    )


def test_has_non_text_content_rejects_text_and_blank_inputs():
    assert not has_non_text_content("plain text")
    assert not has_non_text_content([])
    assert not has_non_text_content([{"type": "text", "text": "hello"}])
    assert not has_non_text_content(SimpleNamespace(type="output_text", text="hello"))


def test_flatten_message_text_does_not_stringify_empty_structured_parts():
    assert flatten_message_text({}) == ""
    assert flatten_message_text({"type": "text", "text": ""}) == ""
    assert flatten_message_text(SimpleNamespace(type="output_text", text="")) == ""


def test_media_with_todo_text_keeps_structured_provenance():
    todo = f"{TODO_INJECTION_HEADER}\n- [>] Finish the task"
    content = [
        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
        {"type": "text", "text": todo},
    ]

    assert has_non_text_content(content)
    assert flatten_message_text(content) == todo


def test_goal_continuations_match_only_complete_runtime_messages():
    from hermes_cli.goals import (
        CONTINUATION_PROMPT_TEMPLATE,
        CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE,
        CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE,
    )

    prompts = [
        CONTINUATION_PROMPT_TEMPLATE.format(goal="Ship the title fix"),
        CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE.format(
            goal="Ship the title fix",
            contract_block="Outcome: fixed\nVerification: focused tests pass",
        ),
        CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE.format(
            goal="Ship the title fix",
            subgoals_block="1. Preserve media provenance",
        ),
    ]

    for prompt in prompts:
        assert is_internal_user_scaffolding_text(prompt)
        assert not is_internal_user_scaffolding_text(
            f"Why did Hermes send this?\n{prompt}"
        )
        assert not is_internal_user_scaffolding_text(
            f"{prompt}\nPlease explain that message."
        )


def test_pure_resume_recovery_notes_are_internal_but_new_human_text_is_not():
    for reason in ("restart_timeout", "shutdown_timeout", "other"):
        for interactive in (True, False):
            note = build_resume_recovery_note(reason, "", interactive=interactive)
            assert is_internal_user_scaffolding_text(note)
            assert not is_internal_user_scaffolding_text(
                f"Please explain this note:\n{note}"
            )

            note_with_human_message = build_resume_recovery_note(
                reason,
                "Please review the new failure",
                interactive=interactive,
            )
            assert not is_internal_user_scaffolding_text(note_with_human_message)


def test_cli_handoff_notice_matches_only_the_complete_runtime_envelope():
    notice = build_cli_handoff_notice("Title repair")

    assert is_internal_user_scaffolding_text(notice)
    assert not is_internal_user_scaffolding_text(
        f"Why did Hermes send this?\n{notice}"
    )
    assert not is_internal_user_scaffolding_text(
        f"{notice}\nPlease explain the handoff."
    )
    assert not is_internal_user_scaffolding_text(build_cli_handoff_notice(""))
    assert not is_internal_user_scaffolding_text(build_cli_handoff_notice("   "))
    assert not is_internal_user_scaffolding_text(
        build_cli_handoff_notice("Title\nrepair")
    )
