import json
import threading
from types import SimpleNamespace

from agent.tool_executor import execute_tool_calls_sequential
from agent.agent_runtime_helpers import invoke_tool


class _AllowGuardrails:
    def before_call(self, function_name, function_args):
        return SimpleNamespace(allows_execution=True)


class _BlockGuardrails:
    def before_call(self, function_name, function_args):
        return SimpleNamespace(allows_execution=False, message="blocked by test guardrail")


class FakeAgent:
    def __init__(self):
        self._interrupt_requested = False
        self._current_tool = None
        self._current_turn_id = "turn-1"
        self._current_api_request_id = "api-1"
        self.session_id = "session-1"
        self.quiet_mode = True
        self.verbose_logging = False
        self.log_prefix = ""
        self.log_prefix_chars = 120
        self.valid_tool_names = {"multi_agent_orchestrate"}
        self.enabled_toolsets = ["delegation"]
        self.disabled_toolsets = None
        self._delegate_spinner = None
        self._context_engine_tool_names = set()
        self._memory_manager = None
        self.tool_progress_callback = None
        self.tool_complete_callback = None
        self._subdirectory_hints = SimpleNamespace(check_tool_call=lambda *_: "")
        self._tool_guardrails = _AllowGuardrails()
        self._checkpoint_mgr = SimpleNamespace(enabled=False)
        self.tool_start_callback = None
        self.tool_delay = 0
        self._tool_worker_threads_lock = threading.Lock()
        self._tool_worker_threads = set()

    def _should_emit_quiet_tool_messages(self):
        return False

    def _should_start_quiet_spinner(self):
        return False

    def _print_fn(self, *args, **kwargs):
        pass

    def _vprint(self, *args, **kwargs):
        pass

    def _append_guardrail_observation(self, function_name, function_args, result, failed=False, **kwargs):
        return result

    def _guardrail_block_result(self, decision):
        return json.dumps({"error": decision.message})

    def _record_file_mutation_result(self, *args, **kwargs):
        pass

    def _touch_activity(self, *args, **kwargs):
        pass

    def _tool_result_content_for_active_model(self, function_name, result):
        return result

    def _apply_pending_steer_to_tool_results(self, messages, count):
        pass


def test_multi_agent_orchestrate_executor_passes_parent_agent(monkeypatch):
    captured = {}

    def fake_multi_agent_orchestrate(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "APPROVED"})

    monkeypatch.setattr("tools.multi_agent_tool.multi_agent_orchestrate", fake_multi_agent_orchestrate)

    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="multi_agent_orchestrate",
            arguments=json.dumps(
                {
                    "objective": "Bearbeite Issue #1",
                    "task_context": {"issue_id": "#1"},
                    "debugger_toolsets": ["terminal"],
                }
            ),
        ),
    )
    assistant_message = SimpleNamespace(tool_calls=[tool_call])
    messages = []
    agent = FakeAgent()

    execute_tool_calls_sequential(agent, assistant_message, messages, effective_task_id="task-1")

    assert captured["parent_agent"] is agent
    assert captured["objective"] == "Bearbeite Issue #1"
    assert captured["debugger_toolsets"] == ["terminal"]
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "call-1"


def test_multi_agent_orchestrate_executor_honors_guardrail_block(monkeypatch):
    called = False

    def fake_multi_agent_orchestrate(**kwargs):
        nonlocal called
        called = True
        return json.dumps({"status": "APPROVED"})

    monkeypatch.setattr("tools.multi_agent_tool.multi_agent_orchestrate", fake_multi_agent_orchestrate)

    tool_call = SimpleNamespace(
        id="call-guardrail",
        function=SimpleNamespace(
            name="multi_agent_orchestrate",
            arguments=json.dumps({"objective": "blocked workflow"}),
        ),
    )
    messages = []
    agent = FakeAgent()
    agent._tool_guardrails = _BlockGuardrails()

    execute_tool_calls_sequential(
        agent,
        SimpleNamespace(tool_calls=[tool_call]),
        messages,
        effective_task_id="task-guardrail",
    )

    assert called is False
    result = json.loads(messages[-1]["content"])
    assert result["error"] == "blocked by test guardrail"


def test_multi_agent_invoke_tool_passes_parent_agent(monkeypatch):
    captured = {}

    def fake_multi_agent_orchestrate(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "APPROVED"})

    monkeypatch.setattr("tools.multi_agent_tool.multi_agent_orchestrate", fake_multi_agent_orchestrate)
    agent = FakeAgent()

    result = invoke_tool(
        agent,
        "multi_agent_orchestrate",
        {
            "objective": "Bearbeite Issue #2",
            "task_context": {"issue_id": "#2"},
            "tester_toolsets": ["terminal"],
        },
        "task-2",
        tool_call_id="call-2",
        pre_tool_block_checked=True,
    )

    assert json.loads(result)["status"] == "APPROVED"
    assert captured["parent_agent"] is agent
    assert captured["objective"] == "Bearbeite Issue #2"
    assert captured["tester_toolsets"] == ["terminal"]
