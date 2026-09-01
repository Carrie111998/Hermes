"""Regression tests for credential redaction at the MCP result boundary."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import mcp_tool


class _FakeContentBlock:
    def __init__(self, text: str, block_type: str = "text"):
        self.text = text
        self.type = block_type


class _FakeCallToolResult:
    def __init__(self, content, *, structured_content=None, meta=None, is_error=False):
        self.content = content
        self.structuredContent = structured_content
        self.meta = meta
        self.isError = is_error


def _run_on_mcp_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:

        async def _run():
            for server in mcp_tool._servers.values():
                if getattr(server, "_rpc_lock", None) is None:
                    server._rpc_lock = asyncio.Lock()
            return await coro

        return loop.run_until_complete(_run())
    finally:
        loop.close()


@pytest.fixture
def _mcp_server():
    session = MagicMock()
    server = SimpleNamespace(session=session, _rpc_lock=None)
    with (
        patch.dict(mcp_tool._servers, {"redaction-server": server}),
        patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_on_mcp_loop),
    ):
        yield session


def _call_result(session, *, content, structured_content=None, meta=None):
    session.call_tool = AsyncMock(
        return_value=_FakeCallToolResult(
            content,
            structured_content=structured_content,
            meta=meta,
        )
    )
    handler = mcp_tool._make_tool_handler("redaction-server", "read-data", 30.0)
    return json.loads(handler({}))


def test_nested_structured_success_result_redacts_credentials(_mcp_server):
    """Credentials cannot survive nested structured MCP serialization."""
    secrets = {
        "api_key": "sk-proj-nested-1234567890ABCDEFGHIJ",
        "access_token": "ghp_nested_1234567890ABCDEFGHIJ",
        "password": "nested-password-1234567890",
        "server_token": "Bearer nested-server-token-1234567890",
        "resource_secret": "DATABASE_PASSWORD=nested-db-password-1234567890",
    }
    structured = {
        "server_metadata": {"access_token": secrets["access_token"]},
        "resource": {"text": secrets["resource_secret"]},
        "image": {"mimeType": "image/png", "api_key": secrets["api_key"]},
        "audio": {"mimeType": "audio/wav", "token": secrets["server_token"]},
        "tool_call": {
            "name": "lookup",
            "arguments": {"password": secrets["password"]},
        },
    }

    data = _call_result(_mcp_server, content=[], structured_content=structured)
    serialized = json.dumps(data, ensure_ascii=False)

    for secret in secrets.values():
        assert secret not in serialized
    assert "***" in serialized
    assert data["result"]["server_metadata"]["access_token"] != secrets["access_token"]
    assert data["result"]["resource"]["text"] != secrets["resource_secret"]
    assert data["result"]["image"]["api_key"] == "***"
    assert data["result"]["audio"]["token"] == "***"
    assert data["result"]["tool_call"]["arguments"]["password"] != secrets["password"]


def test_raw_opaque_resource_text_is_redacted_before_return(_mcp_server):
    """Opaque structured resource text cannot bypass the success boundary."""
    opaque = "opaque-resource-text-r3-cand-003-1234567890"

    with patch("agent.redact._REDACT_ENABLED", False):
        data = _call_result(
            _mcp_server,
            content=[],
            structured_content={"resource": {"text": opaque}},
        )
    serialized = json.dumps(data, ensure_ascii=False)

    assert opaque not in serialized
    assert data["result"]["resource"]["text"] != opaque


def test_primary_content_preserves_non_secret_opaque_values(_mcp_server):
    """Valid opaque identifiers survive primary content and ordinary prose."""
    ordinary_values = (
        "ordinary-long-nonsecret-identifier-1234567890",
        "documentation-release-id-2026-08-30-0001",
    )
    ordinary_prose = "release identifiers: " + ", ".join(ordinary_values)
    credential_value = "ordinary-long-nonsecret-identifier-1234567890"

    data = _call_result(
        _mcp_server,
        content=[_FakeContentBlock(ordinary_prose)],
        structured_content={"password": credential_value},
    )

    assert data["result"] == ordinary_prose
    assert data["structuredContent"]["password"] == "***"


def test_all_structured_free_text_positions_are_forced_redacted(_mcp_server):
    """Every nested MCP prose field gets forced opaque-value protection."""
    markers = {
        "resource_description": "opaque-resource-description-r3-123456",
        "nested_text": "opaque-nested-text-r3-123456",
        "prompt_content": "opaque-prompt-content-r3-123456",
        "prompt_description": "opaque-prompt-description-r3-123456",
    }
    data = _call_result(
        _mcp_server,
        content=[_FakeContentBlock("valid primary MCP text")],
        structured_content={
            "resource": {
                "description": markers["resource_description"],
                "nested": {"text": markers["nested_text"]},
            },
            "prompt": {
                "content": markers["prompt_content"],
                "description": markers["prompt_description"],
            },
            "uri": "https://example.test/report?page=2",
        },
    )

    serialized = json.dumps(data, ensure_ascii=False)
    for marker in markers.values():
        assert marker not in serialized
    assert data["result"] == "valid primary MCP text"
    assert data["structuredContent"]["uri"] == "https://example.test/report?page=2"


def test_error_prose_redacts_non_pattern_opaque_credentials(_mcp_server):
    """Credential prose is redacted even when the value has no known shape."""
    marker = "opaque-error-prose-r3-123456"
    result = _FakeCallToolResult(
        [_FakeContentBlock(f"request failed: the credential presented was {marker}")],
        is_error=True,
    )
    _mcp_server.call_tool = AsyncMock(return_value=result)
    handler = mcp_tool._make_tool_handler("redaction-server", "read-data", 30.0)

    data = json.loads(handler({}))

    assert marker not in json.dumps(data, ensure_ascii=False)
    assert "credential presented" in data["error"]
    assert data["error"] != "MCP tool returned an error"



def test_success_text_and_meta_are_redacted_before_return(_mcp_server):
    """Rendered text and nested model-facing metadata share the boundary."""
    text_secret = "OPENAI_API_KEY=sk-text-nested-1234567890ABCDEFGHIJ"
    meta_secret = "ghp_meta_nested_1234567890ABCDEFGHIJ"

    data = _call_result(
        _mcp_server,
        content=[_FakeContentBlock(text_secret)],
        meta={"com.example/metadata": {"token": meta_secret}},
    )

    serialized = json.dumps(data, ensure_ascii=False)
    assert text_secret not in serialized
    assert meta_secret not in serialized
    assert data["result"] != text_secret
    assert data["_meta"]["com.example/metadata"]["token"] != meta_secret


def test_secret_context_survives_nested_collections_and_meta(_mcp_server):
    """Opaque credentials stay redacted below lists, tuples, and maps."""
    secrets = {
        "password": "opaque-password-deep",
        "authorization": "opaque-authorization-deep",
        "bearer": "opaque-bearer-deep",
    }
    structured = {
        "password": [{"deep": (secrets["password"],)}],
        "authorization": {"items": [secrets["authorization"]]},
    }
    meta = {"vendor.example/data": {"bearer": [{"value": secrets["bearer"]}]}}

    data = _call_result(
        _mcp_server,
        content=[],
        structured_content=structured,
        meta=meta,
    )
    serialized = json.dumps(data, ensure_ascii=False)

    for secret in secrets.values():
        assert secret not in serialized
    assert data["result"]["password"][0]["deep"][0] == "***"
    assert data["result"]["authorization"]["items"][0] == "***"
    assert data["_meta"]["vendor.example/data"]["bearer"][0]["value"] == "***"


def test_error_results_use_the_forced_redaction_boundary(_mcp_server):
    """Error text and embedded-resource text cannot bypass result redaction."""
    opaque = "opaque-error-password"
    result = _FakeCallToolResult(
        [
            _FakeContentBlock(f"request failed password={opaque}"),
            SimpleNamespace(
                type="resource",
                resource=SimpleNamespace(text=f"authorization={opaque}"),
            ),
        ],
        is_error=True,
    )
    _mcp_server.call_tool = AsyncMock(return_value=result)
    handler = mcp_tool._make_tool_handler("redaction-server", "read-data", 30.0)

    data = json.loads(handler({}))

    assert opaque not in json.dumps(data, ensure_ascii=False)
    assert "[REDACTED]" in data["error"] or "***" in data["error"]


def test_resource_link_credentials_are_redacted_but_public_query_survives(_mcp_server):
    """Model-facing ResourceLinks redact token/signature query values."""
    secret_token = "opaque-resource-token"
    secret_signature = "opaque-resource-signature"
    public_uri = "https://example.test/resource?page=2"
    link = SimpleNamespace(
        type="resource_link",
        uri=(
            "https://example.test/resource?token="
            f"{secret_token}&X-Amz-Signature={secret_signature}&page=2"
        ),
        name="report",
        mimeType="text/plain",
    )

    data = _call_result(_mcp_server, content=[link])
    rendered = data["result"]

    assert secret_token not in rendered
    assert secret_signature not in rendered
    assert "https://example.test/resource?" in rendered
    assert "page=2" in rendered


class _FakeResource:
    def __init__(self, uri, description="", mime_type="text/plain"):
        self.uri = uri
        self.name = "resource"
        self.description = description
        self.mimeType = mime_type


class _FakePromptArgument:
    def __init__(self, name, description):
        self.name = name
        self.description = description
        self.required = True


def _utility_session_result(session, factory, method_name, value, args=None):
    """Run one generated utility handler through the public sync boundary."""
    setattr(session, method_name, AsyncMock(return_value=value))
    handler = factory("redaction-server", 30.0)
    return json.loads(handler(args or {}))


@pytest.fixture
def _utility_server():
    session = MagicMock()
    server = SimpleNamespace(session=session, _rpc_lock=None)
    with (
        patch.dict(mcp_tool._servers, {"redaction-server": server}),
        patch("tools.mcp_tool._get_connected_server_for_call", return_value=server),
        patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_on_mcp_loop),
        patch("tools.mcp_tool._mark_server_call_started"),
    ):
        yield session


def test_all_generated_resource_and_prompt_handlers_share_the_boundary(_utility_server):
    """Every model-facing MCP utility handler redacts opaque nested values."""
    resources = SimpleNamespace(
        resources=[
            _FakeResource(
                "https://example.test/r?api_key=opaque-list-resource",
                description="token=opaque-resource-description",
            )
        ],
        nextCursor=None,
    )
    read = SimpleNamespace(
        contents=[SimpleNamespace(text="password=opaque-read-resource")]
    )
    prompts = SimpleNamespace(
        prompts=[
            SimpleNamespace(
                name="lookup",
                description="bearer=opaque-prompt-description",
                arguments=[_FakePromptArgument("arg", "secret=opaque-argument")],
            )
        ],
        nextCursor=None,
    )
    prompt = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="user",
                content=SimpleNamespace(text="authorization=opaque-prompt-content"),
            )
        ],
        description="client_secret=opaque-prompt-result",
    )

    results = [
        _utility_session_result(
            _utility_server,
            mcp_tool._make_list_resources_handler,
            "list_resources",
            resources,
        ),
        _utility_session_result(
            _utility_server,
            mcp_tool._make_read_resource_handler,
            "read_resource",
            read,
            {"uri": "memory://report"},
        ),
        _utility_session_result(
            _utility_server,
            mcp_tool._make_list_prompts_handler,
            "list_prompts",
            prompts,
        ),
        _utility_session_result(
            _utility_server,
            mcp_tool._make_get_prompt_handler,
            "get_prompt",
            prompt,
            {"name": "lookup"},
        ),
    ]

    serialized = json.dumps(results, ensure_ascii=False)
    for marker in (
        "opaque-list-resource",
        "opaque-resource-description",
        "opaque-read-resource",
        "opaque-prompt-description",
        "opaque-argument",
        "opaque-prompt-content",
        "opaque-prompt-result",
    ):
        assert marker not in serialized
    assert "https://example.test/r?api_key=***" in serialized


def test_opaque_authorization_schemes_are_absent_from_all_success_handlers(
    _utility_server,
):
    """Every model-facing success route removes opaque Authorization values."""
    markers = {
        "bearer": "opaque-auth-bearer-r3",
        "basic": "opaque-auth-basic-r3",
        "token": "opaque-auth-token-r3",
        "bare": "opaque-auth-bare-r3",
    }
    values = {
        "bearer": f"Authorization: Bearer {markers['bearer']}",
        "basic": f"Authorization: Basic {markers['basic']}",
        "token": f"Authorization: Token {markers['token']}",
        "bare": f"Authorization: {markers['bare']}",
    }

    primary = _call_result(
        _utility_server,
        content=[_FakeContentBlock("\n".join(values.values()))],
    )
    resources = SimpleNamespace(
        resources=[_FakeResource("memory://report", description=values["bearer"])],
        nextCursor=None,
    )
    read = SimpleNamespace(contents=[SimpleNamespace(text=values["basic"])])
    prompts = SimpleNamespace(
        prompts=[
            SimpleNamespace(
                name="lookup",
                description=values["token"],
                arguments=[],
            )
        ],
        nextCursor=None,
    )
    prompt = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="user",
                content=SimpleNamespace(text=values["bare"]),
            )
        ],
        description=values["bearer"],
    )
    sibling_results = [
        _utility_session_result(
            _utility_server,
            mcp_tool._make_list_resources_handler,
            "list_resources",
            resources,
        ),
        _utility_session_result(
            _utility_server,
            mcp_tool._make_read_resource_handler,
            "read_resource",
            read,
            {"uri": "memory://report"},
        ),
        _utility_session_result(
            _utility_server,
            mcp_tool._make_list_prompts_handler,
            "list_prompts",
            prompts,
        ),
        _utility_session_result(
            _utility_server,
            mcp_tool._make_get_prompt_handler,
            "get_prompt",
            prompt,
            {"name": "lookup"},
        ),
    ]

    serialized = json.dumps([primary, *sibling_results], ensure_ascii=False)
    for marker in markers.values():
        assert marker not in serialized


def test_hostile_structured_extras_degrade_without_losing_primary_text(_mcp_server):
    """Cycles and excessive nesting cannot abort or replace valid text."""
    marker = "opaque-cycle-auth-r3"
    cyclic = []
    cyclic.append(cyclic)
    deeply_nested = marker
    for _ in range(100):
        deeply_nested = {"level": deeply_nested}

    data = _call_result(
        _mcp_server,
        content=[_FakeContentBlock("valid primary MCP text")],
        structured_content={"cycle": cyclic, "deep": deeply_nested},
        meta={"cycle": cyclic, "set_extra": {"unsupported"}},
    )

    serialized = json.dumps(data, ensure_ascii=False)
    assert marker not in serialized
    assert data["result"] == "valid primary MCP text"


def test_list_resources_redacts_page_two_entries(_utility_server):
    """Resource entries on later pages use the same forced boundary."""
    marker = "opaque-page-2-resource-description-r3-123456"
    _utility_server.list_resources = AsyncMock(side_effect=[
        SimpleNamespace(
            resources=[_FakeResource("memory://page-1")],
            nextCursor="page-2",
        ),
        SimpleNamespace(
            resources=[_FakeResource(
                "https://example.test/report?token=opaque-page-2-token&page=2",
                description=marker,
            )],
            nextCursor=None,
        ),
    ])
    handler = mcp_tool._make_list_resources_handler("redaction-server", 30.0)

    data = json.loads(handler({}))
    serialized = json.dumps(data, ensure_ascii=False)

    assert marker not in serialized
    assert "opaque-page-2-token" not in serialized
    assert "https://example.test/report?token=***&page=2" in serialized
    assert "memory://page-1" in serialized


def test_read_resource_redacts_model_facing_text(_utility_server):
    """Read-resource text is sanitized before it becomes the result string."""
    marker = "opaque-read-resource-page-2-r3-123456"
    _utility_server.read_resource = AsyncMock(
        return_value=SimpleNamespace(
            contents=[SimpleNamespace(text=marker)],
        )
    )
    handler = mcp_tool._make_read_resource_handler("redaction-server", 30.0)

    data = json.loads(handler({"uri": "memory://page-2"}))

    assert marker not in json.dumps(data, ensure_ascii=False)
    assert data["result"] == "***"


def test_free_text_context_survives_sequences_and_arbitrary_keys(_mcp_server):
    """Free-text policy survives list/tuple containers and arbitrary keys."""
    markers = {
        "list": "opaque-list-text-r3-cand-003-1234567890",
        "tuple": "opaque-tuple-text-r3-cand-003-1234567890",
        "nested": "opaque-nested-text-r3-cand-003-1234567890",
        "credential": "opaque-credential-desc-r3-cand-003-1234567890",
    }
    data = _call_result(
        _mcp_server,
        content=[_FakeContentBlock("valid primary MCP text")],
        structured_content={
            "description": [
                markers["list"],
                {"arbitrary": {"nested": markers["nested"]}},
                (markers["tuple"],),
                {"password": markers["credential"]},
            ],
        },
    )
    serialized = json.dumps(data, ensure_ascii=False)
    for marker in markers.values():
        assert marker not in serialized
    assert data["result"] == "valid primary MCP text"


def test_top_level_structured_scalar_is_forced_redacted(_mcp_server):
    """A scalar structuredContent value cannot bypass the root boundary."""
    marker = "opaque-top-level-structured-r3-cand-003-1234567890"
    data = _call_result(_mcp_server, content=[], structured_content=marker)

    assert marker not in json.dumps(data, ensure_ascii=False)
    assert data["result"] == "***"


def test_embedded_resource_text_is_sanitized_before_render_return():
    """EmbeddedResource.resource.text is protected before caller sanitization."""
    from tools.mcp_tool import _render_mcp_resource_block

    marker = "opaque-embedded-resource-r3-cand-003-1234567890"
    block = SimpleNamespace(
        type="resource",
        resource=SimpleNamespace(text=f"ordinary resource text: {marker}"),
    )

    rendered = _render_mcp_resource_block(block, "redaction-server")

    assert marker not in rendered
    assert "ordinary resource text" in rendered


def test_generic_error_prose_and_public_url_are_preserved_safely(_mcp_server):
    """Generic opaque error tokens are removed without losing diagnostics."""
    marker = "opaque-generic-error-r3-cand-003-1234567890"
    public_url = "https://example.test/failure?page=2"
    result = _FakeCallToolResult(
        [_FakeContentBlock(f"request failed ({marker}); see {public_url}")],
        is_error=True,
    )
    _mcp_server.call_tool = AsyncMock(return_value=result)
    handler = mcp_tool._make_tool_handler("redaction-server", "read-data", 30.0)

    data = json.loads(handler({}))

    assert marker not in json.dumps(data, ensure_ascii=False)
    assert "request failed" in data["error"]
    assert public_url in data["error"]


def test_error_sanitization_precedes_truncation(_mcp_server):
    """The original opaque value is gone before error truncation runs."""
    marker = "opaque-ordering-error-r3-cand-003-1234567890"
    result = _FakeCallToolResult([_FakeContentBlock(f"request failed {marker}")], is_error=True)
    _mcp_server.call_tool = AsyncMock(return_value=result)
    calls = []

    def sanitize(text):
        calls.append(("sanitize", marker in text))
        return text.replace(marker, "***")

    def truncate(text):
        calls.append(("truncate", marker in text))
        return text

    handler = mcp_tool._make_tool_handler("redaction-server", "read-data", 30.0)
    with (
        patch("tools.mcp_tool._sanitize_error", side_effect=sanitize),
        patch("tools.mcp_tool._truncate_mcp_text_result", side_effect=truncate),
    ):
        data = json.loads(handler({}))

    assert calls == [("sanitize", True), ("truncate", False)]
    assert marker not in json.dumps(data, ensure_ascii=False)


def test_utility_payloads_drop_non_serializable_extras(_utility_server):
    """Utility success payloads retain valid entries despite hostile extras."""
    resources = SimpleNamespace(
        resources=[
            SimpleNamespace(uri="memory://page-1", name=object(), description=object()),
            SimpleNamespace(uri="memory://page-2", name="valid resource"),
        ],
        nextCursor=None,
    )
    prompts = SimpleNamespace(
        prompts=[
            SimpleNamespace(
                name="page-1",
                description=object(),
                arguments=[_FakePromptArgument("valid-arg", object())],
            ),
            SimpleNamespace(name="page-2", description="valid prompt", arguments=[]),
        ],
        nextCursor=None,
    )
    prompt = SimpleNamespace(
        messages=[
            SimpleNamespace(role="user", content=SimpleNamespace(text=object())),
            SimpleNamespace(role="assistant", content="valid prompt content"),
        ],
        description=object(),
    )
    read = SimpleNamespace(
        contents=[
            SimpleNamespace(text=object()),
            SimpleNamespace(text="valid read content"),
        ],
    )

    results = [
        _utility_session_result(
            _utility_server, mcp_tool._make_list_resources_handler,
            "list_resources", resources,
        ),
        _utility_session_result(
            _utility_server, mcp_tool._make_read_resource_handler,
            "read_resource", read, {"uri": "memory://read"},
        ),
        _utility_session_result(
            _utility_server, mcp_tool._make_list_prompts_handler,
            "list_prompts", prompts,
        ),
        _utility_session_result(
            _utility_server, mcp_tool._make_get_prompt_handler,
            "get_prompt", prompt, {"name": "page-1"},
        ),
    ]
    serialized = json.dumps(results, ensure_ascii=False)

    assert all("error" not in result for result in results)
    assert "memory://page-1" in serialized
    assert "memory://page-2" in serialized
    assert '"name": "page-2"' in serialized
    assert "valid prompt content" in serialized
    assert "valid read content" in serialized


def test_list_prompts_redacts_page_two_entries(_utility_server):
    """Prompt descriptions and argument descriptions are protected on page 2."""
    marker = "opaque-page-2-prompt-description-r3-123456"
    argument_marker = "opaque-page-2-argument-description-r3-123456"
    _utility_server.list_prompts = AsyncMock(side_effect=[
        SimpleNamespace(
            prompts=[SimpleNamespace(name="page-1")],
            nextCursor="page-2",
        ),
        SimpleNamespace(
            prompts=[SimpleNamespace(
                name="page-2",
                description=marker,
                arguments=[_FakePromptArgument("arg", argument_marker)],
            )],
            nextCursor=None,
        ),
    ])
    handler = mcp_tool._make_list_prompts_handler("redaction-server", 30.0)

    data = json.loads(handler({}))
    serialized = json.dumps(data, ensure_ascii=False)

    assert marker not in serialized
    assert argument_marker not in serialized
    assert "page-2" in serialized


def test_get_prompt_redacts_message_content_and_description(_utility_server):
    """Prompt messages and result descriptions share the forced policy."""
    content_marker = "opaque-page-2-prompt-content-r3-123456"
    description_marker = "opaque-page-2-prompt-result-r3-123456"
    _utility_server.get_prompt = AsyncMock(return_value=SimpleNamespace(
        messages=[SimpleNamespace(
            role="user",
            content=SimpleNamespace(text=content_marker),
        )],
        description=description_marker,
    ))
    handler = mcp_tool._make_get_prompt_handler("redaction-server", 30.0)

    data = json.loads(handler({"name": "page-2"}))
    serialized = json.dumps(data, ensure_ascii=False)

    assert content_marker not in serialized
    assert description_marker not in serialized
    assert data["messages"][0]["role"] == "user"
