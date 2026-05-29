"""SR-493 (ADR-0024 §4): library exceptions must not be bucketed as
non-retryable local validation errors. The loop body (incl. the
is_local_validation_error check) was extracted from run_agent.py to
agent/conversation_loop.py in the v0.15.x monolith→module refactor.
"""

import re
from pathlib import Path


def test_run_agent_excludes_library_exceptions_from_local_validation():
    src = Path(__file__).resolve().parents[2] / "agent" / "conversation_loop.py"
    text = src.read_text(encoding="utf-8", errors="replace")
    assert "is_library_exception" in text, "SR-493 not wired into conversation_loop"
    # Must be used to exclude library excs from the local-validation bucket.
    assert re.search(r"and not is_library_exception\(api_error\)", text)
