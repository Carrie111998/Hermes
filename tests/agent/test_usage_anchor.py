"""Usage-anchored context accounting (agent/model_metadata.py).

Context-size checks anchor on the provider-reported ``usage.prompt_tokens``
of the last main-loop response and estimate ONLY the messages appended
since. These tests cover:

  * anchor + delta arithmetic (exact base, small estimated delta);
  * the image-heavy divergence the anchor eliminates (flat 1500/image
    heuristic vs provider truth);
  * fallback to full estimation when no anchor exists (first request,
    usage-less providers);
  * invalidation when compaction rewrites the transcript (structural
    id/index check fails closed) and on explicit reset sites;
  * the preflight consumer (_preflight_request_tokens) preferring the
    anchor, plus a sabotage check proving the anchored path (not the
    heuristic) produces the number;
  * persist/restore via a content fingerprint so a restarted agent_init
    can reuse provider usage when the transcript is unchanged, and reject
    restore when the last message or history length no longer matches.
"""

import json
from types import SimpleNamespace

import pytest

from agent.model_metadata import (
    USAGE_ANCHOR_MODEL_CONFIG_KEY,
    anchored_context_tokens,
    capture_usage_anchor,
    estimate_messages_tokens_rough,
    init_agent_usage_anchor,
    persist_usage_anchor,
    restore_usage_anchor,
    serialize_usage_anchor,
    usage_anchor_message_fingerprint,
)
from agent.turn_context import _preflight_request_tokens


def _msg(role, content):
    return {"role": role, "content": content}


def _image_msg():
    # ~40KB of fake base64 — the rough estimator charges a flat 1500
    # tokens per image part regardless of true provider accounting.
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + "A" * 40000},
            },
        ],
    }


def _history_with_images(n_images=10):
    msgs = [_msg("user", "start")]
    for i in range(n_images):
        msgs.append(_msg("assistant", f"taking screenshot {i}"))
        msgs.append(_image_msg())
    msgs.append(_msg("assistant", "done looking"))
    return msgs


class TestAnchorArithmetic:
    def test_anchor_plus_small_delta(self):
        messages = _history_with_images(10)
        anchor = capture_usage_anchor(50_000, 250, messages)
        assert anchor is not None
        assert anchor["prompt_tokens"] == 50_000
        assert anchor["base_count"] == len(messages)

        # Main loop appends the response's own assistant reply, then a tool
        # result / user follow-up.
        messages.append(_msg("assistant", "the anchored reply itself"))
        messages.append(_msg("user", "short follow-up"))

        anchored = anchored_context_tokens(messages, anchor)
        assert anchored is not None
        # Exact base + completion; the assistant reply at base_count is
        # covered by completion_tokens, so only the follow-up is estimated.
        delta_est = estimate_messages_tokens_rough([messages[-1]])
        assert anchored == 50_000 + 250 + delta_est
        assert delta_est < 50  # the estimated window is one small message

    def test_image_heavy_divergence_eliminated(self):
        messages = _history_with_images(10)
        # Provider ground truth: say the real prompt was 12,000 tokens
        # (providers often charge far less than 1500/image, or the images
        # were downscaled). The heuristic charges 10 * 1500 + text.
        anchor = capture_usage_anchor(12_000, 100, messages)
        messages.append(_msg("assistant", "reply"))
        messages.append(_msg("user", "ok"))

        rough = estimate_messages_tokens_rough(messages)
        anchored = anchored_context_tokens(messages, anchor)
        assert rough >= 15_000  # flat 1500 x 10 images dominates
        assert anchored is not None
        assert anchored < 12_200
        # The whole-history heuristic diverges by thousands of tokens;
        # the anchored figure is provider truth + a tiny delta.
        assert rough - anchored > 2_800

    def test_no_usage_returns_none(self):
        messages = [_msg("user", "hi")]
        assert capture_usage_anchor(0, 0, messages) is None
        assert capture_usage_anchor(None, None, messages) is None
        assert capture_usage_anchor("garbage", 1, messages) is None

    def test_missing_anchor_falls_back(self):
        messages = _history_with_images(2)
        assert anchored_context_tokens(messages, None) is None


