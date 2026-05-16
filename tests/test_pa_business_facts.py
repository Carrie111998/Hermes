import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools.pa_business_tools import (
    TenantScopeMismatch,
    execute_business_operation,
    load_business_bridge_config,
)


class _FakeBusinessHandler(BaseHTTPRequestHandler):
    received = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).received = {
            "path": self.path,
            "payload": payload,
            "content_type": self.headers.get("Content-Type"),
            "tgg_token": self.headers.get("X-TGG-Token"),
            "mofex_token": self.headers.get("X-Mofex-Token"),
        }
        body = json.dumps({"ok": True, "echo": payload}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


@pytest.fixture
def fake_business_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBusinessHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/business"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_http_operation_calls_fake_endpoint_and_returns_json(fake_business_endpoint):
    config = {
        "pa_business": {
            "operations": {
                "lookup": {
                    "type": "http",
                    "url": fake_business_endpoint,
                    "method": "POST",
                }
            }
        }
    }

    result = execute_business_operation(config, "lookup", {"case_id": "C-123"})

    assert result["ok"] is True
    assert result["echo"] == {"case_id": "C-123"}
    assert result["status_code"] == 200
    assert _FakeBusinessHandler.received == {
        "path": "/business",
        "payload": {"case_id": "C-123"},
        "content_type": "application/json",
        "tgg_token": None,
        "mofex_token": None,
    }


def test_local_command_operation_returns_json():
    config = {
        "pa_business": {
            "operations": {
                "local_echo": {
                    "type": "command",
                    "command": [
                        sys.executable,
                        "-c",
                        (
                            "import json,sys; "
                            "payload=json.load(sys.stdin); "
                            "print(json.dumps({'ok': True, 'payload': payload}))"
                        ),
                    ],
                }
            }
        }
    }

    result = execute_business_operation(config, "local_echo", {"amount": 42})

    assert result == {"ok": True, "payload": {"amount": 42}}


def test_nested_pa_business_config_is_supported(fake_business_endpoint):
    config = {
        "pa": {
            "business": {
                "operations": {
                    "lookup": {
                        "type": "http",
                        "url": fake_business_endpoint,
                    }
                }
            }
        }
    }

    result = execute_business_operation(config, "lookup", {"case_id": "C-456"})

    assert result["ok"] is True
    assert result["echo"] == {"case_id": "C-456"}


def test_unknown_operation_fails_loudly():
    bridge = load_business_bridge_config({"pa_business": {"operations": {}}})

    with pytest.raises(ValueError, match="unknown PA business operation"):
        execute_business_operation(bridge, "missing", {})


def test_no_config_means_empty_inactive_bridge():
    bridge = load_business_bridge_config(None)

    assert bridge.operations == {}


def test_runtime_bridge_loads_raw_pa_business_config(monkeypatch, tmp_path, fake_business_endpoint):
    from tools.pa_business_tools import _load_runtime_bridge_config

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "\n".join(
            [
                "pa_business:",
                "  operations:",
                "    lookup:",
                "      type: http",
                f"      url: {fake_business_endpoint}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    bridge = _load_runtime_bridge_config()

    assert sorted(bridge.operations) == ["lookup"]


def test_bridge_module_does_not_import_or_call_hermes_state_writers():
    source = Path("tools/pa_business_tools.py").read_text(encoding="utf-8")

    forbidden_fragments = [
        "MemoryManager",
        "memory_tool",
        "state.db",
        "session_db",
        "save_message",
        "add_memory",
        "write_memory",
    ]
    assert not any(fragment in source for fragment in forbidden_fragments)


def test_pa_business_toolset_is_registered_without_all_tools():
    from toolsets import get_toolset, resolve_toolset

    toolset = get_toolset("pa-business")
    assert toolset is not None
    assert set(toolset["tools"]) == {"pa_business_read", "pa_business_write"}
    assert resolve_toolset("pa-business") == ["pa_business_read", "pa_business_write"]


def test_tenant_scoped_http_operation_injects_client_auth(fake_business_endpoint):
    from agent.pa_constitution import resolve_context

    constitution = {
        "id": "bobby",
        "agent_name": "Bobby",
        "identity": {"role": "assistant"},
        "client": {
            "name": "TGG",
            "tenant": "tgg",
            "business_bridge": {
                "auth": {
                    "type": "header",
                    "header": "X-TGG-Token",
                    "token": "tgg-secret",
                },
                "operations": {
                    "update_case": {
                        "type": "http",
                        "tenant": "tgg",
                        "url": fake_business_endpoint,
                    }
                },
            },
        },
        "job_briefs": {
            "ops": {"title": "Ops", "purpose": "Ops", "instructions": ["Do ops."]}
        },
    }
    pa_context = resolve_context({"constitution": constitution, "job_type": "ops"}, {})

    result = execute_business_operation(
        {"pa_business": {"operations": {}}},
        "update_case",
        {"case_id": "C-789"},
        pa_context=pa_context,
    )

    assert result["ok"] is True
    assert _FakeBusinessHandler.received["tgg_token"] == "tgg-secret"


def test_wrong_tenant_operation_fails_loudly(fake_business_endpoint):
    from agent.pa_constitution import resolve_context

    constitution = {
        "id": "bobby",
        "agent_name": "Bobby",
        "identity": {"role": "assistant"},
        "client": {
            "name": "TGG",
            "tenant": "tgg",
            "business_bridge": {
                "auth": {
                    "type": "header",
                    "header": "X-TGG-Token",
                    "token": "tgg-secret",
                },
                "operations": {
                    "mofex_lookup": {
                        "type": "http",
                        "tenant": "mofex",
                        "url": fake_business_endpoint,
                    }
                },
            },
        },
        "job_briefs": {
            "ops": {"title": "Ops", "purpose": "Ops", "instructions": ["Do ops."]}
        },
    }
    pa_context = resolve_context({"constitution": constitution, "job_type": "ops"}, {})

    with pytest.raises(TenantScopeMismatch, match="TENANT_SCOPE_MISMATCH"):
        execute_business_operation(
            {"pa_business": {"operations": {}}},
            "mofex_lookup",
            {"case_id": "M-1"},
            pa_context=pa_context,
        )
