"""``hermes compression`` subcommand parser.

Displays context-compressor configuration for debugging and tuning.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import json


def build_compression_parser(subparsers, *, cmd_compression: Callable) -> None:  # noqa: ANN001
    """Attach the ``compression`` subcommand to ``subparsers``."""
    compression_parser = subparsers.add_parser(
        "compression",
        help="Inspect context-compressor configuration",
        description=(
            "Show configured context-compressor settings: threshold, tail budget, "
            "summary ratio, model, and runtime state when available. "
            "Note: runtime counters require a live agent session."
        ),
    )

    subparsers_c = compression_parser.add_subparsers(dest="compression_command")

    diagnostics_parser = subparsers_c.add_parser(
        "diagnostics",
        help="Print compressor configuration as JSON",
    )
    diagnostics_parser.add_argument(
        "--raw",
        action="store_true",
        help="Output raw JSON (default: human-readable)",
    )
    diagnostics_parser.set_defaults(func=cmd_compression)


def _coalesce_number(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return fallback


def format_diagnostics(d: Dict[str, Any], *, raw: bool = False) -> str:
    if raw:
        return json.dumps(d, ensure_ascii=True, default=str)

    lines = []
    lines.append("Context compressor configuration")
    lines.append("=" * 40)
    lines.append(f"model              : {d.get('model')}")
    lines.append(f"provider           : {d.get('provider')}")
    lines.append(f"base_url           : {d.get('base_url')}")
    lines.append(f"context_length     : {d.get('context_length')}")
    lines.append(f"max_tokens         : {d.get('max_tokens')}")
    lines.append(f"threshold_percent  : {_coalesce_number(d.get('threshold_percent')) * 100:.1f}%")
    lines.append(f"threshold_tokens   : {d.get('threshold_tokens')}")
    lines.append(f"summary_ratio      : {_coalesce_number(d.get('summary_target_ratio')) * 100:.1f}%")
    lines.append(f"tail_token_budget  : {d.get('tail_token_budget')}")
    lines.append(f"max_summary_tokens : {d.get('max_summary_tokens')}")
    lines.append("")
    lines.append("State")
    lines.append("-" * 40)
    lines.append(f"compression_count                        : {d.get('compression_count')}")
    lines.append(f"last_savings_pct                         : {_coalesce_number(d.get('last_compression_savings_pct'), 0.0):.2f}%")
    lines.append(f"ineffective_compression_count            : {d.get('ineffective_compression_count')}")
    lines.append(f"summary_cooldown_remaining_s             : {_coalesce_number(d.get('summary_failure_cooldown_remaining_seconds'), 0.0):.1f}")
    lines.append(f"last_summary_error                       : {d.get('last_summary_error')}")
    lines.append(f"last_compress_aborted                    : {d.get('last_compress_aborted')}")
    lines.append(f"summary_model                            : {d.get('summary_model')}")
    lines.append(f"awaiting_real_usage_after_compression    : {d.get('awaiting_real_usage_after_compression')}")
    lines.append("")
    lines.append("Counters")
    lines.append("-" * 40)
    lines.append(f"last_prompt_tokens                       : {d.get('last_prompt_tokens')}")
    lines.append(f"last_real_prompt_tokens                  : {d.get('last_real_prompt_tokens')}")
    lines.append(f"last_compression_rough_tokens            : {d.get('last_compression_rough_tokens')}")
    lines.append(f"abort_on_summary_failure                 : {d.get('abort_on_summary_failure')}")
    lines.append(f"quiet_mode                               : {d.get('quiet_mode')}")
    return "\n".join(lines)
