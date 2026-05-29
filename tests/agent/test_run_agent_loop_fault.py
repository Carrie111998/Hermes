import re
from pathlib import Path


def test_run_agent_imports_and_calls_loop_fault_at_abort():
    # The loop abort branch was extracted from run_agent.py to
    # agent/conversation_loop.py in the v0.15.x monolith→module refactor.
    src = Path(__file__).resolve().parents[2] / "agent" / "conversation_loop.py"
    text = src.read_text(encoding="utf-8", errors="replace")
    # The emit must be wired in the non-retryable abort branch.
    assert "emit_agent_loop_fault" in text, "SR-471 emit not wired into conversation_loop"
    # It must be a lazy import (conversation_loop has no top-level events import).
    assert re.search(r"from events\.loop_fault import emit_agent_loop_fault", text)
    # It must be guarded so alerting can never break the loop.
    idx = text.index("emit_agent_loop_fault(")
    window = text[idx - 400: idx + 400]
    assert "try:" in window and ("except" in window)
