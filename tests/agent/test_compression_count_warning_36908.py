"""Regression for #36908 + 4tp-2: the repeated-compression escalation must
reach the TUI / gateway, not just CLI stdout.

Originally (#36908) the "compressed N times — accuracy may degrade" warning
went through ``_vprint`` (stdout only), so the Ink TUI / Telegram / Discord
never saw it. It was moved onto ``_emit_status`` — but the gateway noise
filter (gateway/run.py) then re-suppressed that exact phrasing on chat
surfaces, so on Telegram the repeated-compaction signal was silently dropped
again (Sven's "quality craters after a double compaction with no marker").

4tp-2 folds the escalation into the delivered post-compaction caveat, which is
worded to pass the gateway noise filter. This pins that:
  * every successful compaction emits a delivered notice via ``_emit_status``;
  * from the 2nd compaction on, that notice carries the degradation escalation;
  * the escalation is absent on the 1st compaction.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB


def _build_agent_with_db(db: SessionDB, session_id: str, compression_count: int):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )

    compressor = MagicMock()
    compressor.compress.return_value = [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
        {"role": "user", "content": "tail"},
    ]
    compressor.compression_count = compression_count
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    return agent


def test_repeated_compression_escalation_reaches_emit_status(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "PARENT_36908"
    db.create_session(sid, source="cli")

    # compression_count == 2 → the delivered caveat carries the escalation.
    agent = _build_agent_with_db(db, sid, compression_count=2)

    emitted: list[str] = []
    agent._emit_warning = lambda message: emitted.append(message)

    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    agent._compress_context(messages, "sys", approx_tokens=120_000)

    # A delivered compaction caveat reached the gateway-aware channel...
    caveats = [m for m in emitted if "compacted" in m.lower()]
    assert caveats, f"no delivered compaction caveat via _emit_status: {emitted}"
    # ...and it escalates on repeated compaction (worded to pass the gateway
    # noise filter — NOT the old "compressed N times" phrasing it dropped).
    joined = " ".join(caveats).lower()
    assert "2×" in joined
    assert "degrad" in joined

    # Sanity: the escalation is worded so the Telegram noise filter lets it
    # through, unlike the old "Session compressed N times" status.
    from gateway.run import _prepare_gateway_status_message
    from gateway.config import Platform

    assert _prepare_gateway_status_message(Platform.TELEGRAM, "warn", caveats[0]) is not None


def test_first_compaction_caveat_has_no_repeat_escalation(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "PARENT_36908_ONCE"
    db.create_session(sid, source="cli")

    # compression_count == 1 → the caveat fires but without the repeat escalation.
    agent = _build_agent_with_db(db, sid, compression_count=1)
    emitted: list[str] = []
    agent._emit_warning = lambda message: emitted.append(message)

    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    agent._compress_context(messages, "sys", approx_tokens=120_000)

    caveats = [m for m in emitted if "compacted" in m.lower()]
    assert caveats, f"no delivered compaction caveat via _emit_status: {emitted}"
    joined = " ".join(caveats).lower()
    assert "×" not in joined
    assert "degrad" not in joined
