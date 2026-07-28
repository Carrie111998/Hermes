#!/usr/bin/env python3
"""DEV-only SkyAI voice HTTP contract smoke.

This script exercises the transcript/event HTTP adapter only. It does not
connect to SIP, RTP, PBX, STT, TTS, customer data, orders, payments, or
vouchers.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:8787"
CONTRACT_VERSION = "skyai-voice-contract.v0.1"
ALLOWED_ACTIONS = {"speak", "clarify", "transfer_to_human", "end_call"}
REQUIRED_RESPONSE_FIELDS = {
    "status",
    "version",
    "contract_version",
    "call_id",
    "conversation_id",
    "action",
    "spoken_reply",
    "display_reply",
    "cards",
    "transfer",
    "transfer_reason",
    "target",
    "end_call",
    "session_state",
    "trace",
    "notes",
    "unavailable",
}


@dataclass(frozen=True)
class SmokeRequest:
    path: str
    payload: dict[str, Any]
    expected_action: str


def build_smoke_requests(
    *,
    call_id: str,
    conversation_id: str,
    backend_target: str,
) -> list[SmokeRequest]:
    base_payload = {
        "call_id": call_id,
        "conversation_id": conversation_id,
        "caller_id": "+359000000000",
        "did": "+359000000001",
        "pbx_extension": "399",
        "department": "dev",
        "language": "bg-BG",
        "source": "skyai-voice-contract-smoke",
        "metadata": {
            "backend_target": backend_target,
            "smoke": True,
        },
    }
    return [
        SmokeRequest(
            "/voice/start",
            {
                **base_payload,
                "recording_notice_played": False,
            },
            "speak",
        ),
        SmokeRequest(
            "/voice/turn",
            {
                **base_payload,
                "turn_index": 1,
                "transcript": "Търся подарък за рожден ден.",
                "is_final": True,
                "stt_confidence": 0.94,
            },
            "speak",
        ),
        SmokeRequest(
            "/voice/turn",
            {
                **base_payload,
                "turn_index": 2,
                "transcript": "неясен звук",
                "is_final": True,
                "stt_confidence": 0.2,
            },
            "clarify",
        ),
        SmokeRequest(
            "/voice/event",
            {
                **base_payload,
                "event_type": "dtmf",
                "dtmf": "0",
            },
            "transfer_to_human",
        ),
        SmokeRequest(
            "/voice/end",
            {
                **base_payload,
                "ended_by": "smoke",
                "duration_seconds": 12,
                "recording_stored": False,
                "transcript_stored": False,
            },
            "end_call",
        ),
    ]


def call_json(base_url: str, request: SmokeRequest, *, token: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(request.payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "SkyAI-Voice-Contract-Smoke/0.1",
        "X-SkyAI-Test-Signal": "voice_contract_smoke",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    http_request = Request(
        f"{base_url.rstrip('/')}{request.path}",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urlopen(http_request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return {
            "status": "error",
            "error": "http_error",
            "http_status": exc.code,
            "reason": exc.read().decode("utf-8", errors="replace")[:1000],
        }
    except URLError as exc:
        return {"status": "error", "error": "url_error", "reason": str(exc)[:500]}


def validate_response(request: SmokeRequest, response: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_RESPONSE_FIELDS.difference(response))
    if missing:
        errors.append(f"missing_fields:{','.join(missing)}")
    if response.get("status") != "ok":
        errors.append(f"status_not_ok:{response.get('status')}")
    if response.get("contract_version") != CONTRACT_VERSION:
        errors.append(f"contract_version_mismatch:{response.get('contract_version')}")
    action = response.get("action")
    if action not in ALLOWED_ACTIONS:
        errors.append(f"invalid_action:{action}")
    if action != request.expected_action:
        errors.append(f"unexpected_action:{action}:expected:{request.expected_action}")
    if response.get("call_id") != request.payload["call_id"]:
        errors.append("call_id_mismatch")
    if response.get("conversation_id") != request.payload["conversation_id"]:
        errors.append("conversation_id_mismatch")
    if not str(response.get("spoken_reply") or "").strip():
        errors.append("empty_spoken_reply")
    if not isinstance(response.get("trace"), dict):
        errors.append("trace_not_object")
    elif response["trace"].get("raw_audio_stored") is not False:
        errors.append("raw_audio_storage_not_false")
    if action == "transfer_to_human":
        transfer = response.get("transfer")
        if not isinstance(transfer, dict):
            errors.append("transfer_missing")
        elif not transfer.get("target") or not transfer.get("reason"):
            errors.append("transfer_incomplete")
        if not response.get("transfer_reason") or not response.get("target"):
            errors.append("flattened_transfer_fields_missing")
    if action == "end_call" and response.get("end_call") is not True:
        errors.append("end_call_flag_not_true")
    return errors


def run_smoke(
    *,
    base_url: str,
    token: str,
    timeout: float,
    call_id: str,
    conversation_id: str,
    backend_target: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for smoke_request in build_smoke_requests(
        call_id=call_id,
        conversation_id=conversation_id,
        backend_target=backend_target,
    ):
        started = time.monotonic()
        response = call_json(base_url, smoke_request, token=token, timeout=timeout)
        latency_ms = int((time.monotonic() - started) * 1000)
        errors = validate_response(smoke_request, response)
        results.append(
            {
                "path": smoke_request.path,
                "expected_action": smoke_request.expected_action,
                "action": response.get("action"),
                "status": response.get("status"),
                "latency_ms": latency_ms,
                "errors": errors,
            }
        )
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token-env", default="SKYAI_V2_CANARY_TOKEN")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--backend-target", default="skyai_v2_chatkit")
    parser.add_argument("--call-id", default="voice-smoke-call")
    parser.add_argument("--conversation-id", default="voice-smoke-conversation")
    parser.add_argument("--json", action="store_true", help="Print full JSON result")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.getenv(args.token_env, "")
    results = run_smoke(
        base_url=args.base_url,
        token=token,
        timeout=args.timeout,
        call_id=args.call_id,
        conversation_id=args.conversation_id,
        backend_target=args.backend_target,
    )
    ok = all(not item["errors"] for item in results)
    output = {
        "status": "pass" if ok else "fail",
        "base_url": args.base_url,
        "backend_target": args.backend_target,
        "contract_version": CONTRACT_VERSION,
        "results": results,
    }
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"SkyAI voice contract smoke: {output['status'].upper()}")
        for item in results:
            marker = "PASS" if not item["errors"] else "FAIL"
            print(
                f"- {marker} {item['path']}: action={item['action']} "
                f"expected={item['expected_action']} latency_ms={item['latency_ms']}"
            )
            for error in item["errors"]:
                print(f"  error={error}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
