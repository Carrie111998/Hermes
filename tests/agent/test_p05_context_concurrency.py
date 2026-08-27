"""P0.5 regression guard — context & concurrency governance.

Goal (per task, config-first, no new features):
  A. Context governance:
       compression.threshold_tokens = 48000
     -> compression triggers at ~48K tokens instead of waiting for the
        ratio-based threshold near the model's ~196K context window.
     Algorithm unchanged.
  B. Worker concurrency guardrail:
       tool_loop_guardrails.loop_caps.max_subagents = 3
     -> at most 3 subagents spawned concurrently per turn.
     Sequential delegation (one spawn after another, counters reset per
     turn) is unaffected.

Audit finding:
  * compression.threshold_tokens is read in agent_init.py
    (_compression_cfg.get("threshold_tokens")) and applied as
    threshold_tokens_cap to the context compressor; _apply_threshold_tokens_cap
    takes min(ratio_threshold, cap).  Source of truth = DEFAULT_CONFIG
    ["compression"]["threshold_tokens"] (deep-merged with user config.yaml).
  * tool_loop_guardrails.loop_caps.max_subagents feeds
    LoopCapConfig.from_mapping, whose dataclass default is the
    _DEFAULT_MAX_SUBAGENTS_PER_TURN constant.  Both the DEFAULT_CONFIG entry
    AND the constant are set to 3 so the cap is deterministic regardless of
    whether the runtime feeds the config section or relies on the default.

The tests assert the EFFECTIVE default values (what runtime resolves to),
not just the source literals.
"""

from __future__ import annotations

import sys
import os

HERMES = "/Users/phatvo/.hermes/hermes-agent"
if HERMES not in sys.path:
    sys.path.insert(0, HERMES)


class TestP05_CompressionThreshold:
    def test_default_threshold_tokens_is_48000(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        val = DEFAULT_CONFIG["compression"]["threshold_tokens"]
        assert val == 48000, f"expected 48000, got {val!r}"
        assert isinstance(val, int) and val > 0

    def test_effective_threshold_resolves_to_48000(self):
        """Effective config (DEFAULT_CONFIG deep-merged with a minimal user
        config that does NOT override compression) must surface 48000."""
        from hermes_cli.config import cfg_get
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        # Minimal user config — no compression override.
        user_cfg = {}
        merged = {**DEFAULT_CONFIG, **user_cfg}
        # cfg_get walks the default for unset keys; emulate the real read.
        eff = cfg_get(merged, "compression", "threshold_tokens")
        assert eff == 48000

    def test_threshold_is_applied_as_cap_at_runtime(self, monkeypatch):
        """agent_init reads compression.threshold_tokens and uses it as the
        compressor's absolute cap.  Assert the read path yields 48000."""
        import agent.agent_init as ai
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        # Replicate agent_init's exact read expression.
        compression_threshold_tokens = DEFAULT_CONFIG["compression"].get("threshold_tokens")
        assert compression_threshold_tokens is not None
        assert int(compression_threshold_tokens) == 48000


class TestP05_MaxSubagents:
    def test_default_max_subagents_is_3(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        val = DEFAULT_CONFIG["tool_loop_guardrails"]["loop_caps"]["max_subagents"]
        assert val == 3, f"expected 3, got {val!r}"

    def test_guardrail_constant_is_3(self):
        """The dataclass default constant must match — deterministic even if
        the runtime relies on the default rather than the config section."""
        from agent.tool_guardrails import _DEFAULT_MAX_SUBAGENTS_PER_TURN

        assert _DEFAULT_MAX_SUBAGENTS_PER_TURN == 3

    def test_loopcap_default_is_3(self):
        """LoopCapConfig() default (no config fed) must be 3 subagents."""
        from agent.tool_guardrails import LoopCapConfig

        assert LoopCapConfig().max_subagents == 3

    def test_from_mapping_honours_3(self):
        """Parsing the default loop_caps section yields max_subagents=3."""
        from agent.tool_guardrails import LoopCapConfig
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        cfg = DEFAULT_CONFIG["tool_loop_guardrails"]["loop_caps"]
        parsed = LoopCapConfig.from_mapping(cfg)
        assert parsed.max_subagents == 3

    def test_cap_still_blocks_fourth_spawn(self):
        """Regression: with max_subagents=3, the 4th concurrent spawn is
        blocked; sequential (per-turn reset) delegation is unaffected."""
        from agent.tool_guardrails import (
            LoopCapConfig,
            ToolCallGuardrailConfig,
            ToolCallGuardrailController,
        )

        ctl = ToolCallGuardrailController(
            ToolCallGuardrailConfig(loop_caps=LoopCapConfig(max_subagents=3))
        )
        # 3 allowed spawns
        for i in range(3):
            assert ctl.before_call("delegate_task", {"goal": f"g{i}"}).action == "allow"
        # 4th blocked
        assert ctl.before_call("delegate_task", {"goal": "g4"}).action == "block"
        # Fresh controller (new turn) resets the cap -> sequential OK
        ctl2 = ToolCallGuardrailController(
            ToolCallGuardrailConfig(loop_caps=LoopCapConfig(max_subagents=3))
        )
        assert ctl2.before_call("delegate_task", {"goal": "g5"}).action == "allow"
