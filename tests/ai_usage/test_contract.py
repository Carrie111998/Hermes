from datetime import datetime, timezone
from ai_usage.contract import (
    PROVIDERS, WINDOW_LABEL_TO_ID, TOKEN_WINDOWS, iso,
)


def test_providers_grid_order_and_modes():
    keys = [p[0] for p in PROVIDERS]
    assert keys == [
        "anthropic", "anthropic2", "openai-codex", "kimi", "deepseek", "gemini",
        "xai", "opencode-go",
    ]
    modes = {p[0]: p[2] for p in PROVIDERS}
    assert modes["anthropic"] == "budget" and modes["kimi"] == "budget"
    # Second Anthropic subscription (diegodearagaous@gmail.com) via its own
    # ANTHROPIC2_OAUTH_TOKEN; same oauth usage endpoint, same window labels.
    assert modes["anthropic2"] == "budget"
    assert modes["deepseek"] == "balance"  # pay-as-you-go outstanding-$
    assert modes["gemini"] == "spend"  # month-to-date estimated-$ from tokens
    # Grok: grok.com web-session scrape over CDP (agent/grok_session.py);
    # api.x.ai has no usage endpoint.
    assert modes["xai"] == "budget"
    # OpenCode Go: official GET /zen/go/v1/usage endpoint (Bearer
    # OPENCODE_GO_API_KEY) -> rolling/weekly/monthly % windows, like Codex.
    assert modes["opencode-go"] == "budget"


def test_window_label_map_covers_both_providers():
    # Anthropic fetcher labels
    assert WINDOW_LABEL_TO_ID["Current session"] == ("5h", "5h")
    assert WINDOW_LABEL_TO_ID["Current week"][0] == "wk"
    # Codex fetcher labels
    assert WINDOW_LABEL_TO_ID["Session"][0] == "5h"
    assert WINDOW_LABEL_TO_ID["Weekly"][0] == "wk"
    # OpenCode Go fetcher labels
    assert WINDOW_LABEL_TO_ID["Rolling"] == ("5h", "Rolling")
    assert WINDOW_LABEL_TO_ID["Monthly"] == ("mo", "Monthly")
    # Grok CDP scrape label
    assert WINDOW_LABEL_TO_ID["Grok window"] == ("5h", "Grok")


def test_token_windows_are_ordered_and_sized():
    assert [w[0] for w in TOKEN_WINDOWS] == ["5h", "24h", "7d"]
    assert dict((w[0], w[2]) for w in TOKEN_WINDOWS)["7d"] == 7 * 86400


def test_iso_is_utc_z():
    dt = datetime(2026, 8, 4, 15, 30, tzinfo=timezone.utc)
    assert iso(dt) == "2026-08-04T15:30:00Z"
