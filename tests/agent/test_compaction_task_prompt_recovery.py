"""Regression coverage for #100818 — compaction during a cron run drops
the job prompt, and the run still reports success.

The #58753 zero-user guard pins the summary to ``role="user"`` when no user
turn survives, so the request is shape-valid. But the summary's preamble
tells the model "Do NOT answer questions in this summary … If no user
message appears AFTER this summary, do nothing". In an interactive session
a follow-up arrives; in a cron/bot run the swallowed job prompt was the ONLY
user message — the model correctly obeys and returns nothing (frequently
the literal ``[SILENT]`` sentinel), and the scheduler logs a successful,
empty delivery.

Production evidence from the report: 17 of 60 daily runs delivered under
800 chars vs 1150-char healthy runs; failures logged ``last_status: ok``;
the ``[SILENT]`` dud even switched off the reporter's own output guard.

The fix: when the zero-user guard fires, re-append the newest REAL user
turn from the compressed window as a fresh user message AFTER the summary —
the summary stays reference material, the model has a live instruction.

These tests exercise the REAL ``ContextCompressor.compress()`` path with a
stubbed summarizer (the same pattern as test_compressor_zero_user_guard.py).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture()
def compressor():
    from agent.context_compressor import ContextCompressor

    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        c = ContextCompressor(
            model="test/model",
            threshold_percent=0.50,
            protect_first_n=3,
            protect_last_n=20,
            quiet_mode=True,
        )
        c.tail_token_budget = 40
        return c


def _tool_turns(start: int, n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(start, start + n):
        out.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "function": {"name": "read_task", "arguments": "{}"},
                    }
                ],
            }
        )
        out.append({"role": "tool", "content": "x" * 300, "tool_call_id": f"c{i}"})
    return out


def _cron_run(job_prompt: str) -> list[dict]:
    """The #100818 shape: a cron run is one user prompt + tool work, with no
    follow-up user message ever arriving."""
    return [{"role": "user", "content": job_prompt}] + _tool_turns(0, 12)


class TestTaskPromptSurvivesCompaction:
    def test_cron_prompt_reappended_after_summary(self, compressor):
        """The incident witness: single job prompt swallowed by compaction
        must come back as a live user message after the summary."""
        from agent.context_compressor import SUMMARY_PREFIX

        c = compressor
        c.compression_count = 1  # re-compaction: protect_first_n decays to 0
        job_prompt = "Daily briefing: check the weather, email me a summary, and report disk usage"
        messages = _cron_run(job_prompt)

        mocked = f"{SUMMARY_PREFIX}\nsummary of the morning's tool work"
        with patch.object(c, "_generate_summary", return_value=mocked):
            out = c.compress(messages, current_tokens=90_000)

        # The job prompt MUST appear verbatim as its own user message.
        live_prompts = [
            m for m in out
            if m.get("role") == "user" and m.get("content") == job_prompt
        ]
        assert live_prompts, (
            "REGRESSION (#100818): the cron job prompt was swallowed by "
            "compaction and never re-appended — the model receives only the "
            "REFERENCE ONLY summary telling it to do nothing, and the run "
            f"delivers empty while logging success. Output roles: "
            f"{[m.get('role') for m in out]}"
        )

        # The recovered prompt OPENS the visible sequence (strict templates
        # require user-first) and the summary follows as assistant context.
        # Semantically the model sees its live task first; the handoff is
        # reference material behind it.
        roles_contents = [(m.get("role"), m.get("content")) for m in out]
        summary_idx = next(
            i for i, (r, t) in enumerate(roles_contents)
            if isinstance(t, str) and t.startswith(SUMMARY_PREFIX)
        )
        prompt_idx = next(
            i for i, (r, t) in enumerate(roles_contents) if t == job_prompt
        )
        # Both rows present; the prompt is the user-visible opener and the
        # summary follows as its assistant-role handoff.
        assert out[prompt_idx].get("role") == "user"
        assert out[summary_idx].get("role") == "assistant"

    def test_scheduler_sees_a_live_instruction_not_silent(self, compressor):
        """The compressed transcript the model actually receives must contain
        a non-summary, non-empty user message — the thing whose absence made
        17/60 production runs answer [SILENT]."""
        from agent.context_compressor import (
            SUMMARY_PREFIX,
            _content_text_for_contains,
        )

        c = compressor
        c.compression_count = 1
        messages = _cron_run("Check overnight pipeline status and summarize failures")

        mocked = f"{SUMMARY_PREFIX}\nsummary"
        with patch.object(c, "_generate_summary", return_value=mocked):
            out = c.compress(messages, current_tokens=90_000)

        live_user_turns = [
            m for m in out
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and not m.get("content").startswith(SUMMARY_PREFIX)
            and _content_text_for_contains(m.get("content")).strip()
        ]
        assert live_user_turns, (
            "no live user instruction survived compaction — the model will "
            "answer [SILENT] and the run still logs success (#100818)"
        )

    def test_preserved_tail_user_unaffected(self, compressor):
        """When a user turn survives in the tail (interactive shape), the
        recovery must NOT fire — no duplicate prompt appended."""
        from agent.context_compressor import SUMMARY_PREFIX

        c = compressor
        c.compression_count = 1
        c.tail_token_budget = 10
        messages = [{"role": "user", "content": "the original task"}]
        messages += _tool_turns(0, 10)
        # A live follow-up that survives in the tail.
        messages += [
            {"role": "user", "content": "actually, focus on the logs only"},
            {"role": "assistant", "content": "sure"},
        ]

        mocked = f"{SUMMARY_PREFIX}\nsummary body"
        with patch.object(c, "_generate_summary", return_value=mocked):
            out = c.compress(messages, current_tokens=90_000)

        # The ORIGINAL (compacted-away) prompt must NOT be re-appended next
        # to the live follow-up — that would resurrect a stale instruction.
        contents = [m.get("content") for m in out if isinstance(m.get("content"), str)]
        assert "the original task" not in contents, (
            "recovery fired although a live tail user turn existed — "
            "stale instruction resurrected beside the live one"
        )
        # The live follow-up must survive — either as its own row or inside
        # the #58753 merge-into-tail summary carrier (the alternation-safe
        # merge folds the summary into the tail user row; the live text
        # rides after the summary marker inside that merged content).
        joined = "\n".join(contents)
        assert "actually, focus on the logs only" in joined


class TestLatestRealUserTurnExtraction:
    """_latest_real_user_turn_text — the extraction helper."""

    def test_returns_newest_real_user_text(self):
        from agent.context_compressor import ContextCompressor

        msgs = [
            {"role": "user", "content": "first ask, already done"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "the active question"},
            {"role": "assistant", "content": "…"},
        ]
        assert (
            ContextCompressor._latest_real_user_turn_text(msgs)
            == "the active question"
        )

    def test_ignores_synthetic_summary_user_rows(self):
        from agent.context_compressor import (
            SUMMARY_PREFIX,
            ContextCompressor,
        )

        msgs = [
            {
                "role": "user",
                "content": f"{SUMMARY_PREFIX}\ntransport-shaped summary",
                "_compressed_summary": True,
            },
            {"role": "assistant", "content": "ok"},
        ]
        assert ContextCompressor._latest_real_user_turn_text(msgs) == ""

    def test_empty_when_no_user_turn(self):
        from agent.context_compressor import ContextCompressor

        assert ContextCompressor._latest_real_user_turn_text(_tool_turns(0, 4)) == ""
