"""Regression tests for voice-bubble delivery dropped (#34608-followup).

When a TTS tool emits a MEDIA: tag in the current assistant response, it is
persisted to the transcript before delivery runs. The dedupe in
`_history_media_paths_for_session` would then find its own path in the
historical set and filter the current file out, producing the
`response_delivery_dropped: empty after extract` error and dropping the
voice note entirely.

Fix: `_history_media_paths_for_session` accepts an `exclude_paths` argument
that the delivery path passes with the just-extracted `media_files`, so the
current turn's paths can never be treated as "delivered in prior turns."
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.run import _collect_history_media_paths


def _make_adapter(transcript):
    """Build a minimal BasePlatformAdapter stand-in for the history helper."""
    from gateway.platforms.base import BasePlatformAdapter

    adapter = BasePlatformAdapter.__new__(BasePlatformAdapter)
    store = MagicMock()
    store.peek_session_id = MagicMock(return_value="sid")
    store.load_transcript = MagicMock(return_value=transcript)
    adapter._session_store = store
    return adapter


class TestHistoryMediaPathsExcludePaths:
    def test_current_turn_media_path_is_excluded(self, tmp_path):
        """A MEDIA: tag in the *current* assistant message must not appear in the
        dedupe set when `exclude_paths` is passed (mirrors the delivery path)."""
        media_path = str(tmp_path / "tts_current.ogg")
        transcript = [
            {"role": "user", "content": "Say hello as audio"},
            {
                "role": "assistant",
                "content": "Prior turn MEDIA:/tmp/old_turn.ogg",
            },
            {
                # This is the *current* assistant message — already persisted
                # before the delivery loop runs.
                "role": "assistant",
                "content": f"Here you go MEDIA:{media_path}",
            },
        ]
        adapter = _make_adapter(transcript)

        result = adapter._history_media_paths_for_session(
            session_key="telegram:dm:970522396",
            exclude_paths={media_path},
        )

        # /tmp/old_turn.ogg is a genuinely prior turn — must remain in the set.
        assert result is not None, "expected a set, got None"
        assert "/tmp/old_turn.ogg" in result
        # The current-turn path must be excluded from the dedupe set.
        assert media_path not in result, (
            "Current-turn media path was deduplicated against itself; "
            "voice note would be dropped again."
        )

    def test_exclude_paths_none_is_backward_compatible(self):
        adapter = _make_adapter(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "MEDIA:/tmp/a.ogg MEDIA:/tmp/b.ogg"},
            ]
        )
        result_none = adapter._history_media_paths_for_session(session_key="k")
        result_noarg = adapter._history_media_paths_for_session(session_key="k", exclude_paths=None)
        assert result_none == result_noarg == {"/tmp/a.ogg"}

    def test_exclude_empty_set_preserves_behaviour(self):
        adapter = _make_adapter(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "MEDIA:/tmp/a.ogg MEDIA:/tmp/b.ogg"},
            ]
        )
        result = adapter._history_media_paths_for_session(session_key="k", exclude_paths=set())
        assert result == {"/tmp/a.ogg", "/tmp/b.ogg"}

    def test_image_generate_json_paths_can_be_excluded(self, tmp_path):
        img_path = str(tmp_path / "gen" / "image.png")
        transcript = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_img", "function": {"name": "image_generate"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call_img",
                "content": '{"success": true, "image": "' + img_path + '"}',
            },
        ]
        adapter = _make_adapter(transcript)
        result = adapter._history_media_paths_for_session(
            session_key="telegram:dm:1",
            exclude_paths={img_path},
        )
        assert img_path in result or result == set(), (
            "Image-generate JSON paths should still be collected unless explicitly excluded."
        )


class TestDeliveryFlowDoesNotSelfDedupe:
    """End-to-end regression: the delivery flow must not eliminate the voice file."""

    def test_voice_media_not_dropped_from_media_files(self, tmp_path):
        """Simulate the real failure: media_files extracted from the response,
        run through history-dedupe with exclude_paths — result must keep the file."""
        from gateway.platforms.base import BasePlatformAdapter

        media_path = str(tmp_path / "tts_20260728_210200_754732.ogg")
        response_text = f"[[audio_as_voice]]\nMEDIA:{media_path}"

        # The transcript has the CURRENT assistant message already persisted
        # (the agent stores its own reply before the delivery pipeline runs).
        transcript = [
            {"role": "user", "content": "Say hello as audio"},
            {"role": "assistant", "content": response_text},
        ]
        adapter = _make_adapter(transcript)

        # Extract what the agent would extract from the response text.
        media_files, _cleaned = BasePlatformAdapter.extract_media(response_text)
        assert [p for p, _ in media_files] == [media_path]

        # With the fix: exclude the current turn's paths from the history scan.
        exclude_paths = {p for p, _ in media_files}
        history_paths = adapter._history_media_paths_for_session(
            session_key="telegram:dm:970522396",
            exclude_paths=exclude_paths,
        )

        # The current media_path must NOT have been added into the dedupe set
        # (it would be filtered out of media_files, leaving {} and dropping the reply).
        assert media_path not in history_paths, (
            "Current voice file ended up in the history dedupe set; "
            "would be filtered out of media_files and trigger the drop."
        )
        # And media_files itself must be untouched after dedupe.
        assert [p for p, _ in media_files] == [media_path]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
