from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent.turn_router_budget import TurnRouterBudgetLedger
from agent.turn_routing_runtime import TurnRoutingRequest
from run_agent import AIAgent


class _ProviderHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    empty_stream = False

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).requests.append(request)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [] if type(self).empty_stream else [
            {
                "id": "provider-response-1",
                "model": "grok-4.5",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "ok"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "provider-response-1",
                "model": "grok-4.5",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            },
        ]
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_real_user_turn_commits_core_budget_once_at_provider_acceptance(tmp_path):
    _ProviderHandler.requests = []
    _ProviderHandler.empty_stream = False
    server = HTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    events = []
    agent = AIAgent(
        api_key="test-key",
        base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
        provider="xai",
        model="grok-4.5",
        api_mode="chat_completions",
        max_iterations=3,
        enabled_toolsets=[],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        save_trajectories=False,
        platform="cli",
    )
    request = TurnRoutingRequest(
        surface="cli",
        session_id="session-budget-e2e",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "xai",
            "model": "grok-4.5",
        },
        config_loader=lambda: {
            "mode": "off",
            "budget": {
                "grok_weekly_limit": 1,
                "reservation_lease_seconds": 300,
                "cooldown_seconds": 60,
            },
        },
        emit=lambda event, payload: events.append((event, payload)),
    )

    try:
        result = agent.run_conversation(
            "hello",
            conversation_history=[],
            turn_routing_request=request,
        )
    finally:
        server.shutdown()
        server.server_close()
        agent.close()

    assert result["completed"] is True
    assert result["final_response"] == "ok"
    provider_requests = [request for request in _ProviderHandler.requests if "messages" in request]
    assert len(provider_requests) == 1
    status = TurnRouterBudgetLedger(weekly_limit=1).status()
    assert status.reserved_slots == 0
    assert status.committed_slots == 1
    assert [event for event, _payload in events] == [
        "route.decided",
        "route.completed",
    ]
    decided = events[0][1]
    completed = events[-1][1]
    assert decided["authorization"]["reservation_id"]
    assert completed["budget_state"] == "committed"
    assert completed["provider_submission_id"].endswith(":api:1")


def test_provider_accepted_empty_stream_still_consumes_budget(tmp_path):
    _ProviderHandler.requests = []
    _ProviderHandler.empty_stream = True
    server = HTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    agent = AIAgent(
        api_key="test-key",
        base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
        provider="xai",
        model="grok-4.5",
        api_mode="chat_completions",
        max_iterations=1,
        enabled_toolsets=[],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        save_trajectories=False,
        platform="cli",
    )
    request = TurnRoutingRequest(
        surface="cli",
        session_id="session-empty-stream",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "xai",
            "model": "grok-4.5",
        },
        config_loader=lambda: {
            "mode": "off",
            "budget": {
                "grok_weekly_limit": 1,
                "reservation_lease_seconds": 300,
                "cooldown_seconds": 60,
            },
        },
    )

    try:
        agent.run_conversation(
            "hello",
            conversation_history=[],
            turn_routing_request=request,
        )
    finally:
        server.shutdown()
        server.server_close()
        agent.close()
        _ProviderHandler.empty_stream = False

    assert _ProviderHandler.requests
    ledger = TurnRouterBudgetLedger(weekly_limit=1)
    status = ledger.status()
    assert status.reserved_slots == 0
    assert status.committed_slots == 1
    assert [row.state for row in ledger.audit_rows()] == ["reserved", "committed"]
