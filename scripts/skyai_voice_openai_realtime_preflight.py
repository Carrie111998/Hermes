#!/usr/bin/env python3
"""DEV-only SkyAI OpenAI Realtime voice preflight.

This validates the intended low-latency Realtime lane without opening a
WebSocket, sending audio, calling OpenAI, touching PBX/SIP/RTP, or printing
secret values.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.skyai_customer.voice_audio import voice_realtime_preflight


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print full JSON output")
    parser.add_argument(
        "--require-key",
        action="store_true",
        help="Exit non-zero when VOICE_TOOLS_OPENAI_KEY is not configured",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = voice_realtime_preflight()
    configured = bool(result["api_key"]["configured"])  # type: ignore[index]
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        marker = "PASS" if configured else "BLOCKED"
        openai = result["openai_realtime"]  # type: ignore[index]
        brain = result["skyai_brain"]  # type: ignore[index]
        behavior = result["conversation_behavior"]  # type: ignore[index]
        print(f"SkyAI OpenAI Realtime voice preflight: {marker}")
        print(f"provider_lane={result['provider_lane']}")
        print(
            "api_key_configured="
            f"{str(configured).lower()} env={result['api_key']['env']} value_printed=false"  # type: ignore[index]
        )
        print(
            "realtime="
            f"model:{openai['model']} voice:{openai['voice']} "
            f"format_in:{openai['input_audio_format']} format_out:{openai['output_audio_format']} "
            f"turn_detection:{openai['turn_detection']}"
        )
        print(
            "skyai_brain="
            f"runtime:{brain['runtime']} target:{brain['backend_target']} toolset:{brain['toolset']} "
            f"keyword_guards_allowed:{str(brain['keyword_guards_allowed']).lower()}"
        )
        print(
            "behavior="
            f"barge_in_required:{str(behavior['barge_in_required']).lower()} "
            "gateway_repeated_filler_phrases_allowed:"
            f"{str(behavior['gateway_repeated_filler_phrases_allowed']).lower()}"
        )
    if args.require_key and not configured:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
