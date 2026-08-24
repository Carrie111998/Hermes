"""
Routing contract parser for Wave 2 enforcement.
Parses the delimited `# ---routing:v1---` block from task bodies.
"""
import re
import yaml
from typing import Optional

ROUTING_CONTRACT_VERSION = 1
_MAX_ENVELOPE_BYTES = 2048

# Delimiters for the routing envelope
_ENVELOPE_START = "# ---routing:v1---"
_ENVELOPE_END = "# ---/routing:v1---"

# Required fields in the v1 contract
_REQUIRED_FIELDS = {"role", "reason", "enforcement_required"}


class RoutingContractError(ValueError):
    """Raised when the routing envelope is malformed or invalid."""
    pass


def _extract_envelope(body: str) -> Optional[str]:
    """Extract the delimited routing block from a task body.
    
    Returns the YAML content between the delimiters, or None if no envelope found.
    Raises RoutingContractError if the start marker exists but the end marker is missing.
    """
    start_idx = body.find(_ENVELOPE_START)
    if start_idx == -1:
        return None
    end_idx = body.find(_ENVELOPE_END, start_idx)
    if end_idx == -1:
        raise RoutingContractError(
            "Routing envelope has start marker but missing end marker"
        )
    content = body[start_idx + len(_ENVELOPE_START):end_idx]
    if len(content.encode()) > _MAX_ENVELOPE_BYTES:
        raise RoutingContractError(
            f"Routing envelope exceeds {_MAX_ENVELOPE_BYTES} bytes"
        )
    return content


def _is_wave1_envelope(body: str) -> bool:
    """Check if the body contains a Wave 1 routing envelope (assigned_role/action style).
    
    Wave 1 envelopes use a different field set and must be classified as unenforced,
    never as parse errors.
    """
    # Wave 1 used `routing:` as a YAML key with `assigned_role` etc.
    # The v1 contract uses `# ---routing:v1---` delimiters instead.
    # If there's no v1 delimiter but there IS a `routing:` YAML key, it's Wave 1.
    if _ENVELOPE_START in body:
        return False  # Has v1 delimiter, not Wave 1
    # Check for Wave 1 style: indented `routing:` block or `assigned_role:`
    return bool(re.search(r'(?m)^\s*routing:\s*$', body)) or \
           bool(re.search(r'(?m)^\s*assigned_role:\s*\S+', body))


def parse_routing_envelope(body: str) -> dict:
    """Parse the routing contract v1 envelope from a task body.
    
    Returns a dict with the parsed fields, or an empty dict if no v1 envelope
    is found (unenforced/legacy).
    
    Raises RoutingContractError if the envelope is malformed.
    
    For Wave 1 envelopes (assigned_role/action style), returns an empty dict
    (classified as unenforced — never an error).
    """
    content = _extract_envelope(body)
    if content is None:
        # No v1 envelope. Check if it's a Wave 1 envelope.
        if _is_wave1_envelope(body):
            # Wave 1 envelopes are unenforced — not an error, just no v1 contract
            return {}
        return {}
    
    # Parse the YAML content
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise RoutingContractError(f"Routing envelope YAML parse error: {e}") from e
    
    if parsed is None:
        raise RoutingContractError("Routing envelope is empty")
    if not isinstance(parsed, dict):
        raise RoutingContractError(
            f"Routing envelope must be a YAML mapping, got {type(parsed).__name__}"
        )
    
    # Check required fields
    missing = _REQUIRED_FIELDS - set(parsed.keys())
    if missing:
        raise RoutingContractError(
            f"Routing envelope missing required fields: {missing}"
        )
    
    # Type validation
    role = parsed["role"]
    if not isinstance(role, str) or not role.strip():
        raise RoutingContractError("'role' must be a non-empty string")
    
    reason = parsed["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise RoutingContractError("'reason' must be a non-empty string")
    
    enforcement = parsed["enforcement_required"]
    if not isinstance(enforcement, bool):
        raise RoutingContractError("'enforcement_required' must be a boolean")
    
    # Optional fields
    if "model" in parsed and parsed["model"] is not None:
        if not isinstance(parsed["model"], str):
            raise RoutingContractError("'model' must be a string or absent")
    
    if "provider" in parsed and parsed["provider"] is not None:
        if not isinstance(parsed["provider"], str):
            raise RoutingContractError("'provider' must be a string or absent")
    
    if "ac_ids" in parsed and parsed["ac_ids"] is not None:
        if not isinstance(parsed["ac_ids"], list):
            raise RoutingContractError("'ac_ids' must be a list of strings or absent")
        for ac_id in parsed["ac_ids"]:
            if not isinstance(ac_id, str):
                raise RoutingContractError("'ac_ids' must contain only strings")
    
    return parsed