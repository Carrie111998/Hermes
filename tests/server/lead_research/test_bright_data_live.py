"""Opt-in Bright Data credential smoke test.

The default pytest marker expression excludes this file. When explicitly
selected, it still performs exactly one Web Unlocker request.
"""
from __future__ import annotations

import os

import httpx
import pytest

from server.config import Settings
from server.lead_research.providers.bright_data import BrightDataVerifier


pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.environ.get("BRIGHTDATA_API_KEY"),
    reason="BRIGHTDATA_API_KEY is required for the live Web Unlocker smoke test",
)
def test_bright_data_web_unlocker_makes_one_bounded_request():
    settings = Settings.load()
    with httpx.Client() as client:
        verifier = BrightDataVerifier(
            os.environ["BRIGHTDATA_API_KEY"],
            settings.brightdata_unlocker_zone,
            client,
        )
        markdown = verifier._fetch_markdown("https://example.com")

    assert markdown.strip()
