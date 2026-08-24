from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace

import pytest

import tools.mcp_tool as mcp_tool
from mcp.shared.exceptions import MCPError
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    ErrorData,
    InputRequiredResult,
    ListRootsRequest,
    ListRootsResult,
)
from tools.mcp_protocol import StaleConnectionGenerationError


def _run(coro):
    return asyncio.run(coro)


def _roots_request():
    return ListRootsRequest(params=None)


@pytest.mark.parametrize(
    ("method_name", "args", "kwargs"),
    [
        ("call_tool", ("echo",), {"arguments": {"value": "ok"}}),
        ("get_prompt", ("welcome",), {"arguments": {"name": "Ada"}}),
        ("read_resource", ("memory://item",), {}),
    ],
)
def test_mrtr_success_is_sequential_for_all_result_methods(method_name, args, kwargs):
    async def drive() -> None:
        calls = []
        dispatch_order = []
        active = 0
        max_active = 0

        class Session:
            async def _invoke(self, *method_args, **method_kwargs):
                calls.append((method_args, method_kwargs))
                if len(calls) == 1:
                    return InputRequiredResult(
                        inputRequests={"first": _roots_request(), "second": _roots_request()},
                        requestState="opaque-state",
                    )
                return SimpleNamespace(method=method_name)

            call_tool = _invoke
            get_prompt = _invoke
            read_resource = _invoke

            async def dispatch_input_request(self, _ctx, request):
                nonlocal active, max_active
                key = "first" if not dispatch_order else "second"
                dispatch_order.append(key)
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0)
                active -= 1
                assert isinstance(request, ListRootsRequest)
                return ListRootsResult(roots=[])

        session = Session()
        server = mcp_tool.MCPServerTask("mrtr-success")
        server.session = session
        server._connection_generation = 1
        result = await mcp_tool._call_with_input_required(
            server,
            getattr(session, method_name),
            *args,
            timeout=1.0,
            **kwargs,
        )
        assert result.method == method_name
        assert dispatch_order == ["first", "second"]
        assert max_active == 1
        assert calls[1][1]["request_state"] == "opaque-state"
        assert list(calls[1][1]["input_responses"]) == ["first", "second"]
        assert server._pending_mrtr_tasks == set()

    _run(drive())


