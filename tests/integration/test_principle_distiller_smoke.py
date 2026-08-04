"""Smoke tests for the principle distiller integration.

Two minimal end-to-end checks through the real conversation loop
(``run_agent.AIAgent.run_conversation``, i.e. conversation_loop.py) against
an in-process mock provider:

- ENABLED: a single clean turn with ``HERMES_PRINCIPLE_DISTILLER=1`` and a
  seeded principle store produces a distilled principle — the distilled text
  is appended to ``final_response`` and the record is persisted (source
  ``self-distilled``) — and the gate flag is stashed on the agent.
- DISABLED: the same turn with the feature off is byte-identical to the
  model output: no gate stash, no principle injection on the wire, and the
  store keeps exactly the seeded record.

These are deliberately thin (one turn each) so the pair runs in seconds as a
sanity gate; the exhaustive behavior matrix lives in
``test_conversation_loop_distiller_enabled.py`` and
``test_conversation_loop_distiller_robustness.py``. The fixtures/helpers are
imported from those sibling modules rather than re-implemented, so the smoke
layer never drifts from the harness the full suites exercise.
"""

from __future__ import annotations

# The sibling modules self-register the repo root, ~/.hermes/auto, and
# ~/.hermes on sys.path at import time, and alias the bare-name
# `principle_distiller`/`principle_repo` modules — so importing them here
# carries those same guarantees for this file.
from tests.integration.test_conversation_loop_distiller_enabled import (
    agent_env as agent_env_enabled,  # yields (agent, handler, store_path)
    _seed,
    _store_lines,
    _text_resp,
    _wire_user_contents,
    SEED_TEXT,
    TURN1_MSG,
)
from tests.integration.test_conversation_loop_distiller_robustness import (
    agent_env as agent_env_disabled,  # yields (agent, handler, store_path)
    MODEL_TEXT,
)


class TestPrincipleDistillerSmoke:
    def test_smoke_enabled_distills_appends_and_persists(self, agent_env_enabled):
        """Feature on: one turn produces a recorded distilled principle."""
        agent, handler, store_path = agent_env_enabled
        _seed(store_path)
        handler.response_queue.append(_text_resp(MODEL_TEXT))

        result = agent.run_conversation(TURN1_MSG, conversation_history=[], task_id="smoke-on")

        # Gate flag read once at turn start, and it is True.
        assert agent._principle_distiller_enabled is True
        # Model output preserved; distilled text appended to the response.
        distilled = [
            line for line in _store_lines(store_path)
            if line.get("source") == "self-distilled"
        ]
        assert len(distilled) == 1
        assert result["final_response"].startswith(MODEL_TEXT)
        assert result["final_response"].endswith(distilled[0]["text"])
        # The seeded principle reached the wire as context (W1 injection).
        assert any(SEED_TEXT in c for c in _wire_user_contents(handler))

    def test_smoke_disabled_no_behavioral_change(self, agent_env_disabled):
        """Feature off: response byte-identical, store and wire untouched."""
        agent, handler, store_path = agent_env_disabled
        _seed(store_path)
        handler.response_queue.append(_text_resp(MODEL_TEXT))

        result = agent.run_conversation(TURN1_MSG, conversation_history=[], task_id="smoke-off")

        # Gate flag read once at turn start, and it is False.
        assert agent._principle_distiller_enabled is False
        # A5: final response byte-identical to the model output.
        assert result["final_response"] == MODEL_TEXT
        # No stash attributes were ever written.
        assert not hasattr(agent, "_prev_turn_principle_hits")
        assert not hasattr(agent, "_prev_turn_principle_ids")
        # Store untouched: still exactly the seeded record.
        assert len(_store_lines(store_path)) == 1
        # The seed principle never reached the wire.
        assert not any(SEED_TEXT in c for c in _wire_user_contents(handler))
