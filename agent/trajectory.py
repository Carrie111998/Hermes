"""Trajectory saving utilities and static helpers.

_convert_to_trajectory_format stays as an AIAgent method (batch_runner.py
calls agent._convert_to_trajectory_format). Only the static helpers and
the file-write logic live here.
"""

import contextvars
import hashlib
import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolTrace:
    """Execution-fidelity record for a single tool call.

    ``observation_id`` is a UUID. UUID v7 (time-ordered) would be preferable
    but no uuid7 implementation ships with the stdlib on this interpreter, so
    ``uuid.uuid4()`` is used — see ``new_observation_id``.
    """

    observation_id: str
    tool_name: str
    normalized_args: dict
    raw_response_hash: str          # SHA256 hex of full response
    transport_status: int           # HTTP status or equivalent; 0/-1 sentinel if not HTTP-based
    postcondition_status: str       # "succeeded" | "pending" | "failed" | "unknown" | "skipped"
    action_class: str               # "READ" | "DRAFT" | "REVERSIBLE_WRITE" | "IRREVERSIBLE_WRITE"


# Cap the process-lifetime store so long CLI / batch sessions cannot grow
# without bound (one entry per MCP call, args payload included).
_MAX_TRACES = 2000
_TRACES: OrderedDict[str, ToolTrace] = OrderedDict()

# Per-turn traces. ContextVar so nested / threaded / asyncio turns cannot
# wipe or intermix another turn's list. default=None (not []) — a mutable
# default would be shared across every context.
_TURN_TRACES: contextvars.ContextVar[Optional[List[ToolTrace]]] = contextvars.ContextVar(
    "execution_fidelity_turn_traces", default=None,
)


def new_observation_id() -> str:
    """Return a fresh observation id (uuid4 substitute for uuid7)."""
    return str(uuid.uuid4())


def hash_response(response: Any) -> str:
    """SHA256 hex digest of a tool response (stringified if not str/bytes)."""
    if isinstance(response, bytes):
        data = response
    elif isinstance(response, str):
        data = response.encode("utf-8")
    else:
        data = repr(response).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _turn_list() -> List[ToolTrace]:
    traces = _TURN_TRACES.get()
    if traces is None:
        traces = []
        _TURN_TRACES.set(traces)
    return traces


def store_trace(trace: "ToolTrace") -> None:
    """Store a ToolTrace in the in-memory store, keyed by observation_id.

    Also appends to the current turn's trace list (see ``reset_turn_traces``).
    Evicts the oldest entry once the store exceeds ``_MAX_TRACES``.
    """
    _TRACES[trace.observation_id] = trace
    while len(_TRACES) > _MAX_TRACES:
        _TRACES.popitem(last=False)
    _turn_list().append(trace)


def get_trace(observation_id: str) -> Optional["ToolTrace"]:
    """Return the stored ToolTrace for an observation_id, or None."""
    return _TRACES.get(observation_id)


def reset_turn_traces() -> None:
    """Clear the per-turn trace list. Call at the start of a conversation turn."""
    _TURN_TRACES.set([])


def current_turn_traces() -> List["ToolTrace"]:
    """Return a shallow copy of the traces recorded since the last reset."""
    return list(_TURN_TRACES.get() or [])


def convert_scratchpad_to_think(content: str) -> str:
    """Convert <REASONING_SCRATCHPAD> tags to <think> tags."""
    if not content or "<REASONING_SCRATCHPAD>" not in content:
        return content
    return content.replace("<REASONING_SCRATCHPAD>", "<think>").replace("</REASONING_SCRATCHPAD>", "</think>")


def has_incomplete_scratchpad(content: str) -> bool:
    """Check if content has an opening <REASONING_SCRATCHPAD> without a closing tag."""
    if not content:
        return False
    return "<REASONING_SCRATCHPAD>" in content and "</REASONING_SCRATCHPAD>" not in content


def save_trajectory(trajectory: List[Dict[str, Any]], model: str,
                    completed: bool, filename: str = None):
    """Append a trajectory entry to a JSONL file.

    Args:
        trajectory: The ShareGPT-format conversation list.
        model: Model name for metadata.
        completed: Whether the conversation completed successfully.
        filename: Override output filename. Defaults to trajectory_samples.jsonl
                  or failed_trajectories.jsonl based on ``completed``.
    """
    if filename is None:
        filename = "trajectory_samples.jsonl" if completed else "failed_trajectories.jsonl"

    entry = {
        "conversations": trajectory,
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "completed": completed,
    }

    try:
        with open(filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("Trajectory saved to %s", filename)
    except Exception as e:
        logger.warning("Failed to save trajectory: %s", e)
