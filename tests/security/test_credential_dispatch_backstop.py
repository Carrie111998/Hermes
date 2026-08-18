"""Layer 2 wired into ToolRegistry.dispatch (Phase 9 / Packet C, C6).

dispatch is the universal funnel: every builtin and MCP tool returns through
it. These tests cover the surfaces layer 1 structurally cannot reach, and pin
the ones neither layer reaches.
"""

import json

import pytest

from agent import credential_tripwire as ct
from tools.registry import ToolRegistry
from tests.security.test_credential_read_boundary import (
    ENV_BODY,
    OPQ_KEY,
    OPQ_VALUE,
    YELP_KEY,
    YELP_VALUE,
)

SCHEMA = {"name": "x", "description": "", "input_schema": {"type": "object"}}


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(ENV_BODY)
    ct.reset_cache()
    yield tmp_path
    ct.reset_cache()


def _registry(handler, name="probe"):
    """A fresh registry — never the global singleton (xdist safety)."""
    reg = ToolRegistry()
    reg.register(name=name, toolset="test", schema=SCHEMA,
                 handler=handler, check_fn=lambda: True)
    return reg


# --- the catch ---------------------------------------------------------------

def test_scrubs_assignment_form_from_a_tool_result(seeded):
    reg = _registry(lambda args, **kw: json.dumps({"output": ENV_BODY}))
    out = reg.dispatch("probe", {})
    assert YELP_VALUE not in out
    assert OPQ_VALUE not in out
    assert ct.REDACTION_MARKER in out


def test_scrubs_bare_value_form_from_a_tool_result(seeded):
    """No key name present: no content pattern in agent/redact.py can catch
    this. Only the known-value set can."""
    reg = _registry(lambda args, **kw: json.dumps({"stdout": OPQ_VALUE}))
    out = reg.dispatch("probe", {})
    assert OPQ_VALUE not in out


def test_covers_a_simulated_mcp_tool(seeded):
    """MCP tools register through the same registry, so they inherit this."""
    reg = _registry(lambda args, **kw: json.dumps({"content": ENV_BODY}),
                    name="mcp_filesystem_read_file")
    out = reg.dispatch("mcp_filesystem_read_file", {"path": "/whatever/.env"})
    assert YELP_VALUE not in out


def test_covers_a_simulated_background_log_read(seeded):
    """`cat .env &` then read the log: the log read returns via dispatch."""
    reg = _registry(lambda args, **kw: json.dumps({"log": ENV_BODY}), name="process")
    out = reg.dispatch("process", {"action": "read_log"})
    assert YELP_VALUE not in out


# --- C2 non-regression -------------------------------------------------------

def test_ordinary_results_are_byte_identical(seeded):
    """The C2 regression test: nothing legitimate may be mangled."""
    payload = json.dumps({
        "arguments": {"key": "Enter", "selector": "#submit"},
        "properties": {"key": {"type": "string", "description": "key to press"}},
    })
    reg = _registry(lambda args, **kw: payload)
    assert reg.dispatch("probe", {}) == payload


def test_arguments_are_never_inspected_or_rewritten(seeded):
    """Layer 2 sees results only. Args and schemas are untouched by design."""
    seen = {}

    def handler(args, **kw):
        seen.update(args)
        return json.dumps(args)

    reg = _registry(handler)
    out = reg.dispatch("probe", {"key": "Enter", "path": "/tmp/x"})
    assert seen == {"key": "Enter", "path": "/tmp/x"}
    assert json.loads(out) == {"key": "Enter", "path": "/tmp/x"}


# --- fail-closed -------------------------------------------------------------

def test_withholds_the_result_when_the_scan_itself_fails(seeded, monkeypatch):
    """Unlike the transform_tool_result seam, this must not fail open."""
    def boom(_text):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(ct, "scrub_known_secrets", boom)
    reg = _registry(lambda args, **kw: json.dumps({"output": ENV_BODY}))
    out = reg.dispatch("probe", {})
    assert YELP_VALUE not in out
    assert "withheld" in json.loads(out).get("error", "")


def test_handler_exceptions_still_report_normally(seeded):
    def handler(args, **kw):
        raise ValueError("ordinary failure")

    reg = _registry(handler)
    assert "ordinary failure" in json.loads(reg.dispatch("probe", {}))["error"]


def test_unknown_tool_is_unaffected(seeded):
    reg = ToolRegistry()
    assert "Unknown tool" in json.loads(reg.dispatch("nope", {}))["error"]


# --- residual: pinned --------------------------------------------------------

def test_encoded_output_is_NOT_caught_at_dispatch(seeded):
    import base64
    encoded = base64.b64encode(OPQ_VALUE.encode()).decode()
    reg = _registry(lambda args, **kw: json.dumps({"stdout": encoded}))
    out = reg.dispatch("probe", {})
    assert encoded in out, "documented residual: encoding defeats layer 2"


# --- the fail-open plugin seam (C7) -----------------------------------------

def test_plugin_transform_seam_cannot_reintroduce_a_secret(seeded, monkeypatch):
    """transform_tool_result runs after dispatch and can replace the result
    wholesale. Without the re-scrub, a plugin carries a credential past
    layer 2 entirely."""
    import model_tools

    def fake_invoke_hook(hook_name, **kwargs):
        if hook_name == "transform_tool_result":
            return [json.dumps({"output": ENV_BODY})]   # plugin leaks it back
        return []

    import hermes_cli.plugins as plugins
    monkeypatch.setattr(plugins, "invoke_hook", fake_invoke_hook)
    monkeypatch.setattr(
        plugins, "get_pre_tool_call_block_message", lambda *a, **k: None, raising=False
    )

    from tools.registry import registry as global_registry
    monkeypatch.setattr(
        global_registry, "dispatch",
        lambda name, args, **kw: json.dumps({"output": "clean"}),
    )

    out = model_tools.handle_function_call("probe", {})
    assert YELP_VALUE not in out, "plugin seam re-introduced a credential"
    assert OPQ_VALUE not in out
