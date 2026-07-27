"""Real-CLI offline E2E for the Phase-1 implementation/review pipeline.

Only the model HTTP endpoint is faked.  Every worker is the production
``hermes_cli.main -> cli.HermesCLI -> AIAgent`` process spawned by
``_default_spawn``; no replacement argv or agent runner performs tool calls.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest
import yaml

from agent import kanban_handoff_scope as handoff_scope
from gateway.status import get_process_start_time
from hermes_cli import kanban_db as kb


_IMPLEMENTATION_TOOL_NAMES = {
    "read_file",
    "search_files",
    "write_file",
    "patch",
    "kanban_show",
    "kanban_comment",
    "kanban_heartbeat",
    "kanban_block",
    "kanban_complete",
}
_REVIEW_TOOL_NAMES = _IMPLEMENTATION_TOOL_NAMES - {"write_file", "patch"}
_CONTEXT_CANARIES = {
    "HOME_MEMORY_CANARY_7baf",
    "HOME_USER_CANARY_41c2",
    "HOME_SOUL_CANARY_50d3",
    "WORKSPACE_AGENTS_CANARY_8e12",
    "WORKSPACE_HERMES_CANARY_935a",
    "USER_SKILL_CANARY_1a6f",
    "EPHEMERAL_PROMPT_CANARY_c5d8",
    "PREFILL_CANARY_6e90",
}


def _phase1_config(
    *, base_url: str | None = None, workspace_root: str = "/tmp"
) -> dict:
    config = {
        "agent": {
            "max_turns": 90,
            "system_prompt": "EPHEMERAL_PROMPT_CANARY_c5d8",
            "prefill_messages": [
                {"role": "user", "content": "PREFILL_CANARY_6e90"}
            ],
        },
        # Deliberately enabled: a managed worker must still force the
        # checkpoint manager off and must not create a shadow Git store.
        "checkpoints": {"enabled": True, "auto_prune": True},
        "hooks_auto_accept": True,
        "kanban": {
            "failure_limit": 3,
            "short_task_handoff": {
                "enabled": True,
                "soft_iteration_limit": 4,
                "max_handoffs": 3,
                "allowed_workspace_roots": [workspace_root],
                "allowed_origins": [
                    {
                        "platform": "feishu",
                        "chat_type": "group",
                        "chat_id": "offline-e2e-group",
                        "user_id": "offline-e2e-user",
                    }
                ],
            },
        },
    }
    if base_url:
        config["model"] = {
            "default": "offline-model",
            "provider": "custom:offline-e2e",
            "base_url": base_url,
            "api_key": "offline-test-key",
            "api_mode": "chat_completions",
        }
        config["custom_providers"] = [
            {
                "name": "offline-e2e",
                "base_url": base_url,
                "model": "offline-model",
                "api_key": "offline-test-key",
                "api_mode": "chat_completions",
            }
        ]
    return config


def _phase1_control_origin(workspace_root: str) -> dict:
    config = _phase1_config(workspace_root=workspace_root)
    origin = {
        "platform": "feishu",
        "scope_id": "offline-e2e-tenant",
        "chat_type": "group",
        "chat_id": "offline-e2e-group",
        "thread_id": "",
        "user_id": "offline-e2e-user",
        "notifier_profile": "default",
        "session_key": (
            "agent:default:feishu:group:offline-e2e-group:offline-e2e-user"
        ),
        "message_id": "offline-review-pipeline-v1",
        "operation_slot": "slash",
    }
    decision = handoff_scope.decide_gateway_origin(config, origin)
    assert decision["authorized"] is True
    origin["short_handoff_policy"] = decision["task_policy_json"]
    return origin


def _tool_response(name: str, arguments: dict, call_number: int) -> dict:
    return {
        "id": f"offline-chat-{call_number}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "offline-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"offline-tool-{call_number}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "total_tokens": 25,
        },
    }


def _text_response(text: str, call_number: int) -> dict:
    return {
        "id": f"offline-chat-{call_number}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "offline-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "total_tokens": 13,
        },
    }


class _OfflineModelHandler(BaseHTTPRequestHandler):
    lock = threading.Lock()
    implementation_id = ""
    downstream_id = ""
    stages: dict[str, int] = {}
    requests: list[dict] = []
    call_number = 0

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.implementation_id = ""
            cls.downstream_id = ""
            cls.stages = {}
            cls.requests = []
            cls.call_number = 0

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib server API
        self._send_json(
            {
                "object": "list",
                "data": [
                    {
                        "id": "offline-model",
                        "object": "model",
                        "context_length": 131072,
                    }
                ],
            }
        )

    def do_POST(self):  # noqa: N802 - stdlib server API
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        message_blob = json.dumps(request.get("messages") or [], ensure_ascii=False)
        schema_names = {
            item.get("function", {}).get("name")
            for item in request.get("tools") or []
            if isinstance(item, dict)
        }
        system_blob = "\n".join(
            str(message.get("content") or "")
            for message in request.get("messages") or []
            if isinstance(message, dict) and message.get("role") == "system"
        )

        with type(self).lock:
            type(self).call_number += 1
            call_number = type(self).call_number
            if not schema_names:
                role = "auxiliary"
            elif type(self).downstream_id in message_blob:
                role = "downstream"
            elif "# Independent implementation review" in system_blob:
                role = "review"
            elif type(self).implementation_id in message_blob:
                role = "implementation"
            else:
                role = "unknown"
            stage = type(self).stages.get(role, 0)
            type(self).stages[role] = stage + 1
            type(self).requests.append(
                {
                    "role": role,
                    "stage": stage,
                    "schema_names": sorted(name for name in schema_names if name),
                    "system_blob": system_blob,
                    "message_blob": message_blob,
                }
            )

        if role == "implementation" and stage in {0, 3}:
            response = _tool_response(
                "write_file",
                {
                    "path": "candidate.txt",
                    "content": (
                        "needs-review-repai\n"
                        if stage == 0
                        else "phase-one-candidat\n"
                    ),
                },
                call_number,
            )
        elif role == "implementation" and stage in {1, 4}:
            response = _tool_response(
                "patch",
                {
                    "mode": "replace",
                    "path": "candidate.txt",
                    "old_string": (
                        "needs-review-repai" if stage == 1 else "phase-one-candidat"
                    ),
                    "new_string": (
                        "needs-review-repair" if stage == 1 else "phase-one-candidate"
                    ),
                },
                call_number,
            )
        elif role == "implementation":
            response = _tool_response(
                "kanban_complete",
                {
                    "summary": "Implementation file is ready for independent review.",
                    "metadata": {
                        "phase": "implementation",
                        "changed_files": ["candidate.txt"],
                    },
                },
                call_number,
            )
        elif role == "review" and stage in {0, 3}:
            response = _tool_response(
                "search_files",
                {
                    "pattern": (
                        "needs-review-repair" if stage == 0 else "phase-one-candidate"
                    ),
                    "path": ".",
                    "file_glob": "candidate.txt",
                },
                call_number,
            )
        elif role == "review" and stage in {1, 4}:
            response = _tool_response(
                "read_file", {"path": "candidate.txt"}, call_number
            )
        elif role == "review" and stage == 2:
            response = _tool_response(
                "kanban_block",
                {
                    "reason": (
                        "candidate.txt still contains the explicit "
                        "needs-review-repair marker"
                    ),
                    "return_to_implementation": True,
                },
                call_number,
            )
        elif role == "review":
            response = _tool_response(
                "kanban_complete",
                {
                    "summary": (
                        "Independent file review found the candidate consistent "
                        "with the available durable evidence."
                    ),
                    "metadata": {"phase": "review"},
                },
                call_number,
            )
        elif role == "downstream":
            response = _tool_response(
                "kanban_complete",
                {"summary": "Downstream observed the completed predecessor."},
                call_number,
            )
        else:
            response = _text_response("offline auxiliary response", call_number)

        if request.get("stream") is True:
            message = response["choices"][0]["message"]
            chunks = [
                {
                    "id": response["id"],
                    "object": "chat.completion.chunk",
                    "created": response["created"],
                    "model": response["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None,
                        }
                    ],
                }
            ]
            if message.get("content"):
                chunks.append(
                    {
                        "id": response["id"],
                        "object": "chat.completion.chunk",
                        "created": response["created"],
                        "model": response["model"],
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": message["content"]},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            for index, tool_call in enumerate(message.get("tool_calls") or []):
                chunks.append(
                    {
                        "id": response["id"],
                        "object": "chat.completion.chunk",
                        "created": response["created"],
                        "model": response["model"],
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": index,
                                            "id": tool_call["id"],
                                            "type": "function",
                                            "function": tool_call["function"],
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            chunks.append(
                {
                    "id": response["id"],
                    "object": "chat.completion.chunk",
                    "created": response["created"],
                    "model": response["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": (
                                "tool_calls" if message.get("tool_calls") else "stop"
                            ),
                        }
                    ],
                    "usage": response["usage"],
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self._send_json(response)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def isolated_kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _wait_for_status(task_id: str, status: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with kb.connect() as conn:
            task = kb.get_task(conn, task_id)
        if task is not None and task.status == status:
            return
        time.sleep(0.02)
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
    raise AssertionError(f"timed out waiting for {task_id}={status}; got {task}")


def _kill_exact_process(record: dict) -> None:
    proc = record["process"]
    if proc.poll() is None:
        try:
            os.killpg(record["pgid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=2)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX exit-gate E2E")
def test_short_task_review_pipeline_v1_uses_real_cli_and_agent(
    isolated_kanban_home, tmp_path, monkeypatch
):
    """Only model calls are faked; managed startup and tool use are real."""
    _OfflineModelHandler.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OfflineModelHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"

    # macOS pytest's tmp_path lives under /private/var, which production file
    # safety correctly treats as a system path. Use a dedicated /tmp checkout
    # (resolved as /private/tmp) for the worker's actual project workspace.
    workspace = Path(tempfile.mkdtemp(prefix="hermes-real-cli-e2e-", dir="/tmp"))
    outside = tmp_path / "outside-canaries"
    outside.mkdir()
    plugin_marker = outside / "plugin-imported"
    hook_marker = outside / "hook-ran"
    mcp_marker = outside / "mcp-ran"
    site_marker = outside / "sitecustomize-ran"

    (isolated_kanban_home / "MEMORY.md").write_text(
        "HOME_MEMORY_CANARY_7baf", encoding="utf-8"
    )
    (isolated_kanban_home / "USER.md").write_text(
        "HOME_USER_CANARY_41c2", encoding="utf-8"
    )
    (isolated_kanban_home / "SOUL.md").write_text(
        "HOME_SOUL_CANARY_50d3", encoding="utf-8"
    )
    (workspace / "AGENTS.md").write_text(
        "WORKSPACE_AGENTS_CANARY_8e12", encoding="utf-8"
    )
    (workspace / ".hermes.md").write_text(
        "WORKSPACE_HERMES_CANARY_935a", encoding="utf-8"
    )
    (workspace / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(site_marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    skill_dir = isolated_kanban_home / "skills" / "e2e-user-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "USER_SKILL_CANARY_1a6f", encoding="utf-8"
    )

    plugin_dir = isolated_kanban_home / "plugins" / "e2e_sentinel"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: e2e_sentinel\nversion: 0.1.0\n", encoding="utf-8"
    )
    (plugin_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(plugin_marker)!r}).write_text('imported')\n"
        "def register(ctx):\n    pass\n",
        encoding="utf-8",
    )
    hook_script = outside / "hook.sh"
    hook_script.write_text(
        "#!/bin/sh\n" + f"touch {str(hook_marker)!r}\n" + "printf '{}\\n'\n",
        encoding="utf-8",
    )
    hook_script.chmod(0o700)

    config = _phase1_config(
        base_url=base_url,
        workspace_root=str(workspace.resolve()),
    )
    config["plugins"] = {"enabled": ["e2e_sentinel"]}
    config["hooks"] = {
        "on_session_start": [{"command": str(hook_script)}]
    }
    config["mcp_servers"] = {
        "e2e-sentinel": {
            "command": "/bin/sh",
            "args": ["-c", f"touch {str(mcp_marker)!r}"],
        }
    }
    config_path = isolated_kanban_home / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    processes: dict[str, dict] = {}
    spawn_contracts: list[dict] = []
    real_popen = subprocess.Popen
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")
    monkeypatch.setenv("PYTHONPATH", str(workspace))
    monkeypatch.setenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "EPHEMERAL_PROMPT_CANARY_c5d8")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda _name: str(isolated_kanban_home),
    )
    # The ordinary downstream worker also uses the production source CLI;
    # managed workers ignore this legacy resolver and exercise their own
    # isolated launch path directly.
    monkeypatch.setattr(kb, "_resolve_hermes_argv", kb._managed_worker_hermes_argv)
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)

    def capture_spawn(cmd, **kwargs):
        proc = real_popen(cmd, **kwargs)
        env = kwargs.get("env") or {}
        if env.get("HERMES_KANBAN_TASK"):
            role = (
                "review"
                if env.get("HERMES_KANBAN_REVIEW_MODE") == "1"
                else "implementation"
                if env.get("HERMES_KANBAN_MANAGED_LANE") == "implementation"
                else "downstream"
            )
            start_token = get_process_start_time(proc.pid)
            assert start_token is not None
            record = {
                "process": proc,
                "pid": proc.pid,
                "pgid": os.getpgid(proc.pid),
                "start_token": str(start_token),
                "cmd": list(cmd),
                "env": dict(env),
            }
            assert record["pgid"] == proc.pid
            instance = 1 + sum(
                1
                for existing in processes.values()
                if existing.get("role") == role
            )
            record["role"] = role
            record["instance"] = instance
            processes[f"{role}-{instance}"] = record
            spawn_contracts.append(
                {
                    "role": role,
                    "instance": instance,
                    "managed": bool(env.get("HERMES_KANBAN_MANAGED_LANE")),
                    "review": env.get("HERMES_KANBAN_REVIEW_MODE") == "1",
                    "policy_enabled": json.loads(
                        env["HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY"]
                    ).get("enabled"),
                    "cmd": list(cmd),
                }
            )
        return proc

    monkeypatch.setattr(subprocess, "Popen", capture_spawn)

    from hermes_cli.kanban import run_slash

    create_output = run_slash(
        " ".join(
            [
                "create",
                shlex.quote("offline implementation"),
                "--assignee",
                "default",
                "--workspace",
                shlex.quote(f"dir:{workspace}"),
                "--validation-class",
                "text_mechanism",
                "--max-retries",
                "1",
                "--json",
            ]
        ),
        control_origin=_phase1_control_origin(str(workspace.resolve())),
    )
    created = json.loads(create_output)
    implementation_id = created["id"]
    assert created["validation_class"] == "text_mechanism"
    assert created["workspace_kind"] == "dir"
    assert created["workspace_path"] == str(workspace.resolve())
    assert created["max_retries"] == 1

    with kb.connect() as conn:
        downstream_id = kb.create_task(
            conn,
            title="offline downstream",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        kb.link_tasks(conn, implementation_id, downstream_id)
    _OfflineModelHandler.implementation_id = implementation_id
    _OfflineModelHandler.downstream_id = downstream_id

    try:
        with kb.connect() as conn:
            first = kb.dispatch_once(conn, max_spawn=1)
        assert [item[0] for item in first.spawned] == [implementation_id]
        implementation = processes["implementation-1"]["process"]
        assert implementation.wait(timeout=20) == 0
        _wait_for_status(implementation_id, "review")
        assert (workspace / "candidate.txt").read_text(encoding="utf-8") == (
            "needs-review-repair\n"
        )

        with kb.connect() as conn:
            review_tick = kb.dispatch_once(conn, max_spawn=1)
        assert [item[0] for item in review_tick.spawned] == [implementation_id]
        reviewer = processes["review-1"]["process"]
        assert reviewer.wait(timeout=20) == 0
        _wait_for_status(implementation_id, "todo")

        with kb.connect() as conn:
            repair_tick = kb.dispatch_once(conn, max_spawn=1)
        assert [item[0] for item in repair_tick.spawned] == [implementation_id]
        repair = processes["implementation-2"]["process"]
        assert repair.wait(timeout=20) == 0
        _wait_for_status(implementation_id, "review")
        assert (workspace / "candidate.txt").read_text(encoding="utf-8") == (
            "phase-one-candidate\n"
        )

        with kb.connect() as conn:
            second_review_tick = kb.dispatch_once(conn, max_spawn=1)
        assert [item[0] for item in second_review_tick.spawned] == [
            implementation_id
        ]
        second_reviewer = processes["review-2"]["process"]
        assert second_reviewer.wait(timeout=20) == 0
        _wait_for_status(implementation_id, "done")

        # Managed startup/read/edit must not load any user/project context,
        # plugin, MCP, shell hook, skill index, checkpoint store, or auxiliary
        # model task. Exactly three main-model calls per lane are expected:
        # implementation writes, patches, then completes; review searches,
        # reads the full file, then rejects or completes.
        managed_requests = [
            request
            for request in _OfflineModelHandler.requests
            if request["role"] in {"implementation", "review"}
        ]
        assert [request["role"] for request in managed_requests] == [
            "implementation",
            "implementation",
            "implementation",
            "review",
            "review",
            "review",
            "implementation",
            "implementation",
            "implementation",
            "review",
            "review",
            "review",
        ]
        for request in managed_requests:
            expected_names = (
                _REVIEW_TOOL_NAMES
                if request["role"] == "review"
                else _IMPLEMENTATION_TOOL_NAMES
            )
            assert set(request["schema_names"]) == expected_names
        combined_managed_prompt = "\n".join(
            request["system_blob"] + request["message_blob"]
            for request in managed_requests
        )
        assert all(canary not in combined_managed_prompt for canary in _CONTEXT_CANARIES)
        assert not plugin_marker.exists()
        assert not hook_marker.exists()
        assert not mcp_marker.exists()
        assert not site_marker.exists()
        assert not (isolated_kanban_home / ".skills_prompt_snapshot.json").exists()
        assert not (isolated_kanban_home / "checkpoints").exists()
        assert {path.name for path in (isolated_kanban_home / "skills").iterdir()} == {
            "e2e-user-skill"
        }

        for contract in spawn_contracts[:4]:
            cmd = contract["cmd"]
            assert contract["managed"] is True
            assert "--accept-hooks" not in cmd
            assert "--skills" not in cmd
            assert cmd[1:5] == ["-B", "-I", "-s", "-E"]
            assert "runpy.run_module('hermes_cli.main'" in cmd[6]

        # Remove deliberately hostile optional integrations before exercising
        # the ordinary downstream lane; ordinary behavior is not part of the
        # managed no-load assertion above and remains unchanged.
        config.pop("hooks", None)
        config.pop("mcp_servers", None)
        config.pop("plugins", None)
        config["checkpoints"] = {"enabled": False, "auto_prune": False}
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        plugin_dir.rename(isolated_kanban_home / "plugins" / "e2e_sentinel.off")
        monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

        with kb.connect() as conn:
            downstream_tick = kb.dispatch_once(conn, max_spawn=1)
        assert [item[0] for item in downstream_tick.spawned] == [downstream_id]
        downstream = processes["downstream-1"]["process"]
        assert downstream.wait(timeout=20) == 0
        _wait_for_status(downstream_id, "done")

        assert len({record["pid"] for record in processes.values()}) == 5
        assert [item["role"] for item in spawn_contracts] == [
            "implementation",
            "review",
            "implementation",
            "review",
            "downstream",
        ]
        assert spawn_contracts[0]["policy_enabled"] is True
        assert spawn_contracts[1]["policy_enabled"] is False
        assert spawn_contracts[2]["policy_enabled"] is True
        assert spawn_contracts[3]["policy_enabled"] is False
        assert spawn_contracts[4]["managed"] is False
        assert "--accept-hooks" in spawn_contracts[4]["cmd"]

        with kb.connect() as conn:
            gates = conn.execute(
                "SELECT gate_kind, worker_pid, released_at FROM task_exit_gates "
                "WHERE parent_task_id = ? ORDER BY created_at, gate_id",
                (implementation_id,),
            ).fetchall()
            assert [row["gate_kind"] for row in gates] == [
                "control_drain",
                "control_drain",
                "control_drain",
                "control_drain",
            ]
            assert [row["worker_pid"] for row in gates] == [
                processes["implementation-1"]["pid"],
                processes["review-1"]["pid"],
                processes["implementation-2"]["pid"],
                processes["review-2"]["pid"],
            ]
            assert all(row["released_at"] is not None for row in gates)
            events = [event.kind for event in kb.list_events(conn, implementation_id)]
            assert events.count("review_requested") == 2
            assert events.count("review_changes_requested") == 1
            assert events.count("completed") == 1
            completed_task = kb.get_task(conn, implementation_id)
            completed_run = kb.latest_run(conn, implementation_id)
            assert completed_task.validation_class == "text_mechanism"
            evidence = completed_run.metadata[kb._MANAGED_REVIEW_EVIDENCE_KEY]
            assert evidence["validation_class"] == "text_mechanism"
            assert evidence["workspace"] == str(workspace.resolve())
            assert evidence["read_files"] == [
                {
                    "path": "candidate.txt",
                    "sha256": (
                        "ceb3bd1cc367f90b24eb7be6260dedeb1d8686b9b213047c"
                        "fd9ffa80b12d7de3"
                    ),
                    "size": len(b"phase-one-candidate\n"),
                }
            ]
    finally:
        server.shutdown()
        server.server_close()
        for record in processes.values():
            _kill_exact_process(record)
        shutil.rmtree(workspace, ignore_errors=True)
