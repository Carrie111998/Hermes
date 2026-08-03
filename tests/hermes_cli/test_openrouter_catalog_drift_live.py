"""Live check that every curated OpenRouter model is still real and tool-capable.

Opt-in only::

    HERMES_LIVE_TESTS=1 OPENROUTER_API_KEY=sk-or-... \\
        pytest tests/hermes_cli/test_openrouter_catalog_drift_live.py -q

``OPENROUTER_MODELS`` is hand-maintained, and providers retire ids, rename
generations and withdraw ``:free`` tiers without telling us. Nothing in the
suite noticed, because every other model test asserts against the list itself
rather than against what OpenRouter actually serves. That makes the list
self-consistent and quietly wrong.

Two distinct failures matter, and the second is the worse one:

* A retired id fails loudly the moment a user picks it.
* An id that is still served but has dropped ``tools`` from
  ``supported_parameters`` keeps working — right up until the agent needs to
  call a tool. In an agent framework that is a silent trap, not an error.

Live and opt-in on purpose. This asserts against a third-party catalogue that
changes without us, so it must never be able to redden CI on someone else's
deploy; it is a maintenance check a maintainer runs deliberately, in the same
shape as the other ``*_live.py`` tests here.
"""

from __future__ import annotations

import os

import pytest

from hermes_cli.models import OPENROUTER_MODELS

LIVE = os.environ.get("HERMES_LIVE_TESTS") == "1"
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")

pytestmark = [
    pytest.mark.skipif(not LIVE, reason="set HERMES_LIVE_TESTS=1 to run live catalogue checks"),
    pytest.mark.skipif(not OPENROUTER_KEY, reason="OPENROUTER_API_KEY not set"),
]

MODELS_URL = "https://openrouter.ai/api/v1/models"


@pytest.fixture(scope="module")
def live_catalog() -> dict[str, dict]:
    """Every model OpenRouter currently serves, keyed by id."""
    import json
    import urllib.request

    request = urllib.request.Request(
        MODELS_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "User-Agent": "hermes-cli-catalog-drift-check",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    catalog = {entry["id"]: entry for entry in payload.get("data", [])}
    assert catalog, "OpenRouter returned an empty catalogue; refusing to judge drift from it"
    return catalog


def test_every_curated_model_is_still_served(live_catalog: dict[str, dict]) -> None:
    retired = [model_id for model_id, _ in OPENROUTER_MODELS if model_id not in live_catalog]
    assert not retired, (
        "OPENROUTER_MODELS lists ids OpenRouter no longer serves; a user picking one gets a "
        f"failure: {retired}"
    )


def test_every_curated_model_still_advertises_tools(live_catalog: dict[str, dict]) -> None:
    toolless = [
        model_id
        for model_id, _ in OPENROUTER_MODELS
        if model_id in live_catalog
        and "tools" not in (live_catalog[model_id].get("supported_parameters") or [])
    ]
    assert not toolless, (
        "OPENROUTER_MODELS lists ids that no longer advertise tool calling. These do not fail on "
        f"selection — they fail on the first tool call: {toolless}"
    )
