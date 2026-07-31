"""Unit tests for hermes_cli.oneshot_guard (opt-in oneshot output guards)."""

from hermes_cli.oneshot_guard import (
    GuardConfig,
    has_code_final,
    load_guard_config,
    run_guarded,
    verify_answer,
)


class FakeAgent:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self._session_messages = [{"role": "user", "content": "task"}]

    def run_conversation(self, message, conversation_history=None, **kwargs):
        self.calls.append(message)
        return {"final_response": self.replies.pop(0)}


def test_defaults_are_inert():
    gcfg = load_guard_config({})
    assert not gcfg.active
    assert not gcfg.forbid_code_final and not gcfg.verifier_enabled


def test_load_guard_config_parses_nested_keys():
    gcfg = load_guard_config({
        "oneshot": {
            "forbid_code_final": True,
            "finalize_retries": 1,
            "verifier": {"enabled": True, "model": "m", "max_retries": 2},
        }
    })
    assert gcfg.active and gcfg.forbid_code_final
    assert gcfg.finalize_retries == 1
    assert gcfg.verifier_enabled and gcfg.verifier_model == "m"
    assert gcfg.verifier_max_retries == 2


def test_has_code_final():
    assert has_code_final("done\n```python\nprint(1)\n```")
    assert has_code_final("```\nls -la\n```")
    assert not has_code_final("the answer is 0.42, computed via `df.mean()`")


def test_contract_enforcement_retries_until_clean():
    agent = FakeAgent(["```python\nprint(1)\n```", "```\nstill code\n```", "FINAL: 42"])
    gcfg = GuardConfig(forbid_code_final=True, finalize_retries=2)
    response, result = run_guarded(agent, "task", gcfg)
    assert response == "FINAL: 42"
    assert result["hw_guard"]["finalize_turns"] == 2
    assert result["hw_guard"]["contract_violated_at_end"] is False


def test_contract_gives_up_after_budget():
    agent = FakeAgent(["```\na\n```", "```\nb\n```", "```\nc\n```"])
    gcfg = GuardConfig(forbid_code_final=True, finalize_retries=2)
    response, result = run_guarded(agent, "task", gcfg)
    assert result["hw_guard"]["contract_violated_at_end"] is True


def test_verifier_fail_triggers_feedback_retry(monkeypatch):
    import hermes_cli.oneshot_guard as G

    verdicts = iter([
        {"status": "FAIL", "issues": ["missing part 2"]},
        {"status": "PASS", "issues": []},
    ])
    monkeypatch.setattr(G, "verify_answer", lambda *a, **k: next(verdicts))
    agent = FakeAgent(["first answer", "corrected answer"])
    gcfg = GuardConfig(verifier_enabled=True, verifier_model="m", verifier_max_retries=1)
    response, result = run_guarded(agent, "task", gcfg)
    assert response == "corrected answer"
    assert [v["status"] for v in result["hw_guard"]["verifications"]] == ["FAIL", "PASS"]
    assert "missing part 2" in agent.calls[1]


def test_verifier_fails_open_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    verdict = verify_answer("task", "answer", GuardConfig(verifier_enabled=True, verifier_model="m"))
    assert verdict["status"] == "PASS" and verdict.get("verifier_error")


def test_guard_survives_missing_session_history():
    agent = FakeAgent(["```\ncode\n```"])
    agent._session_messages = []  # continuation impossible
    gcfg = GuardConfig(forbid_code_final=True, finalize_retries=2)
    response, result = run_guarded(agent, "task", gcfg)
    assert result["hw_guard"]["finalize_aborted"]
