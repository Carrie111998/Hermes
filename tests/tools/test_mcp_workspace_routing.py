import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def _run_on_loop(coro_or_factory, timeout=30):
    del timeout
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    return asyncio.run(coro)


def _result(text):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        isError=False,
        structuredContent=None,
    )


def test_workspace_sensitive_server_routes_each_task_to_its_workspace(monkeypatch):
    import tools.mcp_tool as mcp_tool

    roots = {
        "session-alpha": "/projects/alpha",
        "session-beta": "/projects/beta",
    }
    template = {
        "command": "filesystem-server",
        "args": ["${workspaceFolder}"],
    }
    seed = mcp_tool.MCPServerTask("filesystem")
    seed.session = MagicMock()
    seed._config = {
        "command": "filesystem-server",
        "args": ["/backend/workspace"],
    }
    created = []

    async def fake_connect(name, config, *, publish_tools=True):
        root = config["args"][0]
        session = MagicMock()
        session.call_tool = AsyncMock(return_value=_result(root))
        server = mcp_tool.MCPServerTask(name, publish_tools=publish_tools)
        server.session = session
        server._config = config
        created.append((root, publish_tools))
        return server

    monkeypatch.setattr(
        mcp_tool,
        "_workspace_folder",
        lambda task_id=None: roots.get(task_id, "/backend/workspace"),
    )
    monkeypatch.setattr(mcp_tool, "_connect_server", fake_connect)
    monkeypatch.setattr(mcp_tool, "_ensure_mcp_loop", lambda: None)
    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", _run_on_loop)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"mcp_servers": {"filesystem": template}},
    )
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager",
        lambda: SimpleNamespace(get_portable_mcp_servers=lambda: {}),
    )
    mcp_tool._servers["filesystem"] = seed
    mcp_tool._workspace_servers.clear()
    mcp_tool._workspace_server_connecting.clear()

    try:
        loaded = mcp_tool._load_mcp_config()
        assert loaded["filesystem"]["args"] == ["/backend/workspace"]
        handler = mcp_tool._make_tool_handler("filesystem", "root", 120)
        alpha = json.loads(handler({}, task_id="session-alpha"))["result"]
        beta = json.loads(handler({}, task_id="session-beta"))["result"]
        alpha_again = json.loads(handler({}, task_id="session-alpha"))["result"]

        assert (alpha, beta, alpha_again) == (
            "/projects/alpha",
            "/projects/beta",
            "/projects/alpha",
        )
        assert created == [
            ("/projects/alpha", False),
            ("/projects/beta", False),
        ]
    finally:
        mcp_tool._servers.pop("filesystem", None)
        mcp_tool._workspace_server_configs.clear()
        mcp_tool._workspace_servers.clear()
        mcp_tool._workspace_server_connecting.clear()


def test_generated_utility_handlers_forward_the_calling_session(monkeypatch):
    import tools.mcp_tool as mcp_tool

    session = MagicMock()
    session.list_resources = AsyncMock(
        return_value=SimpleNamespace(resources=[])
    )
    session.read_resource = AsyncMock(
        return_value=SimpleNamespace(contents=[])
    )
    session.list_prompts = AsyncMock(
        return_value=SimpleNamespace(prompts=[])
    )
    session.get_prompt = AsyncMock(
        return_value=SimpleNamespace(messages=[], description=None)
    )
    server = mcp_tool.MCPServerTask("filesystem")
    server.session = session
    resolved = []

    def resolve(server_name, task_id=None):
        resolved.append((server_name, task_id))
        return server

    monkeypatch.setattr(mcp_tool, "_get_connected_server_for_call", resolve)
    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", _run_on_loop)

    handlers = [
        (mcp_tool._make_list_resources_handler("filesystem", 120), {}),
        (
            mcp_tool._make_read_resource_handler("filesystem", 120),
            {"uri": "file:///workspace/readme.md"},
        ),
        (mcp_tool._make_list_prompts_handler("filesystem", 120), {}),
        (
            mcp_tool._make_get_prompt_handler("filesystem", 120),
            {"name": "summary", "arguments": {}},
        ),
    ]
    for handler, args in handlers:
        assert "error" not in json.loads(
            handler(args, session_id="workspace-session")
        )

    assert resolved == [
        ("filesystem", "workspace-session"),
        ("filesystem", "workspace-session"),
        ("filesystem", "workspace-session"),
        ("filesystem", "workspace-session"),
    ]


def test_workspace_sensitive_circuit_breaker_is_scoped_by_workspace(monkeypatch):
    import tools.mcp_tool as mcp_tool

    roots = {
        "session-alpha": "/projects/alpha",
        "session-beta": "/projects/beta",
    }
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=_result("beta"))
    server = mcp_tool.MCPServerTask("filesystem")
    server.session = session
    resolved = []

    monkeypatch.setattr(
        mcp_tool,
        "_workspace_folder",
        lambda task_id=None: roots.get(task_id, "/backend/workspace"),
    )
    monkeypatch.setattr(
        mcp_tool,
        "_get_connected_server_for_call",
        lambda server_name, task_id=None: (
            resolved.append((server_name, task_id))
            or (server if task_id == "session-beta" else None)
        ),
    )
    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", _run_on_loop)
    mcp_tool._workspace_server_configs["filesystem"] = {
        "command": "filesystem-server",
        "args": ["${workspaceFolder}"],
    }
    try:
        handler = mcp_tool._make_tool_handler("filesystem", "root", 120)
        for _ in range(mcp_tool._CIRCUIT_BREAKER_THRESHOLD):
            assert "error" in json.loads(handler({}, task_id="session-alpha"))

        result = handler({}, task_id="session-beta")

        assert resolved[-1] == ("filesystem", "session-beta")
        assert json.loads(result) == {"result": "beta"}
    finally:
        mcp_tool._workspace_server_configs.clear()
        mcp_tool._server_error_counts.clear()
        mcp_tool._server_breaker_opened_at.clear()


def test_shutdown_clears_workspace_scoped_circuit_breakers():
    import tools.mcp_tool as mcp_tool

    workspace_key = ("filesystem", "/projects/alpha")
    mcp_tool._server_error_counts[workspace_key] = (
        mcp_tool._CIRCUIT_BREAKER_THRESHOLD
    )
    mcp_tool._server_breaker_opened_at[workspace_key] = 1.0

    mcp_tool.shutdown_mcp_servers()

    assert workspace_key not in mcp_tool._server_error_counts
    assert workspace_key not in mcp_tool._server_breaker_opened_at
