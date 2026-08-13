"""Em dashes never reach a chat surface.

House style bans U+2014 in delivered agent text. The rewrite lives at the
gateway delivery boundary (``gateway.response_filters.sanitize_em_dashes``)
rather than in any adapter, so every platform inherits it, and rather than in
the agent loop, so the stored transcript keeps what the model actually wrote.
"""

from gateway.response_filters import EM_DASH, sanitize_em_dashes

EM = "—"
EN = "–"


class TestSanitizeEmDashes:
    def test_constant_is_u2014(self):
        assert EM_DASH == EM

    def test_single_em_dash_becomes_hyphen(self):
        assert sanitize_em_dashes(f"yes{EM}but") == "yes-but"

    def test_every_occurrence_replaced(self):
        text = f"a{EM}b{EM}c{EM}d"
        assert sanitize_em_dashes(text) == "a-b-c-d"
        assert EM not in sanitize_em_dashes(text)

    def test_spaced_em_dash_keeps_its_spaces(self):
        # Only the character is rewritten; surrounding whitespace is the
        # model's business.
        assert sanitize_em_dashes(f"done {EM} shipping now") == "done - shipping now"

    def test_en_dash_is_left_alone(self):
        """U+2013 is a different character with a different job (ranges)."""
        assert sanitize_em_dashes(f"pages 3{EN}7") == f"pages 3{EN}7"

    def test_existing_hyphens_untouched(self):
        assert sanitize_em_dashes("well-known re-entrant") == "well-known re-entrant"

    def test_clean_text_returned_unchanged(self):
        text = "Nothing to do here."
        assert sanitize_em_dashes(text) is text

    def test_empty_string(self):
        assert sanitize_em_dashes("") == ""

    def test_non_string_passes_through(self):
        """Callers chain this inline; None/int must not explode."""
        assert sanitize_em_dashes(None) is None
        assert sanitize_em_dashes(42) == 42


class TestGatewaySendPathAppliesIt:
    """The filter is wired into the real delivery boundary, not just exported."""

    def test_final_response_sanitizer_strips_em_dashes(self):
        from gateway.run import _sanitize_gateway_final_response

        cleaned = _sanitize_gateway_final_response(
            "telegram", f"Deploy finished{EM}no rollback needed"
        )
        assert EM not in cleaned
        assert cleaned == "Deploy finished-no rollback needed"

    def test_applies_across_platforms(self):
        """Wired once at the boundary, so no adapter needs its own copy."""
        from gateway.run import _sanitize_gateway_final_response

        for platform in ("telegram", "discord", "slack", "signal", "whatsapp"):
            assert EM not in _sanitize_gateway_final_response(platform, f"a{EM}b")

    def test_raw_text_surfaces_keep_the_em_dash(self):
        """`local`/API/webhook are programmatic, not chat. Rule doesn't apply."""
        from gateway.run import _sanitize_gateway_final_response

        raw = f"Deploy finished{EM}no rollback needed"
        assert _sanitize_gateway_final_response("local", raw) == raw
        assert _sanitize_gateway_final_response("api_server", raw) == raw

    def test_streaming_display_path_strips_em_dashes(self):
        """The streaming consumer shares the same rule as the final send."""
        from gateway.stream_consumer import GatewayStreamConsumer

        assert GatewayStreamConsumer._clean_for_display(f"a{EM}b") == "a-b"
