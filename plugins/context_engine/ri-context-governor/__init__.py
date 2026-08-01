"""RecursiveIntell context-governor — Rust-backed context engine plugin.

Activate in config.yaml::

    context:
      engine: ri-context-governor
      governor:
        token_budget: 8000

Replaces the default LLM-based summarizer with context-governor's
deterministic token-budgeted compaction.
"""

from __future__ import annotations

from agent.transports.ri_context_compressor import RiContextCompressor


def register(ctx):
    """Plugin contract: register the RI context engine."""
    # Try reading token budget from config, default to 8000
    token_budget = 8000
    try:
        cfg = getattr(ctx, "config", None) or {}
        gov = cfg.get("governor", {}) if isinstance(cfg, dict) else {}
        if isinstance(gov, dict):
            token_budget = int(gov.get("token_budget", 8000))
    except Exception:
        pass

    ctx.register_context_engine(
        RiContextCompressor(token_budget=token_budget, name="ri-context-governor")
    )