def test_mrtr_approval_continues_with_accepted_response():
    async def drive() -> None:
        calls = []

        class Session:
            async def call_tool(self, *args, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return InputRequiredResult(
                        inputRequests={
                            "approval": ElicitRequest(
                                params=ElicitRequestFormParams(
                                    message="Approve operation",
                                    requestedSchema={"type": "object"},
                                )
                            )
                        },
                        requestState="approval-state",
                    )
                return SimpleNamespace(done=True)

            async def dispatch_input_request(self, _ctx, _request):
                return ElicitResult(action="accept", content={})

        session = Session()
        server = mcp_tool.MCPServerTask("mrtr-approval")
        server.session = session
        server._connection_generation = 1
        result = await mcp_tool._call_with_input_required(
            server,
            session.call_tool,
            "approve",
            timeout=1.0,
        )
        assert result.done is True
        assert calls[1]["input_responses"]["approval"].action == "accept"

    _run(drive())


def test_mrtr_denial_fails_closed_without_retry():
    async def drive() -> None:
        calls = 0

        class Session:
            async def call_tool(self, *_args, **_kwargs):
                nonlocal calls
                calls += 1
                return InputRequiredResult(
                    inputRequests={"approval": _roots_request()},
                    requestState="state",
                )

            async def dispatch_input_request(self, _ctx, _request):
                return ErrorData(code=-32000, message="denied")

        session = Session()
        server = mcp_tool.MCPServerTask("mrtr-denial")
        server.session = session
        server._connection_generation = 1
        with pytest.raises(MCPError, match="denied"):
            await mcp_tool._call_with_input_required(
                server,
                session.call_tool,
                "deny",
                timeout=1.0,
            )
        assert calls == 1

    _run(drive())


def test_mrtr_whole_operation_timeout_cleans_task_registry():
    async def drive() -> None:
        class Session:
            async def call_tool(self, *_args, **_kwargs):
                return InputRequiredResult(
                    inputRequests={"blocked": _roots_request()},
                    requestState="state",
                )

            async def dispatch_input_request(self, _ctx, _request):
                await asyncio.Event().wait()

        session = Session()
        server = mcp_tool.MCPServerTask("mrtr-timeout")
        server.session = session
        server._connection_generation = 1
        with pytest.raises(TimeoutError):
            await mcp_tool._call_with_input_required(
                server,
                session.call_tool,
                "timeout",
                timeout=0.01,
            )
        assert server._pending_mrtr_tasks == set()

    _run(drive())


def test_mrtr_caller_cancellation_propagates_and_cleans_task_registry():
    async def drive() -> None:
        entered = asyncio.Event()

        class Session:
            async def call_tool(self, *_args, **_kwargs):
                return InputRequiredResult(
                    inputRequests={"blocked": _roots_request()},
                    requestState="state",
                )

            async def dispatch_input_request(self, _ctx, _request):
                entered.set()
                await asyncio.Event().wait()

        session = Session()
        server = mcp_tool.MCPServerTask("mrtr-cancel")
        server.session = session
        server._connection_generation = 1
        operation = asyncio.create_task(
            mcp_tool._call_with_input_required(
                server,
                session.call_tool,
                "cancel",
                timeout=5.0,
            )
        )
        await entered.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert server._pending_mrtr_tasks == set()

    _run(drive())


def test_mrtr_rejects_malformed_empty_continuation():
    async def drive() -> None:
        class Session:
            async def call_tool(self, *_args, **_kwargs):
                return InputRequiredResult.model_construct(
                    input_requests=None,
                    request_state=None,
                )

            async def dispatch_input_request(self, _ctx, _request):
                raise AssertionError("malformed continuation must not dispatch")

        session = Session()
        server = mcp_tool.MCPServerTask("mrtr-malformed")
        server.session = session
        server._connection_generation = 1
        with pytest.raises(RuntimeError, match="malformed"):
            await mcp_tool._call_with_input_required(
                server,
                session.call_tool,
                "malformed",
                timeout=1.0,
            )

    _run(drive())


def test_mrtr_transport_disconnect_propagates():
    async def drive() -> None:
        calls = 0

        class Session:
            async def call_tool(self, *_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return InputRequiredResult(requestState="resume")
                raise ConnectionError("transport lost")

            async def dispatch_input_request(self, _ctx, _request):
                raise AssertionError("state-only continuation must not dispatch")

        session = Session()
        server = mcp_tool.MCPServerTask("mrtr-disconnect")
        server.session = session
        server._connection_generation = 1
        with pytest.raises(ConnectionError, match="transport lost"):
            await mcp_tool._call_with_input_required(
                server,
                session.call_tool,
                "disconnect",
                timeout=1.0,
            )

    _run(drive())


def test_mrtr_round_limit_exhaustion_is_bounded():
    async def drive() -> None:
        class Session:
            async def call_tool(self, *_args, **_kwargs):
                return InputRequiredResult(requestState="again")

            async def dispatch_input_request(self, _ctx, _request):
                raise AssertionError("state-only continuation must not dispatch")

        session = Session()
        server = mcp_tool.MCPServerTask("mrtr-round-limit")
        server.session = session
        server._config = {"input_required_max_rounds": 2}
        server._connection_generation = 1
        with pytest.raises(RuntimeError, match="more than 2 rounds"):
            await mcp_tool._call_with_input_required(
                server,
                session.call_tool,
                "loop",
                timeout=1.0,
            )

    _run(drive())


def test_mrtr_generation_replacement_rejects_stale_continuation():
    async def drive() -> None:
        entered = asyncio.Event()

        class Session:
            async def call_tool(self, *_args, **_kwargs):
                return InputRequiredResult(
                    inputRequests={"blocked": _roots_request()},
                    requestState="old-generation",
                )

            async def dispatch_input_request(self, _ctx, _request):
                entered.set()
                await asyncio.Event().wait()
                return ListRootsResult(roots=[])

        session = Session()
        server = mcp_tool.MCPServerTask("mrtr-generation")
        server.session = session
        server._connection_generation = 1
        operation = asyncio.create_task(
            mcp_tool._call_with_input_required(
                server,
                session.call_tool,
                "generation",
                timeout=1.0,
            )
        )
        await entered.wait()
        assert await server._begin_connection_generation() == 2
        server.session = SimpleNamespace()
        with pytest.raises(StaleConnectionGenerationError):
            await operation
        assert server._pending_mrtr_tasks == set()

    _run(drive())


def test_stale_mrtr_finalizer_cannot_clear_new_request_context():
    async def drive() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class Session:
            async def call_tool(self, *_args, **_kwargs):
                return InputRequiredResult(
                    inputRequests={"blocked": _roots_request()},
                    requestState="old",
                )

            async def dispatch_input_request(self, _ctx, _request):
                entered.set()
                await release.wait()
                return ListRootsResult(roots=[])

        session = Session()
        server = mcp_tool.MCPServerTask("mrtr-correlation")
        server.session = session
        server._connection_generation = 1
        operation = asyncio.create_task(
            mcp_tool._call_with_input_required(
                server,
                session.call_tool,
                "correlation",
                timeout=1.0,
            )
        )
        await entered.wait()
        new_context = contextvars.copy_context()
        server._connection_generation = 2
        server.session = SimpleNamespace()
        server._pending_call_context = new_context
        server._pending_call_generation = 2
        release.set()
        with pytest.raises(StaleConnectionGenerationError):
            await operation
        assert server._pending_call_context is new_context
        assert server._pending_call_generation == 2

    _run(drive())
