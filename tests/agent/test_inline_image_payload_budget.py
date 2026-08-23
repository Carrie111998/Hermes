"""Regression tests for byte-bounded native vision history.

The model-token estimator intentionally prices images with a model-oriented
heuristic. These tests cover the separate wire-byte metric and the request /
compression projection that prevents inline base64 from defeating compaction.
"""

from __future__ import annotations

from agent.context_compressor import (
    _bound_inline_image_payloads,
    _content_has_images,
    _strip_historical_media,
)
from agent.model_metadata import (
    estimate_messages_inline_image_bytes,
    estimate_messages_tokens_rough,
)


_PLACEHOLDER_MARKER = "image omitted"


def _image_part(size: int, *, part_type: str = "image_url") -> dict:
    url = "data:image/png;base64," + ("A" * size)
    if part_type == "input_image":
        return {"type": part_type, "image_url": url}
    return {"type": part_type, "image_url": {"url": url}}


def _tool_message(call_id: str, size: int, *, part_type: str = "image_url") -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": [
            {"type": "text", "text": f"result {call_id}"},
            _image_part(size, part_type=part_type),
        ],
    }


def _user_message(size: int) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "inspect the image"},
            _image_part(size),
        ],
    }


class TestInlineImageByteMetric:
    def test_wire_metric_scales_with_inline_payload_but_model_estimate_does_not(self):
        small = [_tool_message("small", 1_000)]
        huge = [_tool_message("huge", 3_000_000)]

        small_bytes = estimate_messages_inline_image_bytes(small)
        huge_bytes = estimate_messages_inline_image_bytes(huge)
        small_tokens = estimate_messages_tokens_rough(small)
        huge_tokens = estimate_messages_tokens_rough(huge)

        assert huge_bytes > small_bytes * 2_000
        # The flat model-oriented estimate is intentionally not the wire metric.
        assert huge_tokens < small_tokens * 2

    def test_remote_urls_are_not_counted_as_inline_payload(self):
        messages = [{
            "role": "tool",
            "content": [{
                "type": "image_url",
                "image_url": {"url": "https://example.test/image.png"},
            }],
        }]
        assert estimate_messages_inline_image_bytes(messages) == 0

    def test_native_multimodal_envelope_is_measured(self):
        messages = [{
            "role": "tool",
            "content": {
                "_multimodal": True,
                "content": [{"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + ("A" * 1_000)
                }}],
            },
        }]
        assert estimate_messages_inline_image_bytes(messages) > 1_000

    def test_anthropic_content_sidecar_is_measured(self):
        messages = [{
            "role": "tool",
            "content": "image result",
            "_anthropic_content_blocks": [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "A" * 1_000,
                },
            }],
        }]
        assert estimate_messages_inline_image_bytes(messages) == 1_000


class TestRequestProjection:
    def test_complete_active_tool_exchange_is_preserved_when_it_fits(self):
        messages = [
            {"role": "user", "content": "inspect both"},
            _tool_message("first", 200_000),
            _tool_message("second", 200_000),
        ]

        projected = _bound_inline_image_payloads(messages, max_inline_bytes=512_000)

        assert _content_has_images(projected[1]["content"])
        assert _content_has_images(projected[2]["content"])

    def test_aggregate_budget_preserves_latest_user_and_tool_images(self):
        messages = [
            _user_message(200_000),
            _tool_message("old", 200_000),
            _tool_message("new", 200_000),
        ]

        projected = _bound_inline_image_payloads(
            messages,
            max_inline_bytes=450_000,
        )

        assert _content_has_images(projected[0]["content"])
        assert not _content_has_images(projected[1]["content"])
        assert _content_has_images(projected[2]["content"])
        assert estimate_messages_inline_image_bytes(projected) <= 450_000

    def test_projection_is_copy_on_write_and_drops_stale_sidecar(self):
        messages = [
            _user_message(200_000),
            _tool_message("new", 200_000),
            _tool_message("newest", 200_000),
        ]
        messages[1]["api_content"] = "stale multimodal sidecar"
        original_url = messages[0]["content"][1]["image_url"]["url"]

        projected = _bound_inline_image_payloads(
            messages,
            max_inline_bytes=450_000,
        )

        assert projected is not messages
        assert "api_content" in messages[1]
        assert "api_content" not in projected[1]
        assert messages[0]["content"][1]["image_url"]["url"] == original_url

    def test_responses_input_image_gets_input_text_placeholder(self):
        messages = [
            _tool_message("old", 300_000, part_type="input_image"),
            _tool_message("new", 10_000, part_type="input_image"),
        ]

        projected = _bound_inline_image_payloads(
            messages,
            max_inline_bytes=100_000,
        )

        old_parts = projected[0]["content"]
        assert any(
            part.get("type") == "input_text"
            and _PLACEHOLDER_MARKER in part.get("text", "")
            for part in old_parts
        )
        assert _content_has_images(projected[1]["content"])

    def test_anthropic_content_sidecar_is_projected_copy_on_write(self):
        sidecar = [{
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "A" * 300_000,
            },
        }]
        messages = [
            {
                "role": "tool",
                "content": "image result",
                "_anthropic_content_blocks": sidecar,
            },
            {"role": "user", "content": "follow-up"},
        ]

        projected = _bound_inline_image_payloads(
            messages,
            max_inline_bytes=100_000,
        )

        assert projected is not messages
        assert messages[0]["_anthropic_content_blocks"] is sidecar
        assert projected[0]["_anthropic_content_blocks"][0]["type"] == "text"
        assert "image omitted" in projected[0]["_anthropic_content_blocks"][0]["text"]

    def test_tool_image_ages_out_after_a_text_follow_up(self):
        messages = [
            {"role": "user", "content": "first question"},
            _tool_message("old", 300_000),
            {"role": "user", "content": "follow-up question"},
        ]

        projected = _bound_inline_image_payloads(messages, max_inline_bytes=256_000)

        assert not _content_has_images(projected[1]["content"])
        assert "follow-up question" in projected[2]["content"]


class TestCompressionProjection:
    def test_historical_media_prunes_tool_images_inside_protected_tail(self):
        messages = [
            {"role": "user", "content": "look"},
            _tool_message("a", 3_000_000),
            _tool_message("b", 3_000_000),
            _tool_message("c", 3_000_000),
        ]

        compressed = _strip_historical_media(messages)

        assert not _content_has_images(compressed[1]["content"])
        assert not _content_has_images(compressed[2]["content"])
        assert _content_has_images(compressed[3]["content"])

    def test_historical_media_prunes_old_remote_image_parts(self):
        remote = {
            "type": "image_url",
            "image_url": {"url": "https://example.test/shot.png"},
        }
        messages = [
            {"role": "user", "content": "look"},
            {"role": "tool", "content": [{"type": "text", "text": "old"}, remote]},
            {"role": "tool", "content": [{"type": "text", "text": "new"}, remote]},
        ]

        compressed = _strip_historical_media(messages)

        assert not _content_has_images(compressed[1]["content"])
        assert _content_has_images(compressed[2]["content"])

    def test_first_user_image_does_not_block_tool_image_pruning(self):
        messages = [
            _user_message(100_000),
            _tool_message("a", 100_000),
            _tool_message("b", 100_000),
        ]

        compressed = _strip_historical_media(messages)

        assert _content_has_images(compressed[0]["content"])
        assert not _content_has_images(compressed[1]["content"])
        assert _content_has_images(compressed[2]["content"])
