"""4tp-2: pre-compaction heads-up fires once as usage nears the threshold.

``AIAgent._maybe_warn_precompaction`` should:
  * emit exactly one heads-up while usage is in the warn band
    (``warn_threshold_tokens <= tokens < threshold_tokens``);
  * stay quiet at/above the compaction threshold (that's ``should_compress``);
  * re-arm once usage drops back below the band, so a later approach warns
    again;
  * produce copy that passes the gateway noise filter (so it reaches Telegram,
    unlike the transient "Compacting context…" line).
"""

from __future__ import annotations

from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from run_agent import AIAgent


class _FakeAgent:
    """Minimal duck type carrying only what the helper touches."""

    def __init__(self, compressor):
        self.compression_enabled = True
        self.context_compressor = compressor
        self._precompaction_warned = False
        self.emitted: list[str] = []

    def _emit_warning(self, message: str) -> None:
        self.emitted.append(message)

    # Bind the real, unbound method under test.
    warn = AIAgent._maybe_warn_precompaction


def _compressor():
    # context 100_000 · threshold 85_000 · warn band starts at 72_250.
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(model="m", threshold_percent=0.85, quiet_mode=True)


def test_warns_once_in_band_then_stays_quiet():
    agent = _FakeAgent(_compressor())
    agent.warn(75_000)
    agent.warn(78_000)  # still in band, but already warned
    assert len(agent.emitted) == 1
    assert "approaching compaction" in agent.emitted[0].lower()


def test_no_warning_below_band():
    agent = _FakeAgent(_compressor())
    agent.warn(60_000)
    assert agent.emitted == []


def test_no_warning_at_or_above_threshold():
    agent = _FakeAgent(_compressor())
    agent.warn(90_000)  # compaction territory — should_compress owns this
    assert agent.emitted == []


def test_rearms_after_dropping_below_band():
    agent = _FakeAgent(_compressor())
    agent.warn(75_000)          # warns
    agent.warn(50_000)          # drops below band → re-arm
    assert agent._precompaction_warned is False
    agent.warn(75_000)          # approaches again → warns again
    assert len(agent.emitted) == 2


def test_disabled_compression_never_warns():
    agent = _FakeAgent(_compressor())
    agent.compression_enabled = False
    agent.warn(75_000)
    assert agent.emitted == []


def test_plugin_engine_without_warn_band_is_safe():
    class _NoWarn:
        warn_threshold_tokens = 0
        threshold_tokens = 85_000
        # no should_warn method

    agent = _FakeAgent(_NoWarn())
    agent.warn(75_000)  # must not raise
    assert agent.emitted == []


def test_warning_copy_passes_gateway_noise_filter():
    from gateway.run import _prepare_gateway_status_message
    from gateway.config import Platform

    agent = _FakeAgent(_compressor())
    agent.warn(75_000)
    assert agent.emitted
    assert (
        _prepare_gateway_status_message(Platform.TELEGRAM, "warn", agent.emitted[0])
        is not None
    )
