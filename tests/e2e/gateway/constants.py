"""Shared probe constants (model name, steering prompt, JSON schemas)."""

from __future__ import annotations

import os

# The name the API server advertises (API_SERVER_MODEL_NAME, default below).
MODEL = os.environ.get("HERMES_E2E_MODEL_NAME", "hermes-agent")

# Steer the agent away from its toolset so a simple extraction returns the
# object directly instead of spending turns on tool calls.
STEER = (
    "You are a JSON extraction endpoint. Do not call any tools. Reply with ONLY "
    "the structured object requested, nothing else."
)

LOCATION_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
    "required": ["city", "country"],
    "additionalProperties": False,
}

WORD_SCHEMA = {
    "type": "object",
    "properties": {"word": {"type": "string"}},
    "required": ["word"],
    "additionalProperties": False,
}
