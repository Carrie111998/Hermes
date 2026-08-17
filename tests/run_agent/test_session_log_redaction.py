"""Regression tests for credential capture in _save_session_log.

Phase 9 / Packet B3.

This is the dominant persistence surface: 1514 session_*.json files (~400 MB)
at the time of writing, still growing. Every message the agent sees was
serialized verbatim, along with the system prompt and the tool definitions.
"""

import json

from tests.run_agent.test_run_agent_codex_responses import _patch_agent_bootstrap

import run_agent


CANARY_PREFIXED = "sk-proj-CANARYaaaabbbbccccddddeeeeffff"
CANARY_OPAQUE = "Zq7Z4mKp2Wf9Lx3Rv8Tn1Yb6Hd5Gs0Jc"


def _agent(monkeypatch, tmp_path):
    _patch_agent_bootstrap(monkeypatch)
    agent = run_agent.AIAgent(
        model="gpt-4o",
        base_url="http://127.0.0.1:9208/v1",
        api_key="test-key",
        quiet_mode=True,
        max_iterations=1,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.logs_dir = tmp_path
    agent.session_log_file = tmp_path / "session_test.json"
    return agent


class TestMessageContent:
    def test_recognisable_credential_in_message_redacted(self, monkeypatch, tmp_path):
        agent = _agent(monkeypatch, tmp_path)
        agent._save_session_log([
            {"role": "user", "content": f"my key is {CANARY_PREFIXED}"},
        ])
        assert CANARY_PREFIXED not in agent.session_log_file.read_text()

    def test_sensitive_key_in_tool_call_arguments_redacted(self, monkeypatch, tmp_path):
        """Tool-call arguments are a common credential carrier."""
        agent = _agent(monkeypatch, tmp_path)
        agent._save_session_log([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "http_request",
                            "arguments": {"url": "https://api.example.com", "api_key": CANARY_OPAQUE},
                        },
                    }
                ],
            },
        ])
        assert CANARY_OPAQUE not in agent.session_log_file.read_text()

    def test_message_structure_preserved(self, monkeypatch, tmp_path):
        """Redaction must not destroy the record -- these files are read back
        by humans reconstructing what an agent did."""
        agent = _agent(monkeypatch, tmp_path)
        agent._save_session_log([
            {"role": "user", "content": "what is the weather"},
            {"role": "assistant", "content": "let me check"},
        ])
        payload = json.loads(agent.session_log_file.read_text())
        assert payload["message_count"] == 2
        assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
        assert payload["messages"][0]["content"] == "what is the weather"
        assert payload["messages"][1]["content"] == "let me check"


class TestNonMessageFields:
    """system_prompt and tools carry credentials too, and were not named in
    the original incident brief."""

    def test_system_prompt_redacted(self, monkeypatch, tmp_path):
        agent = _agent(monkeypatch, tmp_path)
        agent._cached_system_prompt = f"You have access to key {CANARY_PREFIXED}"
        agent._save_session_log([{"role": "user", "content": "hi"}])
        assert CANARY_PREFIXED not in agent.session_log_file.read_text()

    def test_tools_redacted(self, monkeypatch, tmp_path):
        agent = _agent(monkeypatch, tmp_path)
        agent.tools = [
            {
                "type": "function",
                "function": {
                    "name": "call_api",
                    "description": f"Uses {CANARY_PREFIXED} to authenticate",
                    "parameters": {"api_key": CANARY_OPAQUE},
                },
            }
        ]
        agent._save_session_log([{"role": "user", "content": "hi"}])
        raw = agent.session_log_file.read_text()
        assert CANARY_PREFIXED not in raw
        assert CANARY_OPAQUE not in raw


class TestLiveStateUnaffected:
    def test_in_memory_messages_not_mutated(self, monkeypatch, tmp_path):
        """The agent keeps using these messages after the log is written. If
        redaction mutated them, the running agent would lose the credential it
        legitimately holds in context and the task would break."""
        agent = _agent(monkeypatch, tmp_path)
        messages = [{"role": "user", "content": f"my key is {CANARY_PREFIXED}"}]
        agent._save_session_log(messages)
        assert messages[0]["content"] == f"my key is {CANARY_PREFIXED}"

    def test_cached_system_prompt_not_mutated(self, monkeypatch, tmp_path):
        agent = _agent(monkeypatch, tmp_path)
        agent._cached_system_prompt = f"key {CANARY_PREFIXED}"
        agent._save_session_log([{"role": "user", "content": "hi"}])
        assert agent._cached_system_prompt == f"key {CANARY_PREFIXED}"


class TestOverwriteGuardStillWorks:
    def test_smaller_log_does_not_clobber_larger(self, monkeypatch, tmp_path):
        """Pre-existing data-loss guard (compares message_count) must survive
        the change -- redaction does not alter message counts."""
        agent = _agent(monkeypatch, tmp_path)
        agent._save_session_log([
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])
        agent._save_session_log([{"role": "user", "content": "only one"}])

        payload = json.loads(agent.session_log_file.read_text())
        assert payload["message_count"] == 3, "guard should have refused the overwrite"


class TestKnownLimitation:
    def test_opaque_credential_in_conversation_text_survives(self, monkeypatch, tmp_path):
        """Measured residual, asserted so it stays visible.

        Session logs cannot use the request dumps' projection defense -- they
        exist to preserve conversation content. So an opaque credential in no
        known vendor format, sitting in ordinary prose under an innocuous key,
        is still written. The fix is upstream (do not read credentials into
        agent context); the existing corpus is Phase E's problem.
        """
        agent = _agent(monkeypatch, tmp_path)
        agent._save_session_log([
            {"role": "user", "content": f"the value is {CANARY_OPAQUE}"},
        ])
        assert CANARY_OPAQUE in agent.session_log_file.read_text()
