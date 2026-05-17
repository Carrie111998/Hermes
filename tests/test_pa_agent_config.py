"""Live agent-config bridge tests.

Exercises the PS-spine agent_config accessors in ``tools.pa_business_tools``:
the per-key read, the full map, the decision-turn prompt block, and the
fail-soft behavior when the bridge is inactive or the call fails.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools.pa_business_tools import (
    fetch_agent_config_map,
    fetch_agent_config_view,
    read_agent_config,
    render_agent_config_prompt,
)


# PS-spine-shaped GET response: {ok, data: {config, directives, keys}}.
_AGENT_CONFIG_RESPONSE = {
    "ok": True,
    "data": {
        "config": {
            "behavior.tone": "brief",
            "behavior.verbosity": "normal",
            "escalation.contact_threshold": 24,
        },
        "directives": [
            "Reply tone: BRIEF — keep replies short and to the point.",
            "Verbosity: NORMAL — include the key supporting detail.",
            "Escalation threshold: 24h — escalate after 24 hours of silence.",
        ],
        "keys": [
            {"key": "behavior.tone", "value": "brief", "isDefault": False},
        ],
    },
}


class _FakeAgentConfigHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(_AGENT_CONFIG_RESPONSE).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


@pytest.fixture
def fake_agent_config_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAgentConfigHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/api/operator/agent-config"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _config_with(url: str) -> dict:
    """A bridge config exposing the agent_config_read GET operation."""
    return {
        "pa_business": {
            "operations": {
                "agent_config_read": {
                    "type": "http",
                    "method": "GET",
                    "url": url,
                }
            }
        }
    }


def test_read_agent_config_returns_a_value(fake_agent_config_endpoint):
    config = _config_with(fake_agent_config_endpoint)
    assert read_agent_config("behavior.tone", config) == "brief"
    assert read_agent_config("escalation.contact_threshold", config) == 24


def test_read_agent_config_unknown_key_is_none(fake_agent_config_endpoint):
    config = _config_with(fake_agent_config_endpoint)
    assert read_agent_config("behavior.nonsense", config) is None


def test_fetch_agent_config_map_returns_full_map(fake_agent_config_endpoint):
    config = _config_with(fake_agent_config_endpoint)
    cfg = fetch_agent_config_map(config)
    assert cfg == {
        "behavior.tone": "brief",
        "behavior.verbosity": "normal",
        "escalation.contact_threshold": 24,
    }


def test_fetch_agent_config_view_carries_directives(fake_agent_config_endpoint):
    view = fetch_agent_config_view(_config_with(fake_agent_config_endpoint))
    assert isinstance(view.get("directives"), list)
    assert len(view["directives"]) == 3


def test_render_agent_config_prompt_builds_behavior_block(fake_agent_config_endpoint):
    block = render_agent_config_prompt(_config_with(fake_agent_config_endpoint))
    assert block.startswith("## Live Behavior Configuration")
    # Every directive lands as a bullet the agent applies on its next turn.
    assert "Reply tone: BRIEF" in block
    assert "Escalation threshold: 24h" in block
    assert block.count("\n- ") == 3


def test_inactive_bridge_yields_empty_results():
    # No operations configured → bridge inactive → fail soft everywhere.
    empty: dict = {}
    assert read_agent_config("behavior.tone", empty) is None
    assert fetch_agent_config_map(empty) == {}
    assert render_agent_config_prompt(empty) == ""


def test_operation_absent_yields_empty_results(fake_agent_config_endpoint):
    # A bridge with other operations but no agent_config_read → fail soft.
    config = {
        "pa_business": {
            "operations": {
                "some_other_op": {
                    "type": "http",
                    "method": "GET",
                    "url": fake_agent_config_endpoint,
                }
            }
        }
    }
    assert read_agent_config("behavior.tone", config) is None
    assert render_agent_config_prompt(config) == ""


def test_failed_call_yields_empty_results():
    # A configured operation pointing at a dead port → call fails → fail soft.
    config = _config_with("http://127.0.0.1:1/api/operator/agent-config")
    assert read_agent_config("behavior.tone", config) is None
    assert fetch_agent_config_map(config) == {}
    assert render_agent_config_prompt(config) == ""
