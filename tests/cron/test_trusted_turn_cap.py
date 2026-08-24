"""Installed cron execution must enforce a strict per-job turn cap."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from cron.scheduler import run_job


class _Model(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length))
        if "messages" in request:
            type(self).calls += 1
        number = type(self).calls
        response = {
            "id": f"turn-{number}",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": f"call-{number}", "type": "function",
                    "function": {"name": "todo", "arguments": "{}"},
                }],
            }, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_real_cron_run_stops_after_twelve_model_calls(tmp_path, monkeypatch):
    _Model.calls = 0
    server = HTTPServer(("127.0.0.1", 0), _Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("agent:\n  max_turns: 99\n")
    runtime = {
        "api_key": "test-key",
        "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
        "provider": "openai-compat",
        "api_mode": "chat_completions",
    }
    job = {
        "id": "strict-twelve", "name": "strict twelve", "prompt": "work",
        "max_turns": 12, "enabled_toolsets": ["todo"],
    }
    try:
        with patch("cron.scheduler._hermes_home", tmp_path), patch(
            "cron.scheduler._resolve_origin", return_value=None,
        ), patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=runtime,
        ), patch("hermes_cli.env_loader.load_hermes_dotenv"), patch(
            "hermes_cli.env_loader.reset_secret_source_cache",
        ):
            success, _output, _final, error = run_job(job)
    finally:
        server.shutdown()
        thread.join()

    assert success is False
    assert error
    assert _Model.calls == 12
