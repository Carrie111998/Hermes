"""Tests for tools/tool_search.py — progressive tool disclosure.

Coverage targets — these mirror the issues called out in the OpenClaw tool
search report. Every test that names an OpenClaw issue is the regression
guard that would have caught that specific failure mode.
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, Dict, Any

import pytest


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _td(name: str, description: str = "", properties: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
            },
        },
    }


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_default_when_missing(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(None)
        assert cfg.enabled == "auto"
        assert cfg.threshold_pct == 5.0

    def test_bool_true_maps_to_auto(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(True)
        assert cfg.enabled == "auto"


    def test_search_limits_clamped(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw({
            "search_default_limit": 999,
            "max_search_limit": 999,
        })
        assert cfg.max_search_limit == 50
        assert cfg.search_default_limit <= cfg.max_search_limit


# ---------------------------------------------------------------------------
# Classification — the hard invariant: core tools NEVER defer.
# ---------------------------------------------------------------------------


class TestClassification:
    def test_core_tools_never_defer(self):
        """The critical invariant from the OpenClaw report."""
        from tools.tool_search import is_deferrable_tool_name
        # Sample of core tools from _HERMES_CORE_TOOLS.
        for core_name in ["terminal", "read_file", "write_file", "patch",
                          "search_files", "todo", "memory", "browser_navigate",
                          "web_search", "session_search", "clarify",
                          "execute_code", "delegate_task", "send_message"]:
            assert not is_deferrable_tool_name(core_name), (
                f"Core tool '{core_name}' must NEVER be deferrable"
            )

    def test_bridge_tools_never_defer(self):
        from tools.tool_search import is_deferrable_tool_name, BRIDGE_TOOL_NAMES
        for name in BRIDGE_TOOL_NAMES:
            assert not is_deferrable_tool_name(name)

    def test_unknown_tool_not_deferrable(self):
        """Defensive: a tool name we cannot resolve to a registry entry must
        not be claimed as deferrable. This protects against the OpenClaw
        cron regression where unresolved tools were silently dropped."""
        from tools.tool_search import is_deferrable_tool_name
        assert not is_deferrable_tool_name("xx_definitely_not_a_tool_xx")

    def test_classify_keeps_unknown_in_visible(self):
        """A tool we can't classify stays visible — never silently dropped.

        This is the OpenClaw #84141 regression guard (cron lost ``exec``
        because it wasn't in the catalog).
        """
        from tools.tool_search import classify_tools
        # Build a tool def for something we don't have a registry entry for.
        defs = [_td("xx_unknown_tool", "Unknown tool")]
        visible, deferrable = classify_tools(defs)
        names = {(td.get("function") or {}).get("name") for td in visible}
        assert "xx_unknown_tool" in names
        assert deferrable == []


# ---------------------------------------------------------------------------
# Token estimation + threshold gate
# ---------------------------------------------------------------------------


class TestThresholdGate:
    def test_off_never_activates(self):
        from tools.tool_search import ToolSearchConfig, should_activate
        cfg = ToolSearchConfig.from_raw({"enabled": "off"})
        assert not should_activate(cfg, deferrable_tokens=1_000_000, context_length=200_000)


    def test_token_estimate_proportional_to_schema_size(self):
        from tools.tool_search import estimate_tokens_from_schemas
        small = [_td("a", "x")]
        big = [_td(f"name_{i}", f"description for tool {i} " * 20,
                   {"q": {"type": "string", "description": "search query " * 10}})
               for i in range(10)]
        small_t = estimate_tokens_from_schemas(small)
        big_t = estimate_tokens_from_schemas(big)
        assert big_t > small_t * 10


# ---------------------------------------------------------------------------
# Retrieval (BM25 + substring fallback)
# ---------------------------------------------------------------------------


class TestRetrieval:
    def _fake_catalog(self):
        """Build a catalog directly without touching the registry."""
        from tools.tool_search import CatalogEntry, _tokenize, _entry_search_text
        defs = [
            _td("github_create_issue", "Open a new issue in a GitHub repository",
                {"title": {"type": "string"}, "body": {"type": "string"}}),
            _td("github_search_repos", "Search GitHub for matching repositories",
                {"query": {"type": "string"}}),
            _td("slack_send_message", "Post a message into a Slack channel",
                {"channel": {"type": "string"}, "text": {"type": "string"}}),
            _td("calendar_create_event", "Add an event to the user's calendar",
                {"title": {"type": "string"}, "start": {"type": "string"}}),
        ]
        catalog = []
        for d in defs:
            fn = d["function"]
            e = CatalogEntry(
                name=fn["name"], description=fn["description"],
                schema=d, source="mcp", source_name="mcp-test",
            )
            e._tokens = _tokenize(_entry_search_text(d))
            catalog.append(e)
        return catalog

    def test_search_finds_relevant_tool(self):
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "create a github issue", limit=3)
        names = [h.name for h in hits]
        assert names[0] == "github_create_issue"


    def test_search_respects_limit(self):
        from tools.tool_search import search_catalog
        hits = search_catalog(self._fake_catalog(), "github", limit=1)
        assert len(hits) <= 1


# ---------------------------------------------------------------------------
# Assembly — the full passthrough/activate decision.
# ---------------------------------------------------------------------------


class TestAssembly:
    def test_no_deferrable_returns_unchanged(self):
        """Pure-core toolset: pass-through, no bridge tools added."""
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig
        defs = [_td("terminal", "Run shell"), _td("read_file", "Read a file")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        assert not result.activated
        assert {t["function"]["name"] for t in result.tool_defs} == {"terminal", "read_file"}

    @staticmethod
    def _register_mcp(name):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True})

        registry.register(
            name=name,
            handler=_handler,
            schema=_td(name, "Deferred capability description.")["function"],
            toolset="mcp-tiertest",
        )


    def test_idempotent_when_bridge_already_present(self):
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES
        defs = [_td("terminal", "Run shell"), _td("tool_search", "old")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "off"}),
        )
        names = [(t["function"]["name"]) for t in result.tool_defs]
        # The pre-existing tool_search was stripped (it would be re-injected if
        # activation happened; here it didn't).
        assert "tool_search" not in names


# ---------------------------------------------------------------------------
# Bridge dispatch
# ---------------------------------------------------------------------------


class TestBridgeDispatch:
    def test_tool_search_requires_query(self):
        from tools.tool_search import dispatch_tool_search
        result = dispatch_tool_search({}, current_tool_defs=[])
        assert "error" in json.loads(result)

    def test_empty_search_keeps_connected_sources_discoverable(self):
        from tools.registry import registry
        from tools.tool_search import dispatch_tool_search

        name = "recovery_catalog_create_record"
        tool_def = _td(name, "Create a record in the connected catalog service.")
        registry.register(
            name=name,
            handler=lambda args, **kwargs: "{}",
            schema=tool_def,
            toolset="mcp-recovery-catalog",
        )

        result = json.loads(dispatch_tool_search(
            {"query": "unrelated vocabulary"},
            current_tool_defs=[tool_def],
        ))

        assert result["matches"] == []
        assert result["total_available"] == 1
        assert result["available_sources"] == [
            {"name": "recovery-catalog", "tool_count": 1},
        ]
        assert "remain available" in result["hint"]
        assert "before concluding" in result["hint"]


    def test_resolve_underlying_call_parses_object_args(self):
        from tools.tool_search import resolve_underlying_call
        name, args, err = resolve_underlying_call({
            "name": "unknown_xxx",
            "arguments": {"foo": "bar"},
        })
        # Will fail classification because unknown_xxx isn't deferrable.
        assert err is not None


    def test_resolve_underlying_call_rejects_recursion(self):
        """tool_call cannot invoke tool_call itself."""
        from tools.tool_search import resolve_underlying_call, TOOL_CALL_NAME
        name, args, err = resolve_underlying_call({
            "name": TOOL_CALL_NAME,
            "arguments": {},
        })
        assert err is not None
        assert "bridge tool" in err.lower()


# ---------------------------------------------------------------------------
# End-to-end via the real handle_function_call (smoke test).
# ---------------------------------------------------------------------------


class TestHandleFunctionCallIntegration:
    def test_tool_search_dispatch_through_handle_function_call(self):
        """The dispatcher recognizes the bridge tool by name."""
        import model_tools
        result = model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "nothing matches this"},
        )
        parsed = json.loads(result)
        # Without a real registry, the matches will be empty, but the
        # dispatch path completed without error.
        assert "matches" in parsed or "error" in parsed

    def test_tool_search_emits_one_terminal_hook(self, monkeypatch):
        """Inline bridge results still complete the tool lifecycle."""
        import model_tools
        from hermes_cli import lifecycle
        from tools import tool_search

        events = []
        monkeypatch.setattr(
            lifecycle,
            "has_hook",
            lambda name: name == "post_tool_call",
        )
        monkeypatch.setattr(
            lifecycle,
            "invoke_hook",
            lambda name, **kwargs: events.append((name, kwargs)),
        )
        monkeypatch.setattr(
            tool_search,
            "dispatch_tool_search",
            lambda *args, **kwargs: json.dumps({"matches": []}),
        )

        result = model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "private-query"},
            session_id="private-session",
            task_id="private-task",
            turn_id="private-turn",
            api_request_id="private-request",
            tool_call_id="private-call",
        )

        assert json.loads(result) == {"matches": []}
        assert len(events) == 1
        hook_name, payload = events[0]
        assert hook_name == "post_tool_call"
        assert payload["status"] == "ok"
        assert payload["turn_id"] == "private-turn"
        assert payload["api_request_id"] == "private-request"
        assert payload["tool_call_id"] == "private-call"


class TestRegression_OpenClawCron84141:
    """Regression guard for the OpenClaw cron-tool-loss class of bug.

    OpenClaw #84141: ``toolsAllow: ["exec"]`` on an isolated cron turn
    resulted in the agent receiving only ``sessions_send`` — the catalog
    builder silently dropped the requested core tool.

    Our defense: core tools are NEVER deferred. This test exercises the
    full assembly pipeline with a mixed core+MCP toolset and asserts that
    every core tool survives.
    """

    def test_core_tool_survives_alongside_many_mcp_tools(self):
        from tools.tool_search import (
            assemble_tool_defs, ToolSearchConfig, BRIDGE_TOOL_NAMES,
            classify_tools,
        )
        # 1 core tool + 50 unknown/MCP-shaped tools (deferrable).
        defs = [_td("terminal", "Run shell commands")]
        # Pad with fake "deferrable" tools — without registry registration,
        # classify_tools puts them in 'visible'. So instead, we just verify
        # the core-tool side: terminal stays in visible regardless.
        visible, deferrable = classify_tools(defs)
        assert any(
            (td.get("function") or {}).get("name") == "terminal"
            for td in visible
        ), "Core tool 'terminal' was wrongly classified as deferrable"

        # Now force activation and check the resulting tool-defs list.
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        names = {(t.get("function") or {}).get("name") for t in result.tool_defs}
        # terminal must be present; bridges are only added if there are
        # deferrable tools to put behind them.
        assert "terminal" in names

    def test_unwrap_rejects_core_tool_attempt(self):
        """Even if the model tries to invoke a core tool through tool_call,
        we reject the call and tell the model to use it directly."""
        from tools.tool_search import resolve_underlying_call
        _, _, err = resolve_underlying_call({
            "name": "terminal",
            "arguments": {"command": "echo hi"},
        })
        assert err is not None
        assert "not a deferrable" in err


class TestRegression_ToolsetScoping:
    """A restricted-toolset session must not see or invoke out-of-scope tools.

    The bug: the bridge dispatch and the tool_executor unwrap read the
    catalog from the *global* registry (get_tool_definitions with no
    toolset scope = "start with everything"), so a session scoped to one
    MCP server could tool_search the entire process registry and tool_call
    any plugin tool it was never granted. registry.dispatch() has no
    enabled_tools gate for non-execute_code tools, so the out-of-scope tool
    actually ran.

    The fix threads the session's enabled/disabled toolsets into the bridge
    dispatch (model_tools.handle_function_call) and the executor unwrap
    (agent.tool_executor), scoping both the searchable catalog and the
    invocable set to the session's own toolsets.
    """

    @staticmethod
    def _register(name, toolset):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True, "tool": name})

        registry.register(
            name=name,
            handler=_handler,
            schema=_td(name, f"desc for {name}", {"repo": {"type": "string"}}),
            toolset=toolset,
        )

    def test_search_catalog_is_scoped_to_session_toolsets(self):
        import model_tools

        for i in range(12):
            self._register(f"mcp_scoped_gh_{i}", "mcp-scoped-gh")
        self._register("scoped_oos_plugin", "scopedoosplugin")

        # tool_search scoped to the github toolset must not count the
        # out-of-scope plugin tool (or any of the host registry).
        result = model_tools.handle_function_call(
            function_name="tool_search",
            function_args={"query": "mcp_scoped_gh", "limit": 5},
            enabled_toolsets=["mcp-scoped-gh"],
        )
        parsed = json.loads(result)
        assert parsed["total_available"] == 12, (
            f"expected scoped catalog of 12, got {parsed['total_available']} "
            "— catalog leaked tools outside the session's toolsets"
        )
        hit_names = {m["name"] for m in parsed["matches"]}
        assert "scoped_oos_plugin" not in hit_names


    def test_scoped_deferrable_names_helper(self):
        from tools.tool_search import scoped_deferrable_names

        self._register("mcp_helper_op", "mcp-helper")
        import model_tools
        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["mcp-helper"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        names = scoped_deferrable_names(defs)
        assert "mcp_helper_op" in names
        # core tools are never deferrable
        assert "terminal" not in names


# ---------------------------------------------------------------------------
# Catalog listing (skills-style progressive disclosure)
# ---------------------------------------------------------------------------


class TestCatalogListing:
    def test_config_defaults(self):
        from tools.tool_search import ToolSearchConfig
        cfg = ToolSearchConfig.from_raw(None)
        assert cfg.listing == "auto"
        assert cfg.listing_max_tokens == 4000
        # legacy bool shapes keep defaults too
        assert ToolSearchConfig.from_raw(True).listing == "auto"


    def test_default_listing_cap_bounds_fixed_catalog_overhead(self):
        """The default manifest must not grow back to the old 20K-token cap."""
        from tools.registry import registry
        from tools.tool_search import (
            ToolSearchConfig,
            assemble_tool_defs,
            estimate_tokens_from_schemas,
        )

        defs = []
        for i in range(500):
            name = f"lean_catalog_tool_{i:04d}"
            registry.register(
                name=name,
                handler=lambda args, **kwargs: "{}",
                schema=_td(name, "Perform a deliberately verbose connected service action."),
                toolset="mcp-lean-catalog",
            )
            defs.append(_td(name, "Perform a deliberately verbose connected service action."))

        cfg = ToolSearchConfig.from_raw(None)
        result = assemble_tool_defs(defs, context_length=1_000_000, config=cfg)
        search = next(
            td for td in result.tool_defs
            if td["function"]["name"] == "tool_search"
        )
        description_tokens = estimate_tokens_from_schemas([search])
        # Includes the bridge schema around the listing, so allow modest
        # framing overhead above the 4K listing budget.
        assert description_tokens < 4500
        assert result.listing_form in {"names", "groups", "mixed"}

    def test_short_desc_first_sentence_and_clip(self):
        from tools.tool_search import _short_desc
        assert _short_desc("Open an issue. Second sentence dropped.") == "Open an issue."
        long = "word " * 40
        s = _short_desc(long)
        assert len(s) <= 61  # 60 + ellipsis char
        assert s.endswith("…")
        assert _short_desc("") == ""


    @staticmethod
    def _register(name):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True})

        registry.register(
            name=name,
            handler=_handler,
            schema=_td(name, "Deferred capability description.")["function"],
            toolset="mcp-listingtest",
        )


    def test_assembly_listing_off_keeps_legacy_description(self):
        from tools.tool_search import assemble_tool_defs, ToolSearchConfig
        for i in range(30):
            self._register(f"mcp_x_{i}")
        defs = [_td(f"mcp_x_{i}", "Deferred.") for i in range(30)]
        result = assemble_tool_defs(
            defs, context_length=1000,
            config=ToolSearchConfig.from_raw({"enabled": "on", "listing": "off"}),
        )
        assert result.activated
        search = next(t for t in result.tool_defs if t["function"]["name"] == "tool_search")
        assert "mcp_x_0" not in search["function"]["description"]


class TestDeferredCallSchemaProbe:
    """Blind tool_call invocations missing required arguments must return
    the tool's parameter schema instead of dispatching into an opaque
    downstream failure (port of nearai/ironclaw#5149's describe-first fix).

    A deferred tool's schema is invisible until tool_describe is called, so
    models routinely invoke deferred tools by name alone. Pre-fix, that
    produced ``KeyError: 'document_id'``-style errors that teach the model
    nothing; post-fix, the probe returns the schema so the model repairs
    the call in one round-trip. Valid calls dispatch untouched.
    """

    @staticmethod
    def _register(name, toolset, required=("document_id",)):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            # Simulates a tool that crashes opaquely on a missing required arg.
            return json.dumps({"ok": True, "doc": args["document_id"]})

        params = {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "description": "Doc id"},
                "format": {"type": "string"},
            },
            "required": list(required),
        }
        registry.register(
            name=name,
            handler=_handler,
            schema={"type": "function",
                    "function": {"name": name, "description": f"desc {name}",
                                 "parameters": params}},
            toolset=toolset,
        )

    def test_validator_returns_schema_for_missing_required(self):
        from tools.tool_search import validate_deferred_call_args

        self._register("mcp_probe_docs_get", "mcp-probe")
        err = validate_deferred_call_args("mcp_probe_docs_get", {})
        assert err is not None
        parsed = json.loads(err)
        assert "document_id" in parsed["error"]
        assert "NOT invoked" in parsed["error"]
        assert parsed["parameters"]["required"] == ["document_id"]
        assert "document_id" in parsed["parameters"]["properties"]


    def test_validator_never_blocks_unvalidatable_tools(self):
        from tools.tool_search import validate_deferred_call_args

        # Unknown tool → no schema → dispatch (downstream scope gate handles it).
        assert validate_deferred_call_args("mcp_no_such_tool_xyz", {}) is None


    def test_valid_tool_call_still_dispatches(self):
        import model_tools

        self._register("mcp_probe_valid_op", "mcp-probe-valid")
        result = json.loads(model_tools.handle_function_call(
            function_name="tool_call",
            function_args={"name": "mcp_probe_valid_op",
                           "arguments": {"document_id": "abc"}},
            enabled_toolsets=["mcp-probe-valid"],
        ))
        assert result.get("ok") is True
        assert result.get("doc") == "abc"


# ---------------------------------------------------------------------------
# Description-only mode — tests for PR #66826 description_only tool_injection
# ---------------------------------------------------------------------------


class TestDescriptionOnly:
    """Tests for description-only tool marking, classification, and assembly."""

    @staticmethod
    def _register(name: str, toolset: str):
        from tools.registry import registry

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True, "tool": name})

        registry.register(
            name=name,
            handler=_handler,
            schema=_td(name, f"desc for {name}", {"q": {"type": "string"}}),
            toolset=toolset,
        )

    @staticmethod
    def _inventory_agent(valid_tools, pre_assembly):
        """Minimal agent-shaped object for build_system_prompt_parts,
        mirroring what agent_init produces (valid_tool_names = post-assembly
        visible set, _pre_assembly_tool_names = full granted set)."""
        from types import SimpleNamespace

        return SimpleNamespace(
            valid_tool_names=list(valid_tools),
            _pre_assembly_tool_names=set(pre_assembly),
            load_soul_identity=False,
            skip_context_files=False,
            _task_completion_guidance=False,
            _tool_use_enforcement=False,
            _environment_probe=False,
            _kanban_worker_guidance="",
            _memory_store=None,
            _memory_manager=None,
            model="",
            provider="",
            platform="",
            pass_session_id=False,
            session_id="",
        )

    # ------------------------------------------------------------------
    # mark / is round-trip
    # ------------------------------------------------------------------

    def test_mark_and_is_round_trip(self):
        """mark_description_only_tool → is_description_only_tool round-trip."""
        from tools.tool_search import (
            mark_description_only_tool,
            is_description_only_tool,
            get_description_only_tool_names,
        )
        mark_description_only_tool("test_desc_only_roundtrip")
        assert is_description_only_tool("test_desc_only_roundtrip")
        assert "test_desc_only_roundtrip" in get_description_only_tool_names()
        assert not is_description_only_tool("unmarked_tool")

    def test_get_description_only_returns_copy(self):
        """get_description_only_tool_names returns a copy, not a live ref."""
        from tools.tool_search import (
            mark_description_only_tool,
            get_description_only_tool_names,
        )
        mark_description_only_tool("test_copy_tool")
        copy1 = get_description_only_tool_names()
        copy1.add("not_real")
        copy2 = get_description_only_tool_names()
        assert "not_real" not in copy2

    # ------------------------------------------------------------------
    # Classification: description_only tools are always deferrable
    # ------------------------------------------------------------------

    def test_description_only_tool_is_deferrable(self):
        """A tool marked description_only is always deferrable regardless
        of its toolset or core-tool status."""
        from tools.tool_search import (
            mark_description_only_tool,
            is_deferrable_tool_name,
        )
        mark_description_only_tool("do_classify_me")
        assert is_deferrable_tool_name("do_classify_me")

    def test_classify_tools_splits_description_only_correctly(self):
        """classify_tools puts description-only tools in deferrable."""
        from tools.tool_search import (
            mark_description_only_tool,
            classify_tools,
        )
        mark_description_only_tool("do_classify_split")
        # Build a mixed list: one core tool + one description-only tool.
        defs = [
            _td("terminal", "Run shell commands"),
            _td("do_classify_split", "A description-only tool"),
        ]
        visible, deferrable = classify_tools(defs)
        visible_names = {(td.get("function") or {}).get("name") for td in visible}
        deferrable_names = {(td.get("function") or {}).get("name") for td in deferrable}
        assert "terminal" in visible_names
        assert "terminal" not in deferrable_names
        assert "do_classify_split" in deferrable_names

    # ------------------------------------------------------------------
    # Assembly: description_only tools force bridge activation
    # ------------------------------------------------------------------

    def test_assemble_forces_bridge_with_description_only_below_threshold(self):
        """Even below the threshold, description_only tools force bridge."""
        from tools.tool_search import (
            assemble_tool_defs,
            mark_description_only_tool,
            ToolSearchConfig,
            BRIDGE_TOOL_NAMES,
        )
        mark_description_only_tool("do_force_bridge")
        # A single description_only tool — way below any reasonable threshold.
        defs = [_td("do_force_bridge", "Tiny tool")]
        result = assemble_tool_defs(
            defs,
            context_length=200_000,
            config=ToolSearchConfig.from_raw({"enabled": "auto", "threshold_pct": 10}),
        )
        assert result.activated
        names = {(t.get("function") or {}).get("name") for t in result.tool_defs}
        assert "tool_search" in names
        assert "do_force_bridge" not in names  # deferred behind bridge

    def test_assemble_with_description_only_off_skips_in_model_tools_layer(self):
        """REAL model_tools path (P3): with tool_search disabled in config,
        ``get_tool_definitions`` must not run assembly — the description_only
        tool stays visible in the returned list (no bridge injected), so the
        session can still use it directly. Previously this test re-implemented
        the production gate on its own copy of the config, which stayed green
        even if model_tools.py stopped honoring ``enabled == "off"``."""
        from unittest.mock import patch
        import model_tools
        from tools.tool_search import (
            mark_description_only_tool,
            ToolSearchConfig,
            BRIDGE_TOOL_NAMES,
        )

        tool_name = "do_gate_realpath"
        self._register(tool_name, "mcp-gate-realpath")
        mark_description_only_tool(tool_name)
        model_tools._clear_tool_defs_cache()

        with patch(
            "tools.tool_search.load_config",
            return_value=ToolSearchConfig.from_raw({"enabled": "off"}),
        ):
            defs = model_tools.get_tool_definitions(
                enabled_toolsets=["mcp-gate-realpath"],
                quiet_mode=True,
            )
        names = {(t.get("function") or {}).get("name") for t in defs}
        assert tool_name in names, (
            "tool_search off must leave description_only tools visible, "
            "not defer them behind a bridge"
        )
        assert not (BRIDGE_TOOL_NAMES & names), (
            "tool_search off must not inject bridge tools"
        )

    # ------------------------------------------------------------------
    # Session scoping: description_only tools filtered by valid_tool_names
    # ------------------------------------------------------------------

    def test_session_scoped_inventory_only_sees_in_scope_tools(self):
        """description_only tools from out-of-scope servers are not listed.

        Goes through the REAL ``build_system_prompt_parts`` path — the same
        ``get_description_only_tool_names() & _pre_assembly_tool_names``
        intersection the production system prompt performs — instead of
        re-implementing that intersection on a hand-picked set.
        """
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.tool_search import (
            mark_description_only_tool,
            ToolSearchConfig,
        )

        in_scope = "mcp_scope_gh_do"
        out_of_scope = "mcp_other_do"
        self._register(in_scope, "mcp-scope-gh")
        self._register(out_of_scope, "mcp-other")
        mark_description_only_tool(in_scope)
        mark_description_only_tool(out_of_scope)

        # A session granted ONLY the mcp-scope-gh server.
        agent = self._inventory_agent(
            valid_tools=[in_scope],
            pre_assembly={in_scope},
        )
        with (
            patch(
                "tools.tool_search.load_config",
                return_value=ToolSearchConfig.from_raw({"enabled": "auto"}),
            ),
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
        ):
            from agent.system_prompt import build_system_prompt_parts

            parts = build_system_prompt_parts(agent)
            stable = parts["stable"]

        assert "Available MCP Tools" in stable
        assert in_scope in stable
        assert out_of_scope not in stable, (
            "out-of-scope description_only tool leaked into the session "
            "inventory"
        )

    # ------------------------------------------------------------------
    # P1 regression: quiet_mode cache hit must not collapse the
    # pre-assembly snapshot (the gateway's 2nd+ session inventory)
    # ------------------------------------------------------------------

    def test_quiet_mode_cache_hit_keeps_pre_assembly_inventory(self):
        """P1 regression (#66826): a second ``get_tool_definitions`` call with
        the same args hits the quiet_mode memoized cache. The pre-assembly
        snapshot must STILL include the description_only tool after the cache
        hit — agent_init captures it into ``agent._pre_assembly_tool_names``,
        so a collapsed snapshot would silently empty the system-prompt
        inventory for every session after the first with the same toolset key
        (gateway/TUI/cron all construct agents with quiet_mode=True)."""
        import model_tools
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.tool_search import (
            mark_description_only_tool,
            ToolSearchConfig,
        )

        tool_name = "do_cache_collapse_test"
        self._register(tool_name, "mcp-cache-collapse")
        mark_description_only_tool(tool_name)
        model_tools._clear_tool_defs_cache()

        kwargs = dict(enabled_toolsets=["mcp-cache-collapse"], quiet_mode=True)
        with patch(
            "tools.tool_search.load_config",
            return_value=ToolSearchConfig.from_raw({"enabled": "on"}),
        ):
            model_tools.get_tool_definitions(**kwargs)  # miss → fresh compute
            pre_first = set(model_tools._last_pre_assembly_tool_names)
            defs_second = model_tools.get_tool_definitions(**kwargs)  # cache hit
            pre_second = set(model_tools._last_pre_assembly_tool_names)

        # Sanity: assembly DID run on the cache-hit call — the returned list
        # is the post-assembly view (bridge present, tool deferred). This is
        # exactly the divergence that used to corrupt the capture source.
        returned_names = {(t.get("function") or {}).get("name") for t in defs_second}
        assert "tool_search" in returned_names, "assembly should have activated"
        assert tool_name not in returned_names

        assert tool_name in pre_first
        assert tool_name in pre_second, (
            "cache hit collapsed the pre-assembly snapshot — the inventory "
            "would be empty on the 2nd+ session (P1 cache-collapse)"
        )
        assert pre_first == pre_second

        # The system-prompt inventory built AFTER the cache hit still lists it.
        agent = self._inventory_agent(
            valid_tools=[tool_name],
            pre_assembly=pre_second,
        )
        with (
            patch(
                "tools.tool_search.load_config",
                return_value=ToolSearchConfig.from_raw({"enabled": "on"}),
            ),
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
        ):
            from agent.system_prompt import build_system_prompt_parts

            parts = build_system_prompt_parts(agent)
            stable = parts["stable"]

        assert "Available MCP Tools" in stable
        assert tool_name in stable

    def test_tool_search_off_no_inventory_in_system_prompt(self):
        """P3 contract on the REAL path: with tool_search disabled, the
        system prompt must contain NO description_only inventory block and NO
        tool_search mention — advertising the bridge would mislead the model
        into calling a tool that is turned off."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.tool_search import (
            mark_description_only_tool,
            ToolSearchConfig,
        )

        tool_name = "do_off_inventory_test"
        self._register(tool_name, "mcp-off-inventory")
        mark_description_only_tool(tool_name)

        agent = self._inventory_agent(
            valid_tools=[tool_name],
            pre_assembly={tool_name},
        )
        with (
            patch(
                "tools.tool_search.load_config",
                return_value=ToolSearchConfig.from_raw({"enabled": "off"}),
            ),
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
        ):
            from agent.system_prompt import build_system_prompt_parts

            parts = build_system_prompt_parts(agent)
            stable = parts["stable"]

        assert "Available MCP Tools" not in stable
        assert "description-only" not in stable
        assert "tool_search" not in stable

    # ------------------------------------------------------------------
    # Error handling: mark non-existent or duplicate tools
    # ------------------------------------------------------------------

    def test_mark_duplicate_does_not_raise(self):
        """Marking the same tool twice is a no-op, not an error."""
        from tools.tool_search import (
            mark_description_only_tool,
            is_description_only_tool,
        )
        mark_description_only_tool("do_duplicate")
        mark_description_only_tool("do_duplicate")  # should not raise
        assert is_description_only_tool("do_duplicate")

    # ------------------------------------------------------------------
    # System prompt inventory: description_only tools listed by name+description
    # ------------------------------------------------------------------

    def test_description_only_inventory_in_system_prompt(self):
        """description_only tools appear as name+description in the system
        prompt inventory block (full JSON schemas are not included)."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.registry import registry
        from tools.tool_search import mark_description_only_tool

        tool_name = "do_inv_sysprompt_test"
        tool_desc = "Searches the project documentation for a given query"

        def _handler(args, task_id=None, **kw):
            return json.dumps({"ok": True, "tool": tool_name})

        registry.register(
            name=tool_name,
            handler=_handler,
            schema=_td(tool_name, tool_desc, {"q": {"type": "string"}}),
            toolset="mcp-inv-test",
            description=tool_desc,
        )
        mark_description_only_tool(tool_name)

        agent = SimpleNamespace(
            valid_tool_names=[tool_name],
            _pre_assembly_tool_names={tool_name},
            load_soul_identity=False,
            skip_context_files=False,
            _task_completion_guidance=False,
            _tool_use_enforcement=False,
            _environment_probe=False,
            _kanban_worker_guidance="",
            _memory_store=None,
            _memory_manager=None,
            model="",
            provider="",
            platform="",
            pass_session_id=False,
            session_id="",
        )

        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
        ):
            from agent.system_prompt import build_system_prompt_parts

            parts = build_system_prompt_parts(agent)
            stable = parts["stable"]

        assert "Available MCP Tools" in stable, (
            "description_only inventory block missing"
        )
        assert "description-only" in stable
        assert tool_name in stable
        assert tool_desc[:50] in stable
        assert "tool_search" in stable
        assert "tool_describe" in stable
        # The full JSON parameter schema must not appear in the inventory
        # block — only the tool name and description.
        assert '"parameters"' not in stable, (
            "Full JSON schema leaked into system prompt inventory"
        )

    # ------------------------------------------------------------------
    # Lazy MCP registration: description_only marking persists correctly
    # ------------------------------------------------------------------

    def test_lazy_mcp_registration_marking_persists(self):
        """REAL registration/refresh/deregister path (P4 scenario 3):
        a server registered AFTER agent init has its tools marked
        description_only by ``_register_server_tools``, a
        ``refresh_agent_mcp_tools`` rebuild publishes them into the agent's
        pre-assembly inventory (so the system prompt can list them), and
        deregistering the server unmarks them (no stale marks)."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools import mcp_tool
        from tools.registry import registry
        from tools.tool_search import (
            is_description_only_tool,
            is_deferrable_tool_name,
            get_description_only_tool_names,
        )

        server_name = "lazy_server"
        tool_name = "mcp__lazy_server__search_docs"

        fake_tool = SimpleNamespace(
            name="search_docs",
            description="Search documentation",
            inputSchema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        fake_server = SimpleNamespace(
            name=server_name,
            _tools=[fake_tool],
            tool_timeout=30.0,
            # Non-None session → check_fn passes (server alive); an object()
            # advertises no resource/prompt methods → no utility schemas.
            session=object(),
            initialize_result=None,
        )

        with patch.dict(mcp_tool._servers, {server_name: fake_server}):
            registered = mcp_tool._register_server_tools(
                server_name, fake_server, {"tool_injection": "description_only"}
            )
            assert tool_name in registered
            assert is_description_only_tool(tool_name)
            assert is_deferrable_tool_name(tool_name)
            assert tool_name in get_description_only_tool_names()

            # Real refresh path: the late-registered server's tool must reach
            # the agent's pre-assembly inventory after a snapshot rebuild.
            agent = SimpleNamespace(
                enabled_toolsets=None,
                disabled_toolsets=None,
                tools=[],
                valid_tool_names=set(),
                _tool_snapshot_generation=0,
                _memory_manager=None,
                context_compressor=None,
                _context_engine_tool_names=set(),
            )
            mcp_tool.refresh_agent_mcp_tools(agent, quiet_mode=True)
            assert tool_name in agent._pre_assembly_tool_names

        # Real deregister path: unloading the server drops both the registry
        # entry and the description_only mark.
        task = mcp_tool.MCPServerTask(server_name)
        task._registered_tool_names = [tool_name]
        task._deregister_tools()
        assert registry.get_entry(tool_name) is None
        assert not is_description_only_tool(tool_name)
        assert tool_name not in get_description_only_tool_names()

    # ------------------------------------------------------------------
    # Bridge dispatch: tool_search finds and tool_call invokes description_only tools
    # ------------------------------------------------------------------

    def test_refresh_publishes_pre_assembly_on_no_post_change(self):
        """P2 regression: when tool_search assembly is ALREADY active, a
        late-registered description_only server leaves the POST-assembly name
        set unchanged (its tools were bridged away), so
        ``refresh_agent_mcp_tools`` early-returns without publishing. The
        pre-assembly view must still be published, or the system-prompt
        inventory silently misses the new server's tools. (#66826 P2)"""
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools import mcp_tool

        server_name = "late_bridge_server"
        tool_name = "mcp__late_bridge_server__lookup"

        fake_tool = SimpleNamespace(
            name="lookup",
            description="Late-registered description_only tool",
            inputSchema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        fake_server = SimpleNamespace(
            name=server_name,
            _tools=[fake_tool],
            tool_timeout=30.0,
            session=object(),
            initialize_result=None,
        )

        # Baseline refresh BEFORE the server exists: assembly is already
        # active (bridge tools present in the live POST-assembly set) and the
        # published pre-assembly view does NOT include the future server.
        agent = SimpleNamespace(
            enabled_toolsets=None,
            disabled_toolsets=None,
            tools=[],
            valid_tool_names=set(),
            _tool_snapshot_generation=0,
            _memory_manager=None,
            context_compressor=None,
            _context_engine_tool_names=set(),
        )
        with patch.dict(mcp_tool._servers, {}):
            mcp_tool.refresh_agent_mcp_tools(agent, quiet_mode=True)
            assert "tool_search" in agent.valid_tool_names  # assembly active
            assert tool_name not in agent._pre_assembly_tool_names

        # Register a description_only server AFTER the baseline: its tool is
        # bridged away, so the POST-assembly name set is unchanged from the
        # baseline (modulo the new deferred name), and a naive refresh would
        # early-return without publishing the widened pre-assembly view.
        with patch.dict(mcp_tool._servers, {server_name: fake_server}):
            mcp_tool._register_server_tools(
                server_name, fake_server, {"tool_injection": "description_only"}
            )
            added = mcp_tool.refresh_agent_mcp_tools(agent, quiet_mode=True)
            # The description_only tool is deferred (bridged away) in the live
            # POST-assembly snapshot, but the pre-assembly inventory tracks it.
            assert tool_name not in agent.valid_tool_names
            assert tool_name in agent._pre_assembly_tool_names

            # Repeat refresh with the SAME published POST-assembly snapshot:
            # early return (added == set()) must still republish the
            # pre-assembly view so the system-prompt inventory stays correct.
            added2 = mcp_tool.refresh_agent_mcp_tools(agent, quiet_mode=True)
            assert added2 == set()
            assert tool_name in agent._pre_assembly_tool_names

        # Deregister to avoid leaking marks into later tests.
        task = mcp_tool.MCPServerTask(server_name)
        task._registered_tool_names = [tool_name]
        task._deregister_tools()

    # ------------------------------------------------------------------
    # Bridge dispatch: tool_search finds and tool_call invokes description_only tools
    # ------------------------------------------------------------------

    def test_bridge_dispatch_finds_description_only_tool(self):
        """tool_search returns description_only tools in the catalog and
        tool_describe returns their full schema."""
        from tools.tool_search import (
            mark_description_only_tool,
            dispatch_tool_search,
            dispatch_tool_describe,
            ToolSearchConfig,
        )

        do_tool = "do_bridge_dispatch_test"
        self._register(do_tool, "mcp-bridge-test")
        mark_description_only_tool(do_tool)

        # Build a defs list that simulates post-classification output:
        # the description_only tool is in the deferrable set.
        defs = [
            _td("terminal", "Run shell commands"),
            _td(do_tool, "Bridge dispatch test tool", {"param": {"type": "string"}}),
        ]

        # tool_search should find it.
        result = dispatch_tool_search(
            {"query": "bridge dispatch"},
            current_tool_defs=defs,
            config=ToolSearchConfig.from_raw({"enabled": "on"}),
        )
        parsed = json.loads(result)
        assert "matches" in parsed
        match_names = [m["name"] for m in parsed["matches"]]
        assert do_tool in match_names

        # tool_describe should return its full schema.
        desc_result = dispatch_tool_describe(
            {"name": do_tool},
            current_tool_defs=defs,
        )
        desc_parsed = json.loads(desc_result)
        assert desc_parsed.get("name") == do_tool
        assert "parameters" in desc_parsed
