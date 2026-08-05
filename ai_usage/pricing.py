from __future__ import annotations

# Rate cards for log-billed, pay-as-you-go providers whose spend is ESTIMATED
# from token counts (state.db's estimated_cost_usd column is unpopulated — all
# rows read $0 — so the tray can't trust it and prices tokens itself).
#
# Gemini API list pricing, USD per 1,000,000 tokens, STANDARD tier only:
#   - prompts <=200k tokens (the >200k long-context tier costs ~2x in / 1.5x
#     out on Pro models; ignored here — this is a running estimate, not a bill);
#   - batch-mode half-price discount ignored likewise.
# As of 2026-08-05 (sources recorded in the spend-mode memory note). Ordered
# most-specific -> least; the FIRST needle found in the lowercased model name
# wins, so generation+tier entries must precede the bare-tier fallbacks, and
# "flash-lite" must precede "flash" (the latter is a substring of the former).
GEMINI_PRICING: list[tuple[str, float, float]] = [
    ("3.1-pro", 2.00, 12.00),
    ("3.6-flash", 1.50, 7.50),
    ("3.5-flash-lite", 0.30, 2.50),
    ("2.5-pro", 1.25, 10.00),
    ("2.5-flash-lite", 0.10, 0.40),
    ("2.5-flash", 0.30, 2.50),
    # generation-agnostic tier fallbacks (flash-lite before flash before pro).
    # Pinned to the CURRENT top generation per tier (flash=3.6, pro=3.1) so a
    # versionless model string (e.g. "gemini-flash-latest", "gemini-pro") is
    # never priced at a cheaper older-generation floor and can't under-report.
    ("flash-lite", 0.10, 0.40),
    ("flash", 1.50, 7.50),
    ("pro", 2.00, 12.00),
]

# Unknown Gemini model -> current top Pro-tier rates (3.1-pro): deliberately the
# priciest common tier so an unrecognized model never silently UNDER-reports.
GEMINI_DEFAULT: tuple[float, float] = (2.00, 12.00)


def gemini_rate(model: str) -> tuple[float, float]:
    """(input, output) $ per 1M tokens for a Gemini model name, by substring."""
    m = str(model or "").lower()
    for needle, in_rate, out_rate in GEMINI_PRICING:
        if needle in m:
            return in_rate, out_rate
    return GEMINI_DEFAULT


def cost_usd(key: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated $ for a provider/model/token-count triple.

    Dispatches on the canonical provider key. Only ``gemini`` has a rate card
    today; any other key returns 0.0 (spend mode is Gemini-only for now).
    """
    if key == "gemini":
        in_rate, out_rate = gemini_rate(model)
        return (int(input_tokens) * in_rate + int(output_tokens) * out_rate) / 1_000_000.0
    return 0.0
