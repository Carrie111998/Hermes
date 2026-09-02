"""The auxiliary-window compaction clamp must be reversible, not a ratchet.

``check_compression_model_feasibility`` lowers a session's compaction trigger
to the auxiliary compression model's context window, so the summariser is
never handed a region it cannot ingest. That clamp used to be one-way: it ran
only at agent construction and only ever lowered.

``auxiliary.compression.model`` is live-editable, so the inputs the clamp
depends on change under a running session. Symptom: a 272K session whose aux
model was temporarily a 128K one stayed pinned at a 128,000 trigger for the
rest of its life — compacting ~1.6x more often than configured — even after
the operator pointed compression back at a full-window model. The reverse is
worse: raising the trigger from config while a small aux model is still
configured re-opens the failure the clamp exists to prevent.

These tests assert the relation both ways: the live trigger must always be
``min(configured_trigger, aux_context)`` for the CURRENT aux model.
"""

from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.conversation_compression import (
    _restore_configured_compression_threshold,
    check_compression_model_feasibility,
)

MAIN_CTX = 272_000
BIG_AUX_CTX = 272_000
SMALL_AUX_CTX = 128_000


class _Agent:
    """Minimal agent surface used by the feasibility check."""

    def __init__(self, compressor):
        self.context_compressor = compressor
        self.compression_enabled = True
        self.model = "main-model"
        self.provider = "test-provider"
        self._compression_warning = None
        self._custom_providers = {}
        self.status_callback = None
        self.emitted: list[str] = []

    def _current_main_runtime(self):
        return {"model": self.model, "provider": self.provider}

    def _emit_status(self, msg):
        self.emitted.append(msg)


def _compressor(threshold_percent=0.5):
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=MAIN_CTX
    ):
        c = ContextCompressor(
            model="main-model",
            provider="test-provider",
            threshold_percent=threshold_percent,
            quiet_mode=True,
        )
        _ = c.context_length
    return c


def _run_check(agent, aux_ctx, aux_model="aux-model"):
    """Drive the real feasibility check with a stubbed aux route."""
    with (
        patch(
            "agent.auxiliary_client.get_text_auxiliary_client",
            return_value=(object(), aux_model),
        ),
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("aux-provider", aux_model, "", "", ""),
        ),
        patch(
            "agent.model_metadata.get_model_context_length", return_value=aux_ctx
        ),
    ):
        check_compression_model_feasibility(agent)


@pytest.fixture()
def configured_threshold():
    """The trigger a fresh session derives from config, with no clamp."""
    return _compressor().threshold_tokens


class TestClampIsReversible:
    def test_small_aux_model_lowers_the_trigger(self, configured_threshold):
        compressor = _compressor()
        agent = _Agent(compressor)
        _run_check(agent, SMALL_AUX_CTX)
        assert compressor.threshold_tokens == SMALL_AUX_CTX
        assert compressor.threshold_tokens < configured_threshold

    def test_switching_back_to_a_large_aux_model_restores_the_trigger(
        self, configured_threshold
    ):
        """The bug: this used to stay pinned at the small model's window."""
        compressor = _compressor()
        agent = _Agent(compressor)
        _run_check(agent, SMALL_AUX_CTX)
        assert compressor.threshold_tokens == SMALL_AUX_CTX

        _run_check(agent, BIG_AUX_CTX)
        assert compressor.threshold_tokens == configured_threshold

    def test_repeated_checks_are_idempotent(self, configured_threshold):
        compressor = _compressor()
        agent = _Agent(compressor)
        for _ in range(3):
            _run_check(agent, SMALL_AUX_CTX)
            assert compressor.threshold_tokens == SMALL_AUX_CTX
        for _ in range(3):
            _run_check(agent, BIG_AUX_CTX)
            assert compressor.threshold_tokens == configured_threshold

    def test_trigger_always_fits_the_current_aux_window(self, configured_threshold):
        """The invariant, stated directly: trigger == min(configured, aux)."""
        compressor = _compressor()
        agent = _Agent(compressor)
        for aux_ctx in (SMALL_AUX_CTX, BIG_AUX_CTX, 200_000, BIG_AUX_CTX, 90_000):
            _run_check(agent, aux_ctx)
            assert compressor.threshold_tokens == min(configured_threshold, aux_ctx)


class TestClampDoesNotCorruptDerivation:
    def test_clamp_never_writes_a_sub_floor_threshold_percent(self):
        """A raw ``clamped/context`` ratio bypasses the small-window floor.

        ``_effective_threshold_percent`` floors sub-512K windows; writing the
        clamp back as a percentage stored 0.47 on a 272K model, below that
        floor, so any later re-derivation produced a below-floor trigger.
        """
        compressor = _compressor()
        agent = _Agent(compressor)
        _run_check(agent, SMALL_AUX_CTX)
        floor = ContextCompressor._effective_threshold_percent(MAIN_CTX, 0.5)
        assert compressor.threshold_percent >= floor

    def test_tail_budget_follows_the_trigger_in_both_directions(self):
        compressor = _compressor()
        agent = _Agent(compressor)
        unclamped_tail = compressor.tail_token_budget

        _run_check(agent, SMALL_AUX_CTX)
        clamped_tail = compressor.tail_token_budget
        assert clamped_tail <= compressor.threshold_tokens

        _run_check(agent, BIG_AUX_CTX)
        assert compressor.tail_token_budget == unclamped_tail

    def test_restore_honours_a_config_edit_made_while_clamped(self):
        """Restoring re-derives from config instead of replaying a snapshot."""
        compressor = _compressor()
        agent = _Agent(compressor)
        _run_check(agent, SMALL_AUX_CTX)

        # Operator lowers the configured threshold while the clamp is active.
        compressor._config_threshold_percent = 0.30
        _run_check(agent, BIG_AUX_CTX)

        expected = ContextCompressor._compute_threshold_tokens(
            MAIN_CTX,
            ContextCompressor._effective_threshold_percent(MAIN_CTX, 0.30),
            compressor.max_tokens,
        )
        assert compressor.threshold_tokens == expected

    def test_restore_respects_the_absolute_threshold_cap(self):
        compressor = _compressor()
        agent = _Agent(compressor)
        _run_check(agent, SMALL_AUX_CTX)

        compressor.threshold_tokens_cap = 100_000
        _run_check(agent, BIG_AUX_CTX)
        assert compressor.threshold_tokens == 100_000


class TestRestoreHelper:
    def test_no_op_when_no_clamp_was_applied(self):
        compressor = _compressor()
        agent = _Agent(compressor)
        before = compressor.threshold_tokens
        assert _restore_configured_compression_threshold(agent) is False
        assert compressor.threshold_tokens == before

    def test_reports_that_it_lifted_a_clamp(self):
        compressor = _compressor()
        agent = _Agent(compressor)
        _run_check(agent, SMALL_AUX_CTX)
        assert _restore_configured_compression_threshold(agent) is True
        assert _restore_configured_compression_threshold(agent) is False
