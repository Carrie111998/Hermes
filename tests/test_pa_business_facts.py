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
    record_agent_action,
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


def _agent_action_config(url: str) -> dict:
    return {
        "pa_business": {
            "operations": {
                "agent_action_record": {
                    "type": "http",
                    "url": url,
                    "method": "POST",
                }
            }
        }
    }


def test_record_agent_action_observation_type(fake_business_endpoint):
    ok = record_agent_action(
        agent_id="iris",
        engagement_id="00000000-0000-0000-0000-000000000001",
        action_type="observation",
        payload={"incoming_message": "hello"},
        source="whatsapp",
        turn_id="turn-1",
        config=_agent_action_config(fake_business_endpoint),
    )

    assert ok is True
    assert _FakeBusinessHandler.received["payload"] == {
        "agent_id": "iris",
        "engagement_id": "00000000-0000-0000-0000-000000000001",
        "action_type": "observation",
        "payload": {"incoming_message": "hello"},
        "source": "whatsapp",
        "cost_usd": 0.0,
        "tokens_input": 0,
        "tokens_output": 0,
        "status": "pending",
        "turn_id": "turn-1",
    }


def test_record_agent_action_dry_run_reply_type(fake_business_endpoint):
    ok = record_agent_action(
        agent_id="iris",
        engagement_id="00000000-0000-0000-0000-000000000002",
        action_type="dry-run-reply",
        payload={"reply": "draft only"},
        source="telegram",
        cost_usd=0.123456,
        tokens_input=321,
        tokens_output=45,
        status="dry-run",
        turn_id="turn-2",
        config=_agent_action_config(fake_business_endpoint),
    )

    assert ok is True
    received = _FakeBusinessHandler.received["payload"]
    assert received["action_type"] == "dry-run-reply"
    assert received["status"] == "dry-run"
    assert received["payload"] == {"reply": "draft only"}
    assert received["cost_usd"] == 0.123456
    assert received["tokens_input"] == 321
    assert received["tokens_output"] == 45


def test_record_agent_action_executed_reply_type(fake_business_endpoint):
    ok = record_agent_action(
        agent_id="iris",
        engagement_id="00000000-0000-0000-0000-000000000003",
        action_type="executed-reply",
        payload={"reply": "sent"},
        source="whatsapp",
        status="executed",
        turn_id="turn-3",
        config=_agent_action_config(fake_business_endpoint),
    )

    assert ok is True
    received = _FakeBusinessHandler.received["payload"]
    assert received["action_type"] == "executed-reply"
    assert received["status"] == "executed"
    assert received["payload"] == {"reply": "sent"}


def test_record_agent_action_photo_pair_classified_type(fake_business_endpoint):
    ok = record_agent_action(
        agent_id="bobby",
        engagement_id="00000000-0000-0000-0000-000000000006",
        action_type="photo-pair-classified",
        payload={
            "before": {"file_id": "before-file", "getFile_url": "https://files/before.jpg"},
            "after": {"file_id": "after-file", "getFile_url": "https://files/after.jpg"},
            "confidence": 0.96,
            "classified_at": "2026-05-18T12:00:31Z",
        },
        source="whatsapp",
        status="executed",
        turn_id="turn-photo-pair",
        config=_agent_action_config(fake_business_endpoint),
    )

    assert ok is True
    received = _FakeBusinessHandler.received["payload"]
    assert received["action_type"] == "photo-pair-classified"
    assert received["status"] == "executed"
    assert received["payload"]["before"]["file_id"] == "before-file"
    assert received["payload"]["after"]["getFile_url"] == "https://files/after.jpg"


def test_record_agent_action_fails_soft_when_bridge_unavailable():
    ok = record_agent_action(
        agent_id="iris",
        engagement_id="00000000-0000-0000-0000-000000000004",
        action_type="observation",
        payload={"incoming_message": "hello"},
        config={"pa_business": {"operations": {}}},
    )

    assert ok is False