class TestAnchorInvalidation:
    def test_compaction_rewrite_fails_closed(self):
        messages = _history_with_images(4)
        anchor = capture_usage_anchor(30_000, 50, messages)
        # Compaction: transcript rebuilt as a new, shorter list.
        compacted = [
            _msg("user", "summary handoff"),
            _msg("assistant", "[compressed summary]"),
        ]
        assert anchored_context_tokens(compacted, anchor) is None

    def test_middle_splice_shifts_base_and_fails_closed(self):
        messages = _history_with_images(4)
        anchor = capture_usage_anchor(30_000, 50, messages)
        # Micro-compact style splice: middle window replaced by one marker.
        spliced = messages[:1] + [_msg("assistant", "[marker]")] + messages[5:]
        assert anchored_context_tokens(spliced, anchor) is None

    def test_same_length_different_objects_fails_closed(self):
        messages = _history_with_images(4)
        anchor = capture_usage_anchor(30_000, 50, messages)
        rebuilt = [dict(m) for m in messages]  # fresh dicts, same values
        assert anchored_context_tokens(rebuilt, anchor) is None

    def test_explicit_invalidation_sites(self):
        """The compaction + session-reset sites null agent._usage_anchor."""
        import inspect

        import agent.conversation_compression as cc
        import agent.codex_runtime as cr
        import run_agent

        assert "agent._usage_anchor = None" in inspect.getsource(cc)
        assert "agent._usage_anchor = None" in inspect.getsource(cr)
        assert "self._usage_anchor = None" in inspect.getsource(
            run_agent.AIAgent.reset_session_state
        )


class TestPreflightConsumer:
    def _agent(self, anchor):
        return SimpleNamespace(
            _usage_anchor=anchor,
            tools=None,
            api_mode="",
            provider="openai",
        )

    def test_preflight_prefers_anchor(self):
        messages = _history_with_images(10)
        anchor = capture_usage_anchor(50_000, 250, messages)
        messages.append(_msg("assistant", "reply"))
        messages.append(_msg("user", "ok"))
        agent = self._agent(anchor)

        got = _preflight_request_tokens(agent, messages, "SYSTEM PROMPT " * 500)
        expected = anchored_context_tokens(messages, anchor)
        assert got == expected
        # The anchored figure ignores the (already-counted) system prompt
        # text passed in — provider usage includes the real one.
        assert 50_000 < got < 50_500

    def test_preflight_falls_back_without_anchor(self):
        messages = _history_with_images(3)
        agent = self._agent(None)
        got = _preflight_request_tokens(agent, messages, "sys")
        # Pure heuristic: flat image cost dominates.
        assert got >= 4_500

    def test_sabotage_disabling_anchor_changes_result(self):
        """Prove the anchored path produced the number: with the anchor
        removed (the sabotage), the same inputs yield the heuristic figure,
        which diverges by thousands of tokens on an image-heavy history."""
        messages = _history_with_images(10)
        anchor = capture_usage_anchor(12_000, 100, messages)
        messages.append(_msg("assistant", "reply"))
        messages.append(_msg("user", "ok"))

        anchored_result = _preflight_request_tokens(
            self._agent(anchor), messages, ""
        )
        sabotaged_result = _preflight_request_tokens(
            self._agent(None), messages, ""
        )
        assert sabotaged_result - anchored_result > 2_800


class TestCompressionTriggerUsesAnchor:
    def test_threshold_decision_flips_with_anchor(self):
        """An image-heavy history the heuristic pushes over a 15K threshold
        stays under it when the provider reports the real 12K prompt."""
        messages = _history_with_images(10)
        anchor = capture_usage_anchor(12_000, 100, messages)
        messages.append(_msg("assistant", "reply"))

        threshold = 15_000
        heuristic = estimate_messages_tokens_rough(messages)
        anchored = anchored_context_tokens(messages, anchor)
        assert heuristic >= threshold  # old behavior: spurious compression
        assert anchored is not None and anchored < threshold


def _plain_history():
    return [
        _msg("user", "start"),
        _msg("assistant", "hello"),
        _msg("user", "do the thing"),
        _msg("assistant", "done looking"),
    ]


def _namespace_agent(session_id, db):
    return SimpleNamespace(
        session_id=session_id,
        _session_db=db,
        _persist_disabled=False,
        _usage_anchor=None,
    )


