from __future__ import annotations

import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.codex_bridge import (
    BRIDGE_PHASES,
    BridgeOrigin,
    BridgeRequest,
    BridgeStore,
    CodexBridgeService,
    CodexBridgeSettings,
    CodexSdkExecutor,
    CodexUserQuestion,
    GatewayCodexBridgeMixin,
    ProgressEvent,
    _structured_codex_user_question,
    legacy_workers_auto_dispatch_enabled,
    validate_workspace,
)
from gateway.codex_bridge_local import (
    LocalCodexTestAdapter,
    make_local_codex_event,
    make_local_codex_reply_event,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig


class FakeCodexExecutor:
    def __init__(self, result: str = "Codex final result"):
        self.result = result
        self.calls: list[str | None] = []

    def execute(
        self,
        request,
        *,
        codex_thread_id,
        on_thread,
        on_progress,
    ):
        self.calls.append(codex_thread_id)
        on_thread(codex_thread_id or "codex-thread-1")
        on_progress("codex_start", "Codex thread đã bắt đầu xử lý request.")
        on_progress("verification", "Codex đang chạy và kiểm tra test trong workspace.")
        return self.result


class NeedsLoginExecutor(FakeCodexExecutor):
    def execute(self, request, *, codex_thread_id, on_thread, on_progress):
        self.calls.append(codex_thread_id)
        on_thread(codex_thread_id or "codex-thread-login")
        raise RuntimeError("Codex authentication login required")


class StructuredQuestionExecutor(FakeCodexExecutor):
    def execute(self, request, *, codex_thread_id, on_thread, on_progress):
        self.calls.append(codex_thread_id)
        on_thread(codex_thread_id or "codex-thread-question")
        raise CodexUserQuestion(
            "Deploy target: Chọn môi trường triển khai.\n"
            "Lựa chọn: Staging (An toàn để kiểm thử); Production (Ảnh hưởng người dùng)"
        )


class ChattyProgressExecutor(FakeCodexExecutor):
    def execute(self, request, *, codex_thread_id, on_thread, on_progress):
        self.calls.append(codex_thread_id)
        on_thread(codex_thread_id or "codex-thread-chatty")
        for step in ("execution", "tooling", "execution", "implementation"):
            on_progress(step, f"Public {step} update")
        return self.result


class LocalGatewayHarness(GatewayCodexBridgeMixin):
    def __init__(self, settings, service, adapter):
        self.settings = settings
        self._codex_bridge_service = service
        self.adapter = adapter

    def _codex_bridge_settings(self):
        return self.settings

    def _adapter_for_source(self, _source):
        return self.adapter


def _settings(workspace: Path, **overrides) -> CodexBridgeSettings:
    values = {
        "enabled": True,
        "allowed_origins": ("local",),
        "workspace_allowlist": (str(workspace),),
        "default_workspace": None,
        "command_prefix": "/codex",
        "model": None,
        "sandbox": "workspace-write",
        "stale_recovery_seconds": 1,
    }
    values.update(overrides)
    return CodexBridgeSettings(**values)


@pytest.mark.asyncio
async def test_local_gateway_request_runs_once_and_returns_to_origin(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store_path = tmp_path / "bridge.db"
    settings = _settings(workspace)
    executor = FakeCodexExecutor()
    store = BridgeStore(store_path)
    service = CodexBridgeService(
        settings, store=store, executor=executor, instance_id="gateway-a"
    )
    adapter = LocalCodexTestAdapter()
    gateway = LocalGatewayHarness(settings, service, adapter)
    event = make_local_codex_event(
        "Inspect this workspace",
        workspace=str(workspace),
        idempotency_key="local-message-key",
    )

    first = await gateway._maybe_handle_codex_bridge(event)
    second = await gateway._maybe_handle_codex_bridge(event)

    assert first.handled is True
    assert first.response == "Codex final result"
    assert second.response == "Codex final result"
    assert executor.calls == [None]
    assert adapter.messages
    assert {message["chat_id"] for message in adapter.messages} == {"local-codex-test"}
    assert all(message["reply_to"] == "local-message-1" for message in adapter.messages)
    assert any("test" in message["content"].lower() for message in adapter.messages)

    mapping = store.get_by_idempotency("local-message-key")
    assert mapping is not None
    assert mapping.codex_thread_id == "codex-thread-1"
    assert mapping.phase == "done"
    assert mapping.workspace == str(workspace.resolve())
    assert mapping.origin["type"] == "local"
    phases = [event["phase"] for event in store.list_events(mapping.hermes_job_id)]
    assert phases[0:2] == ["captured", "working"]
    assert "output_ready" in phases
    assert phases[-1] == "done"


@pytest.mark.asyncio
async def test_initial_idempotency_key_rejects_changed_prompt(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    settings = _settings(workspace)
    executor = FakeCodexExecutor()
    service = CodexBridgeService(
        settings,
        store=BridgeStore(tmp_path / "bridge.db"),
        executor=executor,
        instance_id="gateway-idempotency",
    )
    gateway = LocalGatewayHarness(settings, service, LocalCodexTestAdapter())
    first = make_local_codex_event(
        "Inspect this workspace",
        workspace=str(workspace),
        idempotency_key="same-initial-key",
    )
    changed = make_local_codex_event(
        "Delete this workspace",
        workspace=str(workspace),
        idempotency_key="same-initial-key",
    )

    assert (await gateway._maybe_handle_codex_bridge(first)).response == "Codex final result"
    rejected = await gateway._maybe_handle_codex_bridge(changed)

    assert rejected.handled is True
    assert rejected.response.startswith("Codex bridge rejected request")
    assert executor.calls == [None]


@pytest.mark.asyncio
async def test_progress_updates_are_bounded_to_phase1_delivery_contract(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = BridgeStore(tmp_path / "bridge.db")
    service = CodexBridgeService(
        _settings(workspace),
        store=store,
        executor=ChattyProgressExecutor(),
        instance_id="gateway-progress-cap",
    )
    request = BridgeRequest(
        hermes_job_id="job-progress-cap",
        idempotency_key="progress-cap-key",
        origin=BridgeOrigin("local", "conversation", "message"),
        workspace=str(workspace),
        prompt="work",
    )

    await service.execute(request, lambda _event: None)

    events = store.list_events(request.hermes_job_id)
    in_flight = [
        event for event in events if event["phase"] in {"captured", "working"}
    ]
    assert len(in_flight) == 4
    assert [event["progress"]["current_step"] for event in in_flight] == [
        "capture",
        "codex_start",
        "execution",
        "tooling",
    ]


@pytest.mark.asyncio
async def test_persisted_mapping_survives_service_restart_without_duplicate_thread(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    settings = _settings(workspace)
    store_path = tmp_path / "bridge.db"
    first_executor = FakeCodexExecutor("persisted final")
    first = CodexBridgeService(
        settings,
        store=BridgeStore(store_path),
        executor=first_executor,
        instance_id="gateway-a",
    )
    request = BridgeRequest(
        hermes_job_id="job-restart",
        idempotency_key="restart-key",
        origin=BridgeOrigin("local", "conversation", "message"),
        workspace=str(workspace),
        prompt="work",
    )
    notices = []
    await first.execute(request, notices.append)

    second_executor = FakeCodexExecutor("should not run")
    restarted = CodexBridgeService(
        settings,
        store=BridgeStore(store_path),
        executor=second_executor,
        instance_id="gateway-b",
    )
    result = await restarted.execute(request, notices.append)

    assert result == "persisted final"
    assert second_executor.calls == []
    assert restarted.store.get_by_idempotency("restart-key").codex_thread_id == "codex-thread-1"


@pytest.mark.asyncio
async def test_stale_incomplete_mapping_resumes_same_codex_thread(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    settings = _settings(workspace)
    store_path = tmp_path / "bridge.db"
    store = BridgeStore(store_path)
    request = BridgeRequest(
        hermes_job_id="job-resume",
        idempotency_key="resume-key",
        origin=BridgeOrigin("local", "conversation", "message"),
        workspace=str(workspace),
        prompt="resume work",
    )
    store.capture(
        request,
        owner_instance_id="dead-gateway",
        stale_recovery_seconds=1,
    )
    store.set_thread_id("job-resume", "codex-thread-existing")
    with sqlite3.connect(store_path) as db:
        db.execute(
            "UPDATE bridge_jobs SET phase='working', updated_at='2000-01-01T00:00:00+00:00'"
        )

    executor = FakeCodexExecutor("resumed final")
    restarted = CodexBridgeService(
        settings,
        store=BridgeStore(store_path),
        executor=executor,
        instance_id="new-gateway",
    )
    result = await restarted.execute(request, lambda _event: None)

    assert result == "resumed final"
    assert executor.calls == ["codex-thread-existing"]


@pytest.mark.asyncio
async def test_auth_failure_is_needs_user_and_does_not_expose_exception(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = BridgeStore(tmp_path / "bridge.db")
    settings = _settings(workspace)
    service = CodexBridgeService(
        settings,
        store=store,
        executor=NeedsLoginExecutor(),
        instance_id="gateway-a",
    )
    request = BridgeRequest(
        hermes_job_id="job-needs-user",
        idempotency_key="needs-user-key",
        origin=BridgeOrigin("local", "conversation", "message"),
        workspace=str(workspace),
        prompt="work",
    )
    notices = []
    result = await service.execute(request, notices.append)

    assert "đăng nhập" in result.lower()
    assert notices[-1].phase == "needs_user"
    assert "authentication login required" not in notices[-1].summary
    prompt_id = notices[-1].progress["prompt_id"]
    pending = store.get_pending_question(prompt_id)
    assert pending is not None
    assert pending.status == "pending"
    assert store.get_by_job_id("job-needs-user").codex_thread_id == "codex-thread-login"


def test_structured_codex_question_requires_blocking_protocol_event():
    params = {
        "isBlocking": True,
        "itemId": "item-1",
        "threadId": "thread-1",
        "turnId": "turn-1",
        "questions": [
            {
                "header": "Deploy target",
                "id": "environment",
                "question": "Chọn môi trường triển khai.",
                "options": [
                    {"label": "Staging", "description": "An toàn để kiểm thử"},
                    {"label": "Production", "description": "Ảnh hưởng người dùng"},
                ],
                "isOther": True,
            }
        ],
    }

    question = _structured_codex_user_question(
        "item/tool/requestUserInput", params
    )

    assert question == (
        "Deploy target: Chọn môi trường triển khai.\n"
        "Lựa chọn: Staging (An toàn để kiểm thử); "
        "Production (Ảnh hưởng người dùng); Câu trả lời khác"
    )
    assert (
        _structured_codex_user_question(
            "item/tool/requestUserInput", {**params, "isBlocking": False}
        )
        is None
    )
    assert _structured_codex_user_question("item/completed", params) is None


def test_sdk_executor_intercepts_structured_request_without_fake_answer(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    request = BridgeRequest(
        hermes_job_id="job-sdk-question",
        idempotency_key="sdk-question-key",
        origin=BridgeOrigin("local", "conversation", "message"),
        workspace=str(workspace),
        prompt="deploy",
    )
    observed = {"continued_after_question": False}

    class FakeHandle:
        def __init__(self, client):
            self.client = client

        def stream(self):
            self.client._approval_handler(
                "item/tool/requestUserInput",
                {
                    "isBlocking": True,
                    "questions": [
                        {
                            "header": "Target",
                            "id": "target",
                            "question": "Deploy ở đâu?",
                            "options": None,
                        }
                    ],
                },
            )
            observed["continued_after_question"] = True
            return iter(())

    class FakeThread:
        id = "thread-sdk-question"

        def __init__(self, client):
            self.client = client

        def turn(self, _prompt):
            return FakeHandle(self.client)

    class FakeCodex:
        def __init__(self):
            self._client = SimpleNamespace(_approval_handler=lambda *_args: {})

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def thread_start(self, **_kwargs):
            return FakeThread(self._client)

    monkeypatch.setitem(
        sys.modules,
        "openai_codex",
        SimpleNamespace(
            ApprovalMode=SimpleNamespace(deny_all="deny_all"),
            Codex=FakeCodex,
            Sandbox=SimpleNamespace(
                read_only="read-only", workspace_write="workspace-write"
            ),
        ),
    )

    with pytest.raises(CodexUserQuestion, match="Deploy ở đâu"):
        CodexSdkExecutor(_settings(workspace)).execute(
            request,
            codex_thread_id=None,
            on_thread=lambda _thread_id: None,
            on_progress=lambda _step, _summary: None,
        )

    assert observed["continued_after_question"] is False


def test_sdk_executor_refuses_nonblocking_user_input_instead_of_fake_answer(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    request = BridgeRequest(
        hermes_job_id="job-sdk-nonblocking",
        idempotency_key="sdk-nonblocking-key",
        origin=BridgeOrigin("local", "conversation", "message"),
        workspace=str(workspace),
        prompt="ask",
    )

    class FakeHandle:
        def __init__(self, client):
            self.client = client

        def stream(self):
            self.client._approval_handler(
                "item/tool/requestUserInput",
                {
                    "isBlocking": False,
                    "questions": [
                        {
                            "header": "Target",
                            "id": "target",
                            "question": "Deploy ở đâu?",
                        }
                    ],
                },
            )
            return iter(())

    class FakeThread:
        id = "thread-sdk-nonblocking"

        def __init__(self, client):
            self.client = client

        def turn(self, _prompt):
            return FakeHandle(self.client)

    class FakeCodex:
        def __init__(self):
            self._client = SimpleNamespace(_approval_handler=lambda *_args: {})

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def thread_start(self, **_kwargs):
            return FakeThread(self._client)

    monkeypatch.setitem(
        sys.modules,
        "openai_codex",
        SimpleNamespace(
            ApprovalMode=SimpleNamespace(deny_all="deny_all"),
            Codex=FakeCodex,
            Sandbox=SimpleNamespace(
                read_only="read-only", workspace_write="workspace-write"
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="refusing to fabricate an answer"):
        CodexSdkExecutor(_settings(workspace)).execute(
            request,
            codex_thread_id=None,
            on_thread=lambda _thread_id: None,
            on_progress=lambda _step, _summary: None,
        )


@pytest.mark.skipif(
    os.environ.get("HERMES_CODEX_BRIDGE_LIVE_E2E") != "1",
    reason="set HERMES_CODEX_BRIDGE_LIVE_E2E=1 to use real Codex auth/runtime",
)
@pytest.mark.asyncio
async def test_live_codex_app_server_question_restart_and_correlated_reply(
    tmp_path, monkeypatch
):
    """Exercise the durable bridge against the pinned SDK and app-server."""

    from openai_codex import ApprovalMode, Codex, Sandbox
    from openai_codex.api import TurnHandle
    from openai_codex.client import _installed_codex_path

    from gateway.run import GatewayRunner

    version = importlib.metadata.version("openai-codex")
    version_parts = tuple(int(part) for part in version.split(".")[:3])
    assert (0, 147, 0) <= version_parts < (0, 149, 0)
    runtime = _installed_codex_path()
    version_result = subprocess.run(
        [str(runtime), "--version"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "0.147.0" in version_result.stdout

    schema_dir = tmp_path / "app-server-schema"
    subprocess.run(
        [
            str(runtime),
            "app-server",
            "generate-json-schema",
            "--experimental",
            "--out",
            str(schema_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    server_request_schema = json.loads(
        (schema_dir / "ServerRequest.json").read_text(encoding="utf-8")
    )

    def method_enums(value):
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                method = properties.get("method")
                if isinstance(method, dict) and isinstance(method.get("enum"), list):
                    yield method["enum"]
            for child in value.values():
                yield from method_enums(child)
        elif isinstance(value, list):
            for child in value:
                yield from method_enums(child)

    assert ["item/tool/requestUserInput"] in list(
        method_enums(server_request_schema)
    )
    params_schema = json.loads(
        (schema_dir / "ToolRequestUserInputParams.json").read_text(encoding="utf-8")
    )
    assert {
        "isBlocking",
        "itemId",
        "questions",
        "threadId",
        "turnId",
    }.issubset(params_schema["required"])

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = workspace / "sentinel.txt"
    sentinel.write_text("must remain unchanged", encoding="utf-8")

    def workspace_snapshot():
        return {
            path.relative_to(workspace).as_posix(): path.read_bytes()
            for path in workspace.rglob("*")
            if path.is_file()
        }

    before = workspace_snapshot()
    settings = _settings(workspace, sandbox="read-only")

    class LivePlanQuestionExecutor:
        def __init__(self):
            self.calls = []
            self.authenticated = False
            self.server_method = None
            self.server_params = None

        def execute(
            self,
            request,
            *,
            codex_thread_id,
            on_thread,
            on_progress,
        ):
            self.calls.append(codex_thread_id)
            assert codex_thread_id is None

            def handle_server_request(method, params):
                self.server_method = method
                self.server_params = params
                question = _structured_codex_user_question(method, params)
                if question:
                    raise CodexUserQuestion(question)
                if method in {"item/tool/requestUserInput", "tool/requestUserInput"}:
                    raise RuntimeError(
                        "Codex user-input request was non-blocking or invalid; "
                        "refusing to fabricate an answer"
                    )
                raise RuntimeError(f"Unexpected Codex server request: {method}")

            with Codex() as codex:
                codex._client._approval_handler = handle_server_request
                self.authenticated = codex.account().account is not None
                thread = codex.thread_start(
                    cwd=request.workspace,
                    sandbox=Sandbox.read_only,
                    approval_mode=ApprovalMode.deny_all,
                )
                on_thread(thread.id)
                on_progress("codex_start", "Codex live thread started.")
                models = codex.models().data
                model = next(item.model for item in models if item.is_default)
                started = codex._client.turn_start(
                    thread.id,
                    request.prompt,
                    params={
                        "approvalPolicy": "never",
                        "cwd": request.workspace,
                        "sandboxPolicy": {"type": "readOnly"},
                        "collaborationMode": {
                            "mode": "plan",
                            "settings": {
                                "model": model,
                                "reasoning_effort": "low",
                                "developer_instructions": None,
                            },
                        },
                    },
                )
                handle = TurnHandle(codex._client, thread.id, started.turn.id)
                for _notification in handle.stream():
                    pass
            raise RuntimeError("Codex did not emit structured user input")

    class CountingExecutor:
        def __init__(self, executor):
            self.executor = executor
            self.calls = []

        def execute(self, request, *, codex_thread_id, on_thread, on_progress):
            self.calls.append(codex_thread_id)
            return self.executor.execute(
                request,
                codex_thread_id=codex_thread_id,
                on_thread=on_thread,
                on_progress=on_progress,
            )

    auth_checks = []

    def build_runner(service, adapter):
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={Platform.LOCAL: PlatformConfig(enabled=True)}
        )
        runner.adapters = {Platform.LOCAL: adapter}
        runner.session_store = MagicMock()
        runner._startup_restore_in_progress = False
        runner._running_agents = {}
        runner._codex_bridge_service = service
        runner._codex_bridge_settings = lambda: settings
        runner._scale_to_zero_note_real_inbound = lambda: None

        def authorized(source):
            auth_checks.append(source)
            return True

        runner._is_user_authorized = authorized
        runner._handle_message_with_agent = AsyncMock(return_value="wrong executor")
        return runner

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *args, **kwargs: [])
    store_path = tmp_path / "bridge.db"
    initial_executor = LivePlanQuestionExecutor()
    first_adapter = LocalCodexTestAdapter()
    first_service = CodexBridgeService(
        settings,
        store=BridgeStore(store_path),
        executor=initial_executor,
        instance_id="live-before-restart",
    )
    first_runner = build_runner(first_service, first_adapter)
    initial = make_local_codex_event(
        "Before planning anything else, call request_user_input exactly once. "
        "Ask one question with header Environment, id environment, question "
        "Choose the deployment environment., and options Staging described Safe "
        "test target and Production described User-impacting target. Do not inspect "
        "or modify files.",
        workspace=str(workspace),
        idempotency_key="live-initial-key",
    )

    question_result = await first_adapter.dispatch(first_runner, initial)
    duplicate_initial = await first_adapter.dispatch(first_runner, initial)

    mapping = first_service.store.get_by_idempotency("live-initial-key")
    pending = first_service.store.get_latest_pending_question(mapping.hermes_job_id)
    assert initial_executor.authenticated is True
    assert initial_executor.calls == [None]
    assert initial_executor.server_method == "item/tool/requestUserInput"
    assert initial_executor.server_params["isBlocking"] is True
    assert question_result == duplicate_initial == pending.question
    assert "Staging" in pending.question and "Production" in pending.question
    assert pending.prompt_id.startswith("prompt_")
    assert mapping.phase == "needs_user"
    assert mapping.codex_thread_id == initial_executor.server_params["threadId"]
    assert workspace_snapshot() == before
    first_runner._handle_message_with_agent.assert_not_awaited()

    resumed_executor = CountingExecutor(CodexSdkExecutor(settings))
    second_service = CodexBridgeService(
        settings,
        store=BridgeStore(store_path),
        executor=resumed_executor,
        instance_id="live-after-restart",
    )
    wrong_adapter = LocalCodexTestAdapter()
    wrong_runner = build_runner(second_service, wrong_adapter)
    wrong_origin = make_local_codex_reply_event(
        "Staging",
        prompt_id=pending.prompt_id,
        idempotency_key="live-wrong-origin-key",
        conversation_id="another-conversation",
    )
    wrong_result = await wrong_adapter.dispatch(wrong_runner, wrong_origin)
    assert "origin does not match" in wrong_result
    assert resumed_executor.calls == []
    assert second_service.store.get_pending_question(pending.prompt_id).status == "pending"

    second_adapter = LocalCodexTestAdapter()
    second_runner = build_runner(second_service, second_adapter)
    reply = make_local_codex_reply_event(
        "Staging. Continue from the pending question and finish with a concise "
        "confirmation; do not inspect or modify files.",
        prompt_id=pending.prompt_id,
        idempotency_key="live-reply-key",
    )
    final = await second_adapter.dispatch(second_runner, reply)
    duplicate_reply = await second_adapter.dispatch(second_runner, reply)

    completed = second_service.store.get_by_job_id(mapping.hermes_job_id)
    assert final == duplicate_reply
    assert "Staging" in final
    assert resumed_executor.calls == [mapping.codex_thread_id]
    assert completed.codex_thread_id == mapping.codex_thread_id
    assert completed.phase == "done"
    assert completed.final_result == final
    assert completed.origin == mapping.origin
    assert second_service.store.get_pending_question(pending.prompt_id).status == "answered"
    phases = [
        event["phase"]
        for event in second_service.store.list_events(mapping.hermes_job_id)
    ]
    assert phases == [
        "captured",
        "working",
        "needs_user",
        "working",
        "output_ready",
        "done",
    ]
    assert workspace_snapshot() == before
    assert len(auth_checks) == 5
    assert all(
        "reasoning" not in json.dumps(event).lower()
        for event in second_service.store.list_events(mapping.hermes_job_id)
    )
    with sqlite3.connect(store_path) as db:
        assert db.execute("SELECT COUNT(*) FROM bridge_replies").fetchone()[0] == 1
    delivered = second_adapter.messages[-1]
    assert delivered["chat_id"] == "local-codex-test"
    assert delivered["reply_to"] == "local-reply-1"
    assert delivered["content"] == final
    assert delivered["metadata"]["codex_bridge_final"] is True
    second_runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_question_uses_durable_prompt_and_reply_resume(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = BridgeStore(tmp_path / "bridge.db")
    settings = _settings(workspace)
    service = CodexBridgeService(
        settings,
        store=store,
        executor=StructuredQuestionExecutor(),
        instance_id="gateway-a",
    )
    adapter = LocalCodexTestAdapter()
    gateway = LocalGatewayHarness(settings, service, adapter)
    initial = make_local_codex_event(
        "Deploy app",
        workspace=str(workspace),
        idempotency_key="structured-question-initial",
    )

    result = await gateway._maybe_handle_codex_bridge(initial)

    needs_user = next(
        message["metadata"]["codex_bridge_event"]
        for message in reversed(adapter.messages)
        if message["metadata"].get("codex_bridge_event", {}).get("phase")
        == "needs_user"
    )
    prompt_id = needs_user["progress"]["prompt_id"]
    assert "Deploy target" in result.response
    assert store.get_pending_question(prompt_id).status == "pending"
    assert store.get_by_idempotency("structured-question-initial").phase == "needs_user"

    resumed = FakeCodexExecutor("deployed to staging")
    service.executor = resumed
    reply = make_local_codex_reply_event(
        "Staging",
        prompt_id=prompt_id,
        idempotency_key="structured-question-reply",
    )

    resumed_result = await gateway._maybe_handle_codex_bridge(reply)

    assert resumed_result.response == "deployed to staging"
    assert resumed.calls == ["codex-thread-question"]
    assert store.get_pending_question(prompt_id).status == "answered"


@pytest.mark.asyncio
async def test_local_reply_after_restart_resumes_same_thread_and_is_idempotent(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store_path = tmp_path / "bridge.db"
    settings = _settings(workspace)
    first_adapter = LocalCodexTestAdapter()
    first_service = CodexBridgeService(
        settings,
        store=BridgeStore(store_path),
        executor=NeedsLoginExecutor(),
        instance_id="gateway-before-restart",
    )
    first_gateway = LocalGatewayHarness(settings, first_service, first_adapter)
    initial = make_local_codex_event(
        "Do work after login",
        workspace=str(workspace),
        idempotency_key="initial-needs-user",
    )

    initial_result = await first_gateway._maybe_handle_codex_bridge(initial)
    needs_user = next(
        message["metadata"]["codex_bridge_event"]
        for message in reversed(first_adapter.messages)
        if message["metadata"].get("codex_bridge_event", {}).get("phase") == "needs_user"
    )
    prompt_id = needs_user["progress"]["prompt_id"]
    assert "đăng nhập" in initial_result.response.lower()

    resumed_executor = FakeCodexExecutor("resumed final result")
    second_adapter = LocalCodexTestAdapter()
    restarted_service = CodexBridgeService(
        settings,
        store=BridgeStore(store_path),
        executor=resumed_executor,
        instance_id="gateway-after-restart",
    )
    restarted_gateway = LocalGatewayHarness(settings, restarted_service, second_adapter)
    duplicate_initial = await restarted_gateway._maybe_handle_codex_bridge(initial)
    assert "đăng nhập" in duplicate_initial.response.lower()
    assert resumed_executor.calls == []
    reply = make_local_codex_reply_event(
        "Tôi đã đăng nhập, hãy tiếp tục.",
        prompt_id=prompt_id,
        idempotency_key="reply-key-1",
    )

    first_reply = await restarted_gateway._maybe_handle_codex_bridge(reply)
    duplicate_reply = await restarted_gateway._maybe_handle_codex_bridge(reply)

    assert first_reply.response == "resumed final result"
    assert duplicate_reply.response == "resumed final result"
    assert resumed_executor.calls == ["codex-thread-login"]
    assert second_adapter.messages
    assert {message["chat_id"] for message in second_adapter.messages} == {
        "local-codex-test"
    }
    assert all(message["reply_to"] == "local-reply-1" for message in second_adapter.messages)
    assert restarted_service.store.get_pending_question(prompt_id).status == "answered"
    mapping = restarted_service.store.get_by_idempotency("initial-needs-user")
    assert mapping.codex_thread_id == "codex-thread-login"
    assert mapping.phase == "done"


@pytest.mark.asyncio
async def test_reply_from_different_origin_is_rejected_without_resuming(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store_path = tmp_path / "bridge.db"
    settings = _settings(workspace)
    service = CodexBridgeService(
        settings,
        store=BridgeStore(store_path),
        executor=NeedsLoginExecutor(),
        instance_id="gateway-a",
    )
    adapter = LocalCodexTestAdapter()
    gateway = LocalGatewayHarness(settings, service, adapter)
    initial = make_local_codex_event(
        "work",
        workspace=str(workspace),
        idempotency_key="origin-initial",
    )
    await gateway._maybe_handle_codex_bridge(initial)
    prompt_id = next(
        message["metadata"]["codex_bridge_event"]["progress"]["prompt_id"]
        for message in reversed(adapter.messages)
        if message["metadata"].get("codex_bridge_event", {}).get("phase") == "needs_user"
    )
    resumed = FakeCodexExecutor()
    service.executor = resumed
    wrong_origin = make_local_codex_reply_event(
        "continue",
        prompt_id=prompt_id,
        idempotency_key="wrong-origin-reply",
        conversation_id="different-conversation",
    )

    result = await gateway._maybe_handle_codex_bridge(wrong_origin)

    assert "origin does not match" in result.response
    assert resumed.calls == []
    assert service.store.get_pending_question(prompt_id).status == "pending"


@pytest.mark.asyncio
async def test_disabled_bridge_leaves_direct_gateway_flow_untouched(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    settings = _settings(workspace, enabled=False)
    adapter = LocalCodexTestAdapter()
    gateway = LocalGatewayHarness(settings, None, adapter)
    event = make_local_codex_event(
        "ordinary direct message",
        workspace=str(workspace),
        idempotency_key="direct-key",
    )

    result = await gateway._maybe_handle_codex_bridge(event)

    assert result.handled is False
    assert gateway._codex_bridge_service is None
    assert adapter.messages == []


@pytest.mark.asyncio
async def test_gateway_runner_short_circuits_hermes_agent_for_bridge_request(
    tmp_path, monkeypatch
):
    from gateway.run import GatewayRunner

    workspace = tmp_path / "repo"
    workspace.mkdir()
    settings = _settings(workspace)
    adapter = LocalCodexTestAdapter()
    executor = FakeCodexExecutor()
    service = CodexBridgeService(
        settings,
        store=BridgeStore(tmp_path / "bridge.db"),
        executor=executor,
        instance_id="gateway-a",
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.LOCAL: PlatformConfig(enabled=True)}
    )
    runner.adapters = {Platform.LOCAL: adapter}
    runner.session_store = MagicMock()
    runner._startup_restore_in_progress = False
    runner._running_agents = {}
    runner._codex_bridge_service = service
    runner._codex_bridge_settings = lambda: settings
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda _source: True
    runner._handle_message_with_agent = AsyncMock(return_value="wrong executor")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *args, **kwargs: [])
    event = make_local_codex_event(
        "Execute in Codex",
        workspace=str(workspace),
        idempotency_key="runner-key",
    )

    result = await adapter.dispatch(runner, event)

    assert result == "Codex final result"
    assert executor.calls == [None]
    assert adapter.messages[-1]["content"] == "Codex final result"
    assert adapter.messages[-1]["chat_id"] == "local-codex-test"
    assert adapter.messages[-1]["metadata"]["codex_bridge_final"] is True
    runner._handle_message_with_agent.assert_not_awaited()


def test_workspace_allowlist_rejects_sibling_and_requires_explicit_root(tmp_path):
    allowed = tmp_path / "allowed"
    sibling = tmp_path / "allowed-sibling"
    allowed.mkdir()
    sibling.mkdir()
    assert validate_workspace(str(allowed), (str(allowed),)) == allowed.resolve()
    with pytest.raises(ValueError, match="outside"):
        validate_workspace(str(sibling), (str(allowed),))
    with pytest.raises(ValueError, match="allowlist"):
        validate_workspace(str(allowed), ())


def test_phase_contract_and_legacy_worker_gate_default_off(tmp_path):
    assert BRIDGE_PHASES == {
        "captured",
        "working",
        "needs_user",
        "output_ready",
        "done",
        "failed",
    }
    with pytest.raises(ValueError):
        ProgressEvent(
            event_id="event",
            task_id="task",
            executor="codex",
            phase="private_reasoning",
            summary="must never persist",
            origin={},
            created_at="now",
            idempotency_key="key",
        )
    config = tmp_path / "config.yaml"
    config.write_text("kanban:\n  dispatch_in_gateway: true\n", encoding="utf-8")
    assert legacy_workers_auto_dispatch_enabled(config) is False
    config.write_text(
        "legacy_hermes_workers:\n  auto_dispatch_enabled: true\n",
        encoding="utf-8",
    )
    assert legacy_workers_auto_dispatch_enabled(config) is True
