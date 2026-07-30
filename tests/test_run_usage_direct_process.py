from __future__ import annotations

import os
import subprocess
import sys
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.run_usage_ledger import UsageLedger


def test_direct_hermes_style_process_writes_receipt_without_card(tmp_path):
    code = """
from hermes_cli.lifecycle import invoke_hook
invoke_hook('on_session_start', session_id='direct-session', model='direct-model', provider='direct-provider', platform='cli')
invoke_hook('post_api_request', session_id='direct-session', turn_id='direct-turn', api_request_id='direct-api', model='direct-model', provider='direct-provider', usage={'input_tokens': 4, 'output_tokens': 3}, cost_usd=0.01)
invoke_hook('on_session_finalize', session_id='direct-session', completed=True, platform='cli')
"""
    env = {
        **os.environ,
        "HERMES_HOME": str(tmp_path),
        "HERMES_RUN_ID": "direct-process-run",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    subprocess.run([sys.executable, "-c", code], env=env, check=True)

    receipt = UsageLedger(tmp_path / "state.db").get_run("direct-process-run")
    assert receipt["task_id"] is None
    assert receipt["process_id"] != ""
    assert receipt["session_id"] == "direct-session"
    assert receipt["input_tokens"] == 4
    assert receipt["output_tokens"] == 3
    assert receipt["outcome"] == "completed"


def test_two_processes_resuming_one_session_keep_distinct_receipts(tmp_path):
    code = """
from hermes_cli.lifecycle import invoke_hook
invoke_hook('on_session_start', session_id='resumed', model='m', provider='p')
invoke_hook('post_api_request', session_id='resumed', turn_id='t', api_request_id='a', model='m', provider='p', usage={'input_tokens': 1, 'output_tokens': 1})
invoke_hook('on_session_finalize', session_id='resumed', completed=True)
"""
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    subprocess.run([sys.executable, "-c", code], env=env, check=True)
    subprocess.run([sys.executable, "-c", code], env=env, check=True)
    import sqlite3
    with sqlite3.connect(tmp_path / "state.db") as connection:
        rows = connection.execute("SELECT run_id, process_id FROM usage_runs WHERE session_id='resumed'").fetchall()
    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert rows[0][1] != rows[1][1]


def test_real_aiagent_conversation_lifecycle_writes_direct_receipt(tmp_path, monkeypatch):
    from hermes_cli.lifecycle import finalize_session
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from run_agent import AIAgent

    token = set_hermes_home_override(tmp_path)
    monkeypatch.setenv("HERMES_RUN_ID", "e2e-direct-run")
    try:
        class Handler(BaseHTTPRequestHandler):
            requests = []

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                Handler.requests.append(json.loads(self.rfile.read(length)))
                chunks = [
                    {"id": "chatcmpl-local", "object": "chat.completion.chunk", "created": 1,
                     "model": "fake/local-model", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}]},
                    {"id": "chatcmpl-local", "object": "chat.completion.chunk", "created": 1,
                     "model": "fake/local-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}},
                ]
                body = b"".join((b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks)) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            agent = AIAgent(
                api_key="test-key",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                provider="local",
                model="fake/local-model",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_id="e2e-session",
                platform="cli",
            )
            result = agent.run_conversation("hello")
            assert result["final_response"] == "done"
            finalize_session(session_id="e2e-session", platform="cli", completed=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        receipt = UsageLedger(tmp_path / "state.db").get_run("e2e-direct-run")
        assert receipt["task_run_id"] is None
        assert receipt["model"]
        assert receipt["provider"]
        assert receipt["input_tokens"] == 9
        assert receipt["output_tokens"] == 4
        assert receipt["cost_usd"] == 0.0
        assert receipt["outcome"] == "completed"
    finally:
        reset_hermes_home_override(token)


def test_real_aiagent_codex_responses_lifecycle_writes_usage_receipt(tmp_path, monkeypatch):
    from hermes_cli.lifecycle import finalize_session
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from run_agent import AIAgent

    token = set_hermes_home_override(tmp_path)
    monkeypatch.setenv("HERMES_RUN_ID", "e2e-codex-run")
    try:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                events = [
                    {"type": "response.created", "response": {"id": "resp-local", "model": "fake/codex-model"}},
                    {"type": "response.output_text.delta", "delta": "codex-done"},
                    {"type": "response.completed", "response": {"id": "resp-local", "model": "fake/codex-model", "usage": {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16}}},
                ]
                body = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def log_message(self, format, *args):  # noqa: A002, ANN001
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            agent = AIAgent(
                api_key="test-key",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                provider="openai-codex",
                model="fake/codex-model",
                api_mode="codex_responses",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_id="e2e-codex-session",
                platform="cli",
            )
            result = agent.run_conversation("hello")
            assert result["final_response"] == "codex-done"
            finalize_session(session_id="e2e-codex-session", platform="cli", completed=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        receipt = UsageLedger(tmp_path / "state.db").get_run("e2e-codex-run")
        assert receipt["input_tokens"] == 11
        assert receipt["output_tokens"] == 5
        assert receipt["model"] == "fake/codex-model"
        assert receipt["provider"] == "openai-codex"
        assert receipt["outcome"] == "completed"
    finally:
        reset_hermes_home_override(token)
