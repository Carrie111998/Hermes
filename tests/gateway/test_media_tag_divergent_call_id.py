"""Regression tests: MEDIA-tag collection must resolve the producer tool
name through the same id/call_id policy as the rest of the codebase.

``_collect_auto_append_media_tags`` and ``_collect_history_media_paths``
(``gateway/run.py``) each built a private ``id`` (before ``call_id``)
producer-name map and looked it up via a tool result's ``tool_call_id`` —
which for a Codex/Responses-style tool call is populated from ``call_id``,
not ``id``. For a tool_call whose ``id`` ("fc_...") and ``call_id``
("call_...") diverge, the registration key and the lookup key never match,
so the producer-tool name is never resolved and a real TTS/image-generate
result is silently treated as an untrusted tool: its ``MEDIA:`` tag is
never auto-appended (data loss, no error surfaced) and its JSON-payload
path is never collected for history dedup.

The fix routes both functions through
``agent.message_sanitization.tool_call_id_variants`` /
``tool_result_id_variants`` — the single policy owner the rest of the
codebase (``agent/agent_runtime_helpers.py``, ``agent/conversation_loop.py``)
already consolidated behind for this exact divergent-id shape.
"""

from gateway.run import _collect_auto_append_media_tags, _collect_history_media_paths


def _divergent_id_tool_call(name: str) -> dict:
    """A Codex/Responses-shaped tool_call: ``id`` and ``call_id`` differ."""
    return {
        "id": "fc_abc123",
        "call_id": "call_xyz789",
        "function": {"name": name},
    }


class TestCollectAutoAppendMediaTagsDivergentId:
    def test_text_media_tag_survives_divergent_id(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [_divergent_id_tool_call("text_to_speech")],
            },
            {
                "role": "tool",
                "tool_call_id": "call_xyz789",
                "content": "MEDIA:/tmp/audio_output.mp3",
            },
        ]

        tags, _voice = _collect_auto_append_media_tags(messages, history_offset=0)

        assert tags == ["MEDIA:/tmp/audio_output.mp3"], (
            "auto-append lost the tag when the tool result paired on "
            "call_id while the registration map only trusted id"
        )

    def test_image_generate_json_payload_survives_divergent_id(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [_divergent_id_tool_call("image_generate")],
            },
            {
                "role": "tool",
                "tool_call_id": "call_xyz789",
                "content": '{"success": true, "image": "/tmp/gen/cat.png"}',
            },
        ]

        tags, _voice = _collect_auto_append_media_tags(messages, history_offset=0)

        assert tags == ["MEDIA:/tmp/gen/cat.png"], (
            "image_generate JSON-payload path was lost for a divergent "
            "id/call_id tool_call"
        )

    def test_matching_single_id_still_works(self):
        """Control: a tool_call with only one id spelling (the common,
        non-Codex shape) must keep working exactly as before."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "c1", "function": {"name": "text_to_speech"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "MEDIA:/tmp/single_id.mp3",
            },
        ]

        tags, _voice = _collect_auto_append_media_tags(messages, history_offset=0)

        assert tags == ["MEDIA:/tmp/single_id.mp3"]

    def test_untrusted_tool_still_filtered_with_divergent_id(self):
        """The producer-tool allowlist must still reject a non-eligible
        tool even when its id/call_id diverge — this fix must not widen
        the allowlist, only fix id resolution."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [_divergent_id_tool_call("execute_code")],
            },
            {
                "role": "tool",
                "tool_call_id": "call_xyz789",
                "content": "some log mentioning MEDIA:/etc/passwd as an example",
            },
        ]

        tags, _voice = _collect_auto_append_media_tags(messages, history_offset=0)

        assert tags == []


class TestCollectHistoryMediaPathsDivergentId:
    def test_image_generate_json_path_dedup_survives_divergent_id(self):
        history = [
            {
                "role": "assistant",
                "tool_calls": [_divergent_id_tool_call("image_generate")],
            },
            {
                "role": "tool",
                "tool_call_id": "call_xyz789",
                "content": '{"success": true, "image": "/tmp/gen/dog.png"}',
            },
        ]

        paths = _collect_history_media_paths(history)

        assert "/tmp/gen/dog.png" in paths, (
            "history-dedup path collection lost the image_generate JSON "
            "payload path for a divergent id/call_id tool_call, which "
            "would let the same file be re-delivered on a later turn"
        )

    def test_matching_single_id_still_works(self):
        history = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "c1", "function": {"name": "image_generate"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": '{"success": true, "image": "/tmp/single_id.png"}',
            },
        ]

        paths = _collect_history_media_paths(history)

        assert "/tmp/single_id.png" in paths
