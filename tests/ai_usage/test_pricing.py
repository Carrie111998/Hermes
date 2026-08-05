from ai_usage.pricing import cost_usd, gemini_rate


def test_generation_and_tier_specific_rates_win():
    assert gemini_rate("gemini-2.5-pro") == (1.25, 10.00)
    assert gemini_rate("gemini-2.5-flash") == (0.30, 2.50)
    assert gemini_rate("gemini-2.5-flash-lite") == (0.10, 0.40)
    assert gemini_rate("gemini-3.1-pro") == (2.00, 12.00)
    assert gemini_rate("gemini-3.6-flash") == (1.50, 7.50)


def test_flash_lite_precedes_flash_substring():
    # "flash" is a substring of "flash-lite"; the lite rate must still win.
    assert gemini_rate("some-future-flash-lite") == (0.10, 0.40)
    assert gemini_rate("some-future-flash") == (0.30, 2.50)


def test_model_prefixes_and_case_are_tolerated():
    assert gemini_rate("models/Gemini-2.5-Flash") == (0.30, 2.50)


def test_unknown_gemini_model_falls_back_to_pro_tier():
    # Deliberately the priciest common tier — never under-report.
    assert gemini_rate("gemini-experimental-xyz") == (1.25, 10.00)
    assert gemini_rate("") == (1.25, 10.00)


def test_cost_usd_prices_input_and_output_separately():
    # 0.2M input @ $1.25 + 0.1M output @ $10.00 = 0.25 + 1.00 = $1.25
    assert cost_usd("gemini", "gemini-2.5-pro", 200_000, 100_000) == 1.25
    # 1M in + 1M out on Flash = 0.30 + 2.50 = $2.80
    assert round(cost_usd("gemini", "gemini-2.5-flash", 1_000_000, 1_000_000), 2) == 2.80


def test_cost_usd_is_zero_for_non_gemini_keys():
    assert cost_usd("xai", "grok-4", 1_000_000, 1_000_000) == 0.0