def test_record_agent_action_agent_id_passed_verbatim(fake_business_endpoint):
    ok = record_agent_action(
        agent_id="Iris V1",
        engagement_id="00000000-0000-0000-0000-000000000005",
        action_type="observation",
        payload={"incoming_message": "hello"},
        config=_agent_action_config(fake_business_endpoint),
    )

    assert ok is True
    assert _FakeBusinessHandler.received["payload"]["agent_id"] == "Iris V1"


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


class _PathParamHandler(BaseHTTPRequestHandler):
    """Records path + payload for any method, useful for path-param tests."""

    last_request: dict = {}

    def _record(self, body_bytes: bytes) -> None:
        try:
            payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except json.JSONDecodeError:
            payload = {"__raw": body_bytes.decode("utf-8", errors="replace")}
        type(self).last_request = {
            "method": self.command,
            "path": self.path,
            "payload": payload,
            "authorization": self.headers.get("Authorization"),
            "ps_tenant": self.headers.get("X-PS-Tenant"),
        }
        body = json.dumps({"ok": True, "echoPath": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self._record(self.rfile.read(length))

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", "0"))
        self._record(self.rfile.read(length))

    def log_message(self, _format, *_args):
        return


@pytest.fixture
def path_param_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PathParamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_path_param_interpolation_get(path_param_endpoint):
    """GET with one path param + remaining payload becomes query string."""
    config = {
        "pa_business": {
            "operations": {
                "case_lookup": {
                    "type": "http",
                    "method": "GET",
                    "url": f"{path_param_endpoint}/api/operator/cases/{{jobNo}}",
                    "path_params": ["jobNo"],
                    "headers": {"X-PS-Tenant": "tgg"},
                }
            }
        }
    }

    result = execute_business_operation(
        config, "case_lookup", {"jobNo": "AM/JOB/2605/0112"}
    )

    assert result["ok"] is True
    last = _PathParamHandler.last_request
    assert last["method"] == "GET"
    # Slashes in jobNo must be percent-encoded (safe="").
    assert last["path"] == "/api/operator/cases/AM%2FJOB%2F2605%2F0112"
    assert last["payload"] == {}
    assert last["ps_tenant"] == "tgg"


def test_path_param_interpolation_patch_keeps_remaining_payload_in_body(path_param_endpoint):
    """PATCH with path param: jobNo goes in URL, remaining payload becomes JSON body."""
    config = {
        "pa_business": {
            "operations": {
                "case_state_update": {
                    "type": "http",
                    "method": "PATCH",
                    "url": f"{path_param_endpoint}/api/operator/cases/{{jobNo}}/state",
                    "path_params": ["jobNo"],
                }
            }
        }
    }

    result = execute_business_operation(
        config,
        "case_state_update",
        {"jobNo": "BS/JOB/2605/0087", "state": "completed"},
    )

    assert result["ok"] is True
    last = _PathParamHandler.last_request
    assert last["method"] == "PATCH"
    assert last["path"] == "/api/operator/cases/BS%2FJOB%2F2605%2F0087/state"
    # jobNo was popped from payload; only state remains in the body.
    assert last["payload"] == {"state": "completed"}


def test_path_param_missing_from_payload_fails_loudly(path_param_endpoint):
    config = {
        "pa_business": {
            "operations": {
                "case_lookup": {
                    "type": "http",
                    "method": "GET",
                    "url": f"{path_param_endpoint}/api/operator/cases/{{jobNo}}",
                    "path_params": ["jobNo"],
                }
            }
        }
    }
    with pytest.raises(ValueError, match="requires path_param 'jobNo'"):
        execute_business_operation(config, "case_lookup", {})


def test_path_param_without_placeholder_fails_loudly(path_param_endpoint):
    config = {
        "pa_business": {
            "operations": {
                "broken": {
                    "type": "http",
                    "method": "GET",
                    "url": f"{path_param_endpoint}/api/operator/cases",
                    "path_params": ["jobNo"],
                }
            }
        }
    }
    with pytest.raises(ValueError, match="URL has no \\{jobNo\\} placeholder"):
        execute_business_operation(config, "broken", {"jobNo": "X"})


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
