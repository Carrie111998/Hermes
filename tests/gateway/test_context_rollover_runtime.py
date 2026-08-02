"""Runtime measurements used by model-aware context rollover."""

from gateway.run import _usable_input_budget


def test_usable_input_budget_reserves_output_tokens():
    assert _usable_input_budget(500_000, 32_000) == 468_000


def test_usable_input_budget_uses_full_context_without_reservation():
    assert _usable_input_budget(500_000, 0) == 500_000


def test_usable_input_budget_rejects_unknown_and_ignores_invalid_reservation():
    assert _usable_input_budget(0, 32_000) == 0
    assert _usable_input_budget("unknown", 32_000) == 0
    assert _usable_input_budget(16_000, 32_000) == 16_000


def test_usable_input_budget_logs_discarded_oversized_reservation(caplog):
    with caplog.at_level("DEBUG", logger="gateway.run"):
        assert _usable_input_budget(16_000, 32_000) == 16_000

    assert "output reservation (32000 tokens) consumes the context window" in (
        caplog.text
    )
