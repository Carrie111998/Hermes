"""Tests for bounded tool-error logging (tools/tool_log.py + call sites).

Regression coverage for the bug where tool error handlers logged the full
upstream HTTP response body twice (message + traceback), ballooning
agent.log / errors.log / gateway.log to tens of megabytes on one failed call.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from tools.tool_log import LOG_ERROR_PREVIEW_LIMIT, truncate_for_log


class TestTruncateForLog:
    """Pure helper contract: bounded single-line log messages."""

    def test_short_text_passes_through_unchanged(self):
        msg = "Error analyzing image: boom"
        assert truncate_for_log(msg) == msg

    def test_exact_limit_is_unchanged(self):
        msg = "e" * LOG_ERROR_PREVIEW_LIMIT
        assert truncate_for_log(msg) == msg

    def test_long_text_is_bounded(self):
        msg = "e" * (LOG_ERROR_PREVIEW_LIMIT * 10)
        out = truncate_for_log(msg)
        assert len(out) == LOG_ERROR_PREVIEW_LIMIT + 1  # + ellipsis
        assert out == "e" * LOG_ERROR_PREVIEW_LIMIT + "…"

    def test_none_becomes_empty_string(self):
        assert truncate_for_log(None) == ""

    def test_multiline_error_collapses_to_single_line(self):
        msg = "line one\r\nline two\nline three\rline four"
        out = truncate_for_log(msg)
        assert "\n" not in out
        assert "\r" not in out
        assert out == "line one line two line three line four"

    def test_multiline_long_error_is_bounded_and_single_line(self):
        msg = ("line one\n" * (LOG_ERROR_PREVIEW_LIMIT // 10)) + "x" * (LOG_ERROR_PREVIEW_LIMIT * 2)
        out = truncate_for_log(msg)
        assert len(out) == LOG_ERROR_PREVIEW_LIMIT + 1  # + ellipsis
        assert "\n" not in out
        assert "\r" not in out
        assert out.endswith("…")


class TestVisionErrorLogBounded:
    """End-to-end: a failed vision call logs one bounded line, no traceback,
    while the full error still reaches the agent in the tool result."""

    @pytest.mark.asyncio
    async def test_error_log_is_bounded_but_result_keeps_full_body(self, tmp_path, caplog):
        from tools.vision_tools import vision_analyze_tool

        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

        huge_body = "upstream body " + "x" * 50_000
        api_error = Exception(f"HTTP 500: {huge_body}")

        with (
            patch(
                "tools.vision_tools._image_to_base64_data_url",
                return_value="data:image/png;base64,abc",
            ),
            patch(
                "tools.vision_tools.async_call_llm",
                new_callable=AsyncMock,
                side_effect=api_error,
            ),
            caplog.at_level(logging.ERROR, logger="tools.vision_tools"),
        ):
            result = json.loads(await vision_analyze_tool(str(img), "describe", "test/model"))

        assert result["success"] is False
        assert huge_body in result["error"]
        assert huge_body in result["analysis"]

        error_records = [
            r
            for r in caplog.records
            if r.name == "tools.vision_tools" and r.levelno == logging.ERROR
        ]
        assert error_records
        for rec in error_records:
            assert len(rec.getMessage()) <= LOG_ERROR_PREVIEW_LIMIT + 1
            assert rec.exc_info is None