class TestUsageAnchorPersist:
    def test_capture_includes_content_fingerprint(self):
        messages = _plain_history()
        anchor = capture_usage_anchor(10_000, 20, messages)
        assert anchor is not None
        assert isinstance(anchor["base_last_fp"], str)
        assert len(anchor["base_last_fp"]) == 64
        assert anchor["base_last_fp"] == usage_anchor_message_fingerprint(messages[-1])
        assert anchor["base_last_id"] == id(messages[-1])

    def test_serialize_drops_process_local_id(self):
        messages = _history_with_images(2)
        captured = capture_usage_anchor(12_000, 100, messages)
        blob = serialize_usage_anchor(captured)
        assert blob is not None
        assert "base_last_id" not in blob
        assert blob["base_last_fp"] == captured["base_last_fp"]
        assert blob["prompt_tokens"] == 12_000
        restored = restore_usage_anchor(blob)
        assert restored is not None
        assert restored["base_last_id"] is None

    def test_restore_strips_stale_id_from_blob(self):
        messages = _plain_history()
        captured = capture_usage_anchor(8_000, 40, messages)
        blob = serialize_usage_anchor(captured)
        blob["base_last_id"] = 123456789
        restored = restore_usage_anchor(blob)
        assert restored["base_last_id"] is None
        rebuilt = [dict(m) for m in messages]
        assert anchored_context_tokens(rebuilt, restored) == 8_000 + 40

    def test_round_trip_new_objects_returns_provider_total(self):
        messages = _history_with_images(4)
        captured = capture_usage_anchor(30_000, 50, messages)
        restored = restore_usage_anchor(serialize_usage_anchor(captured))
        rebuilt = [dict(m) for m in messages]
        assert anchored_context_tokens(rebuilt, restored) == 30_000 + 50
        # In-process capture still requires id() — compaction-style rebuilds
        # of the same values must not suppress compression in-process.
        assert anchored_context_tokens(rebuilt, captured) is None

    def test_restore_rejected_on_last_message_change(self):
        messages = _history_with_images(4)
        captured = capture_usage_anchor(30_000, 50, messages)
        restored = restore_usage_anchor(serialize_usage_anchor(captured))
        changed = [dict(m) for m in messages]
        changed[-1] = _msg("assistant", "different last message")
        assert anchored_context_tokens(changed, restored) is None

    def test_restore_rejected_on_shorter_history(self):
        messages = _history_with_images(4)
        captured = capture_usage_anchor(30_000, 50, messages)
        restored = restore_usage_anchor(serialize_usage_anchor(captured))
        assert anchored_context_tokens(messages[:2], restored) is None

    def test_sessiondb_round_trip_via_agent_init(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        sid = "usage-anchor-roundtrip"
        messages = _plain_history()
        db.create_session(sid, source="cli")
        db.append_messages_batch(sid, messages)

        captured = capture_usage_anchor(40_000, 120, messages)
        persist_usage_anchor(_namespace_agent(sid, db), captured)
        stored = json.loads(db.get_session(sid)["model_config"] or "{}")
        assert USAGE_ANCHOR_MODEL_CONFIG_KEY in stored
        assert "base_last_id" not in stored[USAGE_ANCHOR_MODEL_CONFIG_KEY]

        resumed = _namespace_agent(sid, db)
        init_agent_usage_anchor(resumed)
        rebuilt = [dict(m) for m in messages]
        rebuilt.append(_msg("user", "next turn"))
        got = anchored_context_tokens(rebuilt, resumed._usage_anchor)
        assert got is not None
        delta = estimate_messages_tokens_rough([rebuilt[-1]])
        assert got == 40_000 + 120 + delta

    def test_sessiondb_mismatch_last_message_rejected(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        sid = "usage-anchor-mismatch"
        original = _plain_history()
        db.create_session(sid, source="cli")
        captured = capture_usage_anchor(10_000, 10, original)
        persist_usage_anchor(_namespace_agent(sid, db), captured)

        changed = [dict(m) for m in original]
        changed[-1] = _msg("assistant", "CHANGED")
        db.append_messages_batch(sid, changed)

        resumed = _namespace_agent(sid, db)
        init_agent_usage_anchor(resumed)
        assert resumed._usage_anchor is None
        stored = json.loads(db.get_session(sid)["model_config"] or "{}")
        assert USAGE_ANCHOR_MODEL_CONFIG_KEY not in stored

    def test_sessiondb_shorter_history_rejected(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        sid = "usage-anchor-short"
        original = _plain_history()
        db.create_session(sid, source="cli")
        db.append_messages_batch(sid, original[:1])
        captured = capture_usage_anchor(10_000, 10, original)
        persist_usage_anchor(_namespace_agent(sid, db), captured)

        resumed = _namespace_agent(sid, db)
        init_agent_usage_anchor(resumed)
        assert resumed._usage_anchor is None

    def test_fresh_agent_init_restores_anchor(self, tmp_path, monkeypatch):
        """A new AIAgent for the same session_id restores the persisted blob."""
        import os
        from unittest.mock import patch

        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        sid = "USAGE_ANCHOR_AGENT_INIT"
        messages = _plain_history()
        db.create_session(sid, source="cli")
        db.append_messages_batch(sid, messages)
        persist_usage_anchor(
            _namespace_agent(sid, db),
            capture_usage_anchor(22_000, 80, messages),
        )

        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent

            resumed = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=db,
                session_id=sid,
                platform="cli",
                skip_context_files=True,
                skip_memory=True,
            )
        rebuilt = [dict(m) for m in messages]
        assert anchored_context_tokens(rebuilt, resumed._usage_anchor) == 22_000 + 80


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
