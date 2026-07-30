#!/usr/bin/env python3
"""DEV-only SkyAI OpenAI audio preflight.

Checks whether the SkyAI voice gateway can use OpenAI API-backed STT/TTS while
leaving SkyAI reasoning on the existing Hermes/Codex OAuth lane.  This script
does not send audio, synthesize speech, call OpenAI, touch PBX/SIP/RTP, or
print secret values.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.skyai_customer.voice_audio import voice_audio_preflight


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
    result = voice_audio_preflight()
    configured = bool(result["api_key"]["configured"])  # type: ignore[index]
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        marker = "PASS" if configured else "BLOCKED"
        print(f"SkyAI OpenAI audio preflight: {marker}")
        print(f"provider_lane={result['provider_lane']}")
        print(f"reasoning_auth={result['reasoning_auth']}")
        print(f"audio_auth={result['audio_auth']}")
        print(
            "api_key_configured="
            f"{str(configured).lower()} env={result['api_key']['env']} value_printed=false"  # type: ignore[index]
        )
        openai = result["openai"]  # type: ignore[index]
        print(
            "models="
            f"stt_primary:{openai['stt_primary_model']} "  # type: ignore[index]
            f"stt_fast:{openai['stt_fast_model']} "  # type: ignore[index]
            f"tts:{openai['tts_model']} "  # type: ignore[index]
            f"voice:{openai['tts_voice']} "  # type: ignore[index]
            f"realtime_transcription:{openai['realtime_transcription_model']}"  # type: ignore[index]
        )
    if args.require_key and not configured:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
