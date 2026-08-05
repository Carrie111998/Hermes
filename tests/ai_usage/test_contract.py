from datetime import datetime, timezone
from ai_usage.contract import (
    PROVIDERS, WINDOW_LABEL_TO_ID, BILLING_PROVIDER_ALIASES, TOKEN_WINDOWS, iso,
)


def test_providers_grid_order_and_modes():
    keys = [p[0] for p in PROVIDERS]
    assert keys == ["anthropic", "openai-codex", "kimi", "gemini", "xai"]
    modes = {p[0]: p[2] for p in PROVIDERS}
    assert modes["anthropic"] == "budget" and modes["kimi"] == "budget"


def test_window_label_map_covers_both_providers():
    # Anthropic fetcher labels
    assert WINDOW_LABEL_TO_ID["Current session"] == ("5h", "5h")
    assert WINDOW_LABEL_TO_ID["Current week"][0] == "wk"
    # Codex fetcher labels
    assert WINDOW_LABEL_TO_ID["Session"][0] == "5h"
    assert WINDOW_LABEL_TO_ID["Weekly"][0] == "wk"


def test_token_windows_are_ordered_and_sized():
    assert [w[0] for w in TOKEN_WINDOWS] == ["5h", "24h", "7d"]
    assert dict((w[0], w[2]) for w in TOKEN_WINDOWS)["7d"] == 7 * 86400


def test_iso_is_utc_z():
    dt = datetime(2026, 8, 4, 15, 30, tzinfo=timezone.utc)
    assert iso(dt) == "2026-08-04T15:30:00Z"
