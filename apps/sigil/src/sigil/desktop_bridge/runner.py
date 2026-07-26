"""Governed JSON command runner for Sigil Desktop.

This bridge is intentionally read-only. It exposes no execution, broker,
shell, filesystem-write, credential, or arbitrary command capability.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime
from typing import Any, Final

BRIDGE_VERSION: Final[str] = "1"
SUPPORTED_COMMANDS: Final[tuple[str, ...]] = (
    "health",
    "explain_proposal",
)


def generated_at() -> str:
    """Return a UTC ISO-8601 timestamp."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def backend_status() -> dict[str, Any]:
    """Return deterministic, local-only Sigil backend status."""

    return {
        "bridge_version": BRIDGE_VERSION,
        "status": "ok",
        "mode": "local-read-only",
        "environment": "paper",
        "simulation": True,
        "execution_authorized": False,
        "broker_submission_available": False,
        "supported_commands": list(SUPPORTED_COMMANDS),
    }


def error_response(error: str, message: str) -> dict[str, Any]:
    """Return a consistent fail-closed response."""

    return {
        "ok": False,
        "error": error,
        "message": message,
    }


def valid_non_empty_string(value: object) -> bool:
    """Return whether a value is a non-empty string."""

    return isinstance(value, str) and bool(value.strip())


def validate_evidence_references(value: object) -> list[dict[str, str]] | None:
    """Validate and sanitize evidence references."""

    if not isinstance(value, list):
        return None

    references: list[dict[str, str]] = []

    for item in value:
        if not isinstance(item, dict):
            return None

        reference_id = item.get("id")
        if not valid_non_empty_string(reference_id):
            return None

        reference: dict[str, str] = {"id": reference_id.strip()}

        label = item.get("label")
        source = item.get("source")

        if label is not None:
            if not valid_non_empty_string(label):
                return None
            reference["label"] = label.strip()

        if source is not None:
            if not valid_non_empty_string(source):
                return None
            reference["source"] = source.strip()

        references.append(reference)

    return references


def explain_proposal(payload: object) -> dict[str, Any]:
    """Return a governed, deterministic proposal explanation."""

    if not isinstance(payload, dict):
        return error_response(
            "invalid_payload",
            "Proposal explanation requires a JSON payload object.",
        )

    proposal_id = payload.get("proposal_id")
    symbol = payload.get("symbol")
    side = payload.get("side")
    strategy = payload.get("strategy")
    estimated_notional = payload.get("estimated_notional")
    evidence = validate_evidence_references(payload.get("evidence_references"))

    if not valid_non_empty_string(proposal_id):
        return error_response("invalid_payload", "proposal_id is required.")

    if not valid_non_empty_string(symbol):
        return error_response("invalid_payload", "symbol is required.")

    if side not in {"BUY", "SELL"}:
        return error_response("invalid_payload", "side must be BUY or SELL.")

    if not valid_non_empty_string(strategy):
        return error_response("invalid_payload", "strategy is required.")

    if (
        isinstance(estimated_notional, bool)
        or not isinstance(estimated_notional, (int, float))
        or not math.isfinite(float(estimated_notional))
        or estimated_notional < 0
    ):
        return error_response(
            "invalid_payload",
            "estimated_notional must be a finite non-negative number.",
        )

    if evidence is None:
        return error_response(
            "invalid_payload",
            "evidence_references must be a list of valid evidence objects.",
        )

    normalized_proposal_id = proposal_id.strip()
    normalized_symbol = symbol.strip().upper()
    normalized_strategy = strategy.strip()
    normalized_notional = float(estimated_notional)

    return {
        "ok": True,
        "result": {
            "kind": "proposal-explanation",
            "summary": (
                f"Proposal {normalized_proposal_id} is governed, "
                "locally verified, and simulated."
            ),
            "explanation": (
                f"{normalized_symbol} is presented as a {side} proposal from "
                f"{normalized_strategy} with an estimated notional of "
                f"${normalized_notional:.2f}. The Python bridge validated the "
                "proposal context and evidence references. The proposal may be "
                "reviewed or simulated, but it cannot be submitted to a broker."
            ),
            "model_route": f"python-bridge-v{BRIDGE_VERSION}",
            "source": "local",
            "confidence": 1.0,
            "evidence_references": evidence,
            "generated_at": generated_at(),
            "execution_authorized": False,
            "broker_submission_available": False,
        },
    }


def handle_request(request: object) -> dict[str, Any]:
    """Handle one allow-listed bridge request."""

    if not isinstance(request, dict):
        return error_response(
            "invalid_request",
            "Request must be a JSON object.",
        )

    command = request.get("command")

    if command == "health":
        return {
            "ok": True,
            "result": backend_status(),
        }

    if command == "explain_proposal":
        return explain_proposal(request.get("payload"))

    return error_response(
        "unsupported_command",
        "Only allow-listed read-only commands are available.",
    )


def main() -> int:
    """Read one JSON request from stdin and emit one JSON response."""

    try:
        request = json.load(sys.stdin)
        response = handle_request(request)
    except (json.JSONDecodeError, UnicodeDecodeError):
        response = error_response(
            "invalid_json",
            "Input must contain one valid JSON object.",
        )
    except Exception:
        response = error_response(
            "bridge_failure",
            "The local Sigil bridge failed safely.",
        )

    json.dump(response, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0 if response.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
