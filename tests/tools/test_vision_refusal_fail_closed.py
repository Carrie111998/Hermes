"""The auxiliary vision path must fail closed on a non-analysis response.

A vision model that declines returns prose on the HTTP success path, so
nothing downstream could tell "I'm unable to describe the image" apart from a
real description — ``vision_analyze`` reported ``success: true`` either way.
Gates that require an actual visual read then pass on a picture nothing ever
looked at.

The refusal detector is deliberately conservative: a false positive blocks a
legitimate read, so the negative cases below matter as much as the positive
ones. In particular, "I can't see any banding in this crop" is a real finding
about the pixels, not a refusal to look at them.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.vision_tools import _is_vision_refusal, vision_analyze_tool


# The exact text the OpenAI auxiliary model returned on every cropped read.
_OBSERVED_REFUSAL = (
    "I'm unable to describe or analyze the specific content of the image. "
    "However, I can help you understand how to describe texture and edges in "
    "general.\n\nWhen discussing texture, consider the following aspects:\n\n"
    "1. **Surface Quality**: Is it smooth, rough, glossy, or matte?"
)


# ─── _is_vision_refusal ──────────────────────────────────────────────────────


class TestIsVisionRefusalPositive:
    def test_observed_production_refusal(self):
        assert _is_vision_refusal(_OBSERVED_REFUSAL) is True

    @pytest.mark.parametrize("text", [
        "I'm unable to analyze this image.",
        "I am not able to describe the image.",
        "I cannot process the image you provided.",
        "Sorry, but I can't describe the image.",
        "Unfortunately, I am unable to view the picture.",
        "I don't have the ability to see images.",
        "I can't interpret the cropped region of the image.",
        "No image was provided.",
        "There is no image attached to analyze.",
        "I don't see an image in your message.",
    ])
    def test_refusal_forms(self, text):
        assert _is_vision_refusal(text) is True

    def test_leading_whitespace_and_case(self):
        assert _is_vision_refusal("\n\n  I'M UNABLE TO ANALYZE THE IMAGE.  ") is True


class TestIsVisionRefusalNegative:
    @pytest.mark.parametrize("text", [
        # The whole point of a render-defect read: reporting absences.
        "I can't see any banding in this crop; the gradient is clean.",
        "I cannot make out fine grain within the image at this scale.",
        "There is no visible smearing in the image.",
        # Ordinary descriptions.
        "The image features a series of pyramidal shapes casting long shadows.",
        "A cropped dark mass sits low-left against a pale blue field.",
        # Describing an inability that belongs to the subject, not the model.
        "The figure appears unable to see past the doorway.",
        "The composition is unable to resolve into a single focal point.",
        # Hedged but substantive.
        "It is hard to describe the edge quality precisely, but the fibers "
        "read as torn rather than cut, with visible rag texture.",
    ])
    def test_not_a_refusal(self, text):
        assert _is_vision_refusal(text) is False

    def test_late_disclaimer_after_real_analysis_is_not_a_refusal(self):
        analysis = (
            "The crop shows a scorched-paper ground with three warm inclusions "
            "clustered low-right. Edge quality is soft but intact; no banding, "
            "no compression artifacting, no smearing along the torn seam. The "
            "rag fiber stays legible at full size. " + ("Grain is even. " * 20)
            + "I can't describe the image beyond this without a wider view."
        )
        assert _is_vision_refusal(analysis) is False

    @pytest.mark.parametrize("value", ["", "   ", None, 42, [], {}])
    def test_non_text_inputs(self, value):
        assert _is_vision_refusal(value) is False


# ─── vision_analyze_tool integration ─────────────────────────────────────────


def _response(content):
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = content
    mock_response.choices = [mock_choice]
    return mock_response


async def _run(tmp_path, content, **kwargs):
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    with (
        patch(
            "tools.vision_tools._image_to_base64_data_url",
            return_value="data:image/png;base64,abc",
        ),
        patch(
            "tools.vision_tools.async_call_llm",
            new_callable=AsyncMock,
            return_value=_response(content),
        ),
    ):
        return json.loads(
            await vision_analyze_tool(str(img), "describe this", "test/model", **kwargs)
        )


class TestAuxPathFailsClosed:
    @pytest.mark.asyncio
    async def test_refusal_is_an_explicit_failure(self, tmp_path):
        result = await _run(tmp_path, _OBSERVED_REFUSAL)
        assert result["success"] is False
        assert result["refusal"] is True
        assert "not analyzed" in result["error"]

    @pytest.mark.asyncio
    async def test_refusal_keeps_the_model_text_for_diagnosis(self, tmp_path):
        result = await _run(tmp_path, _OBSERVED_REFUSAL)
        assert "unable to describe" in result["analysis"]

    @pytest.mark.asyncio
    async def test_empty_response_is_an_explicit_failure(self, tmp_path):
        """Previously reported success with a canned "problem" string."""
        result = await _run(tmp_path, "")
        assert result["success"] is False
        assert result["refusal"] is True
        assert "empty response" in result["error"]

    @pytest.mark.asyncio
    async def test_real_analysis_still_succeeds(self, tmp_path):
        result = await _run(
            tmp_path,
            "Rough rag fiber, torn seam, no banding or smearing at full size.",
        )
        assert result["success"] is True
        assert "refusal" not in result
        assert "rag fiber" in result["analysis"]

    @pytest.mark.asyncio
    async def test_negative_finding_still_succeeds(self, tmp_path):
        """A clean technical read reports absences — it must not be swallowed."""
        result = await _run(
            tmp_path, "I can't see any banding or artifacting in this crop."
        )
        assert result["success"] is True
