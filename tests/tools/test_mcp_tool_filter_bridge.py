"""Regression tests for NousResearch/hermes-agent#72553.

Bug: ``mcp_servers.<name>.tools.include`` / ``tools.exclude`` filtering is
applied at tool *registration* time only. The deferred tool bridge
(``tool_search`` / ``tool_describe`` / ``tool_call``) never re-checks the
filter, so a blocked tool (e.g. ``merge_pull_request`` under a whitelist)
remains fully discoverable and callable despite the include/exclude list.

These tests pin the contract: every entry point that ships a deferred MCP
tool to the model must honor the same include/exclude policy as
``_register_server_tools``. The contract is expressed as an explicit,
independent "blocked" set -- the tests do not peek inside
``_should_register`` because we want the contract to outlive any
refactor of that helper.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

import pytest


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# Names whose only path into the model is via the deferral bridge. If the
# bug comes back, the bridge will leak at least these names (issue describes
# ``mcp__github__merge_pull_request`` as the canonical example -- we cover
# several so a regression on any single name still fails the suite).
_BLOCKED_NAMES = [
    "mcp__github__merge_pull_request",
    "mcp__github__delete_file",
    "mcp__github__fork_repository",
]

# Tools that the session is legitimately allowed to see and call.
_ALLOWED_NAMES = [
    "mcp__github__list_issues",
    "mcp__github__create_issue",
    "mcp__github__search_code",
]


def _td(name: str, description: str = "", parameters: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Build a minimal tool-definition dict in the same shape ``tool_search`` receives."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or f"Mock description for {name}",
            "parameters": parameters
            or {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
        },
    }


def _current_defs() -> List[Dict[str, Any]]:
    """Return the tool list the bridge would see in a session whose
    include filter has been applied but the named tools have *not* been
    excluded at registration time.

    The bug -- and these tests' foundation -- is that this list contains
    tools the include/exclude config said are blocked. Until the fix, the
    deferred bridge trusts this list and exposes those names anyway.
    """
    return [_td(name) for name in _BLOCKED_NAMES + _ALLOWED_NAMES]


def _register_mcp_sentinels() -> List[str]:
    """Register a sentinel for each ``_ALLOWED_NAMES`` entry so that
    :func:`is_deferrable_tool_name` returns True for them. Returns the
    sentinel name list for symmetric cleanup."""
    from tools.mcp_tool import _track_mcp_tool_server  # module-private
    from tools.registry import registry

    sentinel_names: List[str] = []
    for name in _ALLOWED_NAMES:
        sentinel = f"{name}__bridge_test_sentinel"
        registry.register(
            name=sentinel,
            toolset=f"mcp-{name.split('__')[1].rstrip('_')}",
            schema=_td(sentinel, "Sentinel tool"),
            handler=lambda args, **kw: json.dumps({"ok": True}),
            check_fn=lambda: True,
            is_async=False,
            description="Sentinel tool",
        )
        _track_mcp_tool_server(sentinel, name.split('__')[1].rstrip('_'))
        sentinel_names.append(sentinel)
    return sentinel_names


def _unregister_mcp_sentinels(sentinel_names: List[str]) -> None:
    """Tear down everything ``_register_mcp_sentinels`` put in the registry."""
    from tools.mcp_tool import _lock, _mcp_tool_server_names

    for sentinel_name in sentinel_names:
        with _lock:
            _mcp_tool_server_names.pop(sentinel_name, None)
        try:
            from tools.registry import registry
            registry.deregister(sentinel_name)
        except Exception:
            pass


def _forget_mcp_tool_server(tool_name: str) -> None:
    """Best-effort cleanup of the MCP server provenance map."""
    from tools.mcp_tool import _lock, _mcp_tool_server_names

    with _lock:
        _mcp_tool_server_names.pop(tool_name, None)


# ---------------------------------------------------------------------------
# tools.mcp_tool: source-of-truth for filter decisions
# ---------------------------------------------------------------------------


class TestMcpToolFilterContract:
    """``tools.mcp_tool`` owns the include/exclude filter. The contract
    surface for the deferred bridge is the small helper
    :func:`is_mcp_tool_filtered_in_session` -- its semantics are pinned here
    so a regression in either mcp_tool.py or tool_search.py fails one of
    these tests.

    Semantics of :func:`is_mcp_tool_filtered_in_session`:

      * ``True``  -- caller's ``tool_name`` MUST NOT be surfaced through the
        deferred bridge. Either the name is MCP-shaped but its
        ``tools.include`` / ``tools.exclude`` decision filtered it out at
        registration time, or the name is unknown to the registry (which
        is treated as 'cannot be made available through the bridge').
      * ``False`` -- caller's ``tool_name`` is a non-MCP tool OR an MCP
        tool that survived the include/exclude filter (i.e. is registered
        with an ``mcp-*`` toolset in the live registry). The bridge is
        allowed to surface it.
    """

    def test_helper_lives_in_mcp_tool(self):
        from tools import mcp_tool

        assert hasattr(mcp_tool, "is_mcp_tool_filtered_in_session"), (
            "is_mcp_tool_filtered_in_session() is the shared contract surface "
            "for the deferred bridge; without it, the filter cannot be "
            "enforced on tool_search / tool_describe / tool_call paths."
        )

    def test_helper_returns_false_for_non_mcp_tool_names(self):
        """Non-MCP tool names (core Hermes tools or non-MCP plugin tools)
        are *outside* this helper's remit. The contract is "for MCP-shaped
        names only", so non-MCP names are reported as un-filtered. The
        deferred bridge uses ``is_deferrable_tool_name`` for the wider
        decision about whether a name is bridge-able at all."""
        from tools import mcp_tool

        assert (
            mcp_tool.is_mcp_tool_filtered_in_session("terminal") is False
        )
        assert (
            mcp_tool.is_mcp_tool_filtered_in_session("read_file") is False
        )

    def test_helper_rejects_mcp_shaped_name_not_registered(self):
        """An MCP-shaped name (``mcp__``...``__) that was *not* registered
        -- because include/exclude filtered it -- must be reported as
        filtered. This is the canonical #72553 case."""
        from tools import mcp_tool

        # We rely on the absent name being MCP-shaped and un-registered.
        assert (
            mcp_tool.is_mcp_tool_filtered_in_session(
                "mcp__bridge_test_no_such_tool__never_registered"
            )
            is True
        )

    def test_helper_accepts_registered_mcp_tool(self):
        """A name whose MCP server registration tracked it as registered
        is unfiltered (the include/exclude logic already let it past)."""
        from tools.mcp_tool import _track_mcp_tool_server  # module-private
        from tools.registry import registry

        sentinel_name = "mcp__bridge_test__allowed_to_be_seen"
        try:
            registry.register(
                name=sentinel_name,
                toolset="mcp-bridge-test",
                schema=_td(sentinel_name, "Sentinel tool"),
                handler=lambda args, **kw: json.dumps({"ok": True}),
                check_fn=lambda: True,
                is_async=False,
                description="Sentinel tool",
            )
            _track_mcp_tool_server(sentinel_name, "bridge_test")

            from tools import mcp_tool

            assert mcp_tool.is_mcp_tool_filtered_in_session(sentinel_name) is False
        finally:
            _forget_mcp_tool_server(sentinel_name)
            try:
                registry.deregister(sentinel_name)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# tools.tool_search: deferral bridge must honor the same filter
# ---------------------------------------------------------------------------


class TestToolSearchBridgeRespectsFilter:
    """The three bridge entry points must refuse to surface or invoke a
    name that the include/exclude filter has blocked. The tests below
    assume ``_current_defs`` reaches the bridge with blocked names still
    present -- that is the exact situation #72553 reports -- and assert the
    bridge now refuses those names.
    """

    def test_dispatch_tool_search_hides_blocked_hits(self):
        """A search against the catalog must not return blocked names
        even when ``current_tool_defs`` still lists them."""
        from tools.tool_search import dispatch_tool_search

        result = json.loads(
            dispatch_tool_search({"query": "merge"}, current_tool_defs=_current_defs())
        )
        matches = result.get("matches", [])
        match_names = {m["name"] for m in matches}
        for blocked in _BLOCKED_NAMES:
            assert blocked not in match_names, (
                f"{blocked!r} leaked into tool_search results -- include/exclude "
                "filter is not being applied to the deferred catalog."
            )

    def test_dispatch_tool_describe_rejects_blocked_name(self):
        """``tool_describe`` must treat a blocked name as 'not available'
        even when the tool defs list still contains it."""
        from tools.tool_search import dispatch_tool_describe

        result = json.loads(
            dispatch_tool_describe(
                {"name": "mcp__github__merge_pull_request"},
                current_tool_defs=_current_defs(),
            )
        )
        # The contract: blocked name returns an error that names the
        # filter as the cause, with no schema leakage.
        assert "error" in result, (
            "dispatch_tool_describe should refuse to describe a tool that "
            "include/exclude has blocked."
        )
        assert "parameters" not in result, (
            "dispatch_tool_describe leaked the blocked tool's parameter "
            "schema -- that is a security boundary violation."
        )

    def test_dispatch_tool_describe_still_describes_allowed_tools(self):
        """The same fix must not over-reach: allowed names keep working.

        We register a sentinel tied to ``mcp__github__list_issues`` so
        that ``is_deferrable_tool_name`` returns True -- exactly the
        state of a tool that survived the include/exclude filter at
        registration time."""
        sentinel_names = _register_mcp_sentinels()
        try:
            # The dispatch sees _current_defs() but still finds the
            # allowed tool through the bridge because the sentinel is
            # registered. Use the sentinel name to match what the bridge
            # will look up.
            sentinel_name = next(n for n in sentinel_names if "list_issues" in n)
            from tools.tool_search import dispatch_tool_describe

            result = json.loads(
                dispatch_tool_describe(
                    {"name": sentinel_name},
                    current_tool_defs=_current_defs() + [_td(sentinel_name)],
                )
            )
            assert "error" not in result, result
            assert result["name"] == sentinel_name
        finally:
            _unregister_mcp_sentinels(sentinel_names)

    def test_resolve_underlying_call_rejects_blocked_name(self):
        """``tool_call`` resolves through ``resolve_underlying_call``.
        A blocked name must surface as an error string (matching the
        existing 'not deferrable' / 'not in session' error shape) and
        must NOT return a usable ``underlying_name``."""
        from tools.tool_search import resolve_underlying_call

        name, _args, err = resolve_underlying_call(
            {"name": "mcp__github__merge_pull_request", "arguments": {"x": "y"}}
        )
        assert name is None, (
            f"resolve_underlying_call returned {name!r} for a blocked tool -- "
            "the include/exclude filter is not blocking the dispatch."
        )
        assert err, "blocked-name rejection must include a human-readable reason"

    def test_scoped_deferrable_names_excludes_blocked(self):
        """A blocked MCP tool must not appear in the scoped-deferrable
        catalog used as the defense-in-depth gate in ``handle_function_call``.

        This test only fails when ``_current_defs`` itself contains the
        blocked name (the bug state); in that case the helper must not
        echo it back, because the bridge downstream would otherwise trust
        the list and call the tool.
        """
        from tools.tool_search import scoped_deferrable_names

        scoped = scoped_deferrable_names(_current_defs())
        for blocked in _BLOCKED_NAMES:
            assert blocked not in scoped, (
                f"{blocked!r} leaked into scoped_deferrable_names -- the "
                "tool_call defense-in-depth gate would accept the call."
            )


# ---------------------------------------------------------------------------
# Pin the helper to the bridge surface
# ---------------------------------------------------------------------------


class TestBridgeDelegatesToContract:
    """The deferred bridge's filter check must call the contract helper
    from ``tools.mcp_tool``. If a future refactor splits the helper away,
    one of these tests pins the path.
    """

    def test_tool_search_imports_filter_helper(self):
        """``tools.tool_search`` must reference the helper, not reimplement
        include/exclude logic in three places (which is exactly the bug)."""
        from tools import tool_search

        src = open(tool_search.__file__).read()
        # We accept either a top-level import or lazy imports inside
        # the dispatch functions -- both are valid and used elsewhere
        # in the file. The contract is that the helper *name* appears.
        assert "is_mcp_tool_filtered_in_session" in src, (
            "tools/tool_search.py must call into tools.mcp_tool's "
            "is_mcp_tool_filtered_in_session helper to enforce the "
            "include/exclude filter on the deferred bridge."
        )


# ---------------------------------------------------------------------------
# End-to-end: real ``tools.include`` config feeds through registration
# AND the deferred bridge, so a blocked name has the same observable
# outcome through every consumer path
# ---------------------------------------------------------------------------


class _StubMCPTool:
    """Minimal stand-in for an MCP server tool. Matches the duck-type
    that ``_register_server_tools`` reads: ``.name`` and ``.description``."""

    __slots__ = ("name", "description")

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description


class _StubMCPServer:
    """Minimal MCPServerTask stand-in. ``_register_server_tools`` reads
    ``server._tools`` (a list of MCP tool objects), ``server.name``,
    ``server.tool_timeout``, ``server.session`` (legacy utility-capability
    fallback), and ``server.initialize_result`` -- we shape all of them so
    the include/exclude decision runs without a live transport."""

    __slots__ = ("name", "_tools", "tool_timeout", "session", "initialize_result")

    def __init__(self, name: str, tools: list):
        self.name = name
        self._tools = list(tools)
        self.tool_timeout: float = 30.0
        # A legacy session with no utility methods -- forces the
        # capability-aware branch into "no utility" and returns [].
        self.session = type("StubSession", (), {})()
        # No initialize_result -- the legacy ``hasattr`` fallback runs and
        # registers nothing for resources/prompts (good for this test).
        self.initialize_result = None


def _cleanup_registry(server_name: str, registered_names: list) -> None:
    """Tear down the registry entries registered by ``_register_server_tools``."""
    from tools.mcp_tool import _lock, _mcp_tool_server_names
    from tools.registry import registry

    for tool_name in registered_names:
        with _lock:
            _mcp_tool_server_names.pop(tool_name, None)
        try:
            registry.deregister(tool_name)
        except Exception:
            pass
    # Alias registrations keyed by the toolset name; clear them too.
    try:
        registry._toolset_aliases.pop(f"mcp-{server_name}", None)  # type: ignore[attr-defined]
    except Exception:
        pass


class TestMCPFilterEndToEnd:
    """Drive ``_register_server_tools`` with a real config so the include
    filter is exercised through the production code path, not a mock.
    Then assert the deferred bridge refuses any tool the filter
    excluded -- the same tool that ``tool_search`` / ``tool_describe`` /
    ``tool_call`` would otherwise leak (#72553).

    These tests deliberately monkey-patch ``is_deferrable_tool_name`` to
    return True for every MCP-shaped name. The production behavior
    consults the registry directly, which would (today) already screen
    out names that were not registered. The mock simulates the
    dynamic-refresh window the issue describes: the deferred bridge's
    caller has a ``current_tool_defs`` list that still references the
    blocked name, and the existing label-alone rejection must not be
    the only line of defence.
    """

    def test_include_filters_block_bridge_lookup(self, monkeypatch):
        """Configure a whitelist that excludes ``merge_pull_request`` and
        confirm the registry, the bridge search, the bridge describe,
        and the bridge tool_call all refuse it -- *even when the
        pre-existing ``is_deferrable_tool_name`` label accepts it*."""
        from tools.mcp_tool import _register_server_tools, MCP_TOOL_NAME_PREFIX
        from tools.tool_search import (
            dispatch_tool_search,
            dispatch_tool_describe,
            resolve_underlying_call,
            scoped_deferrable_names,
        )

        # Force every MCP-shaped name to be deferrable so the helper
        # is the only line of defence. This is the situation the
        # production code is in during a brief dynamic-refresh window
        # when the bridge has a stale defs list.
        monkeypatch.setattr(
            "tools.tool_search.is_deferrable_tool_name",
            lambda name: True if name.startswith("mcp__") else False,
        )

        server = _StubMCPServer(
            "github_bridge_test",
            [
                _StubMCPTool("list_issues", "List issues"),
                _StubMCPTool("create_issue", "Create issue"),
                _StubMCPTool("merge_pull_request", "Merge a PR"),  # blocked
            ],
        )
        config = {"tools": {"include": ["list_issues", "create_issue"]}}

        registered = _register_server_tools(
            "github_bridge_test", server, config
        )
        try:
            blocked_name = (
                f"{MCP_TOOL_NAME_PREFIX}github_bridge_test__merge_pull_request"
            )
            allowed_name = (
                f"{MCP_TOOL_NAME_PREFIX}github_bridge_test__list_issues"
            )
            assert blocked_name not in registered, (
                "_register_server_tools must drop tools not in the include list."
            )
            assert allowed_name in registered, (
                "Sanity: an allowed tool should be registered."
            )

            # Feed the bridge a tool defs list that *still* contains the
            # blocked name (the buggy state from #72553). The bridge must
            # refuse it even though the defs list carries it forward.
            bridge_defs = [
                _td(blocked_name, "Merge a PR"),
                _td(allowed_name, "List issues"),
            ]

            # The new helper correctly flags the blocked name. (This
            # is true independent of the mock: _register_server_tools
            # did not call _track_mcp_tool_server for the blocked name.)
            from tools.mcp_tool import is_mcp_tool_filtered_in_session
            assert is_mcp_tool_filtered_in_session(blocked_name) is True
            assert is_mcp_tool_filtered_in_session(allowed_name) is False

            # Bridge search refuses the blocked name even when the defs
            # list carries it.
            search_result = json.loads(
                dispatch_tool_search(
                    {"query": "merge"},
                    current_tool_defs=bridge_defs,
                )
            )
            assert all(
                m["name"] != blocked_name for m in search_result.get("matches", [])
            ), "blocked MCP name leaked through tool_search"

            # Bridge describe refuses the blocked name.
            describe_result = json.loads(
                dispatch_tool_describe(
                    {"name": blocked_name},
                    current_tool_defs=bridge_defs,
                )
            )
            assert "error" in describe_result
            assert "parameters" not in describe_result

            # Bridge tool_call refuses the blocked name even when the
            # caller wraps it through ``tool_call``.
            _, _, err = resolve_underlying_call(
                {"name": blocked_name, "arguments": {"x": "y"}}
            )
            assert err, "resolve_underlying_call must reject the blocked name"
            assert "filtered" in err or "not available" in err, (
                f"rejection reason should reference the filter, got: {err!r}"
            )

            # Defense-in-depth gate (model_tools.handle_function_call
            # consults scoped_deferrable_names): blocked name absent.
            scoped = scoped_deferrable_names(bridge_defs)
            assert blocked_name not in scoped
            assert allowed_name in scoped
        finally:
            _cleanup_registry("github_bridge_test", registered)

    def test_dispatch_tool_search_fails_closed_on_helper_import_failure(
        self, monkeypatch
    ):
        """When ``tools.mcp_tool`` cannot be imported, ``dispatch_tool_search``
        must drop every MCP-prefixed def rather than surfacing them
        unfiltered -- the same fail-closed posture as the helper path."""
        import builtins

        from tools.tool_search import dispatch_tool_search

        blocked = "mcp__github__merge_pull_request"
        local = "local_acceptance_tool"
        definitions = [_td(blocked, "Merge a PR"), _td(local, "Local tool")]

        original_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "tools.mcp_tool":
                raise ImportError("simulated helper-import failure")
            return original_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", guarded_import)

        result = json.loads(
            dispatch_tool_search(
                {"query": "tool", "limit": 20},
                current_tool_defs=definitions,
            )
        )
        names = {match["name"] for match in result.get("matches", [])}
        assert blocked not in names, (
            "dispatch_tool_search must fail-closed when the policy helper "
            "is unavailable, not surface blocked MCP names."
        )

    def test_scoped_deferrable_names_fails_closed_on_helper_import_failure(
        self, monkeypatch
    ):
        """Same fail-closed contract for ``scoped_deferrable_names``: an
        import failure must not admit blocked MCP names to the scoped
        set that ``model_tools.handle_function_call`` consults."""
        import builtins

        from tools.tool_search import scoped_deferrable_names

        blocked = "mcp__github__merge_pull_request"
        definitions = [_td(blocked, "Merge a PR")]

        original_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "tools.mcp_tool":
                raise ImportError("simulated helper-import failure")
            return original_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", guarded_import)

        scoped = scoped_deferrable_names(definitions)
        assert blocked not in scoped, (
            "scoped_deferrable_names must fail-closed when the policy "
            "helper is unavailable, not admit blocked MCP names."
        )

    @pytest.mark.parametrize(
        "policy",
        [
            {"tools": {"include": ["safe"]}},
            {"tools": {"exclude": ["dangerous"]}},
        ],
    )
    def test_registration_with_stricter_policy_skips_blocked(
        self, monkeypatch, policy
    ):
        """A tool that the include/exclude policy rejects must NOT be
        added to the provenance map by ``_register_server_tools``.
        Stale-entry reconciliation for the *reconnect / list_changed*
        lifecycle is owned by ``MCPServerTask._refresh_tools`` (#68808);
        we do not re-test that lifecycle here. The contract for #72553
        is the *initial* registration path: a filtered tool never makes
        it into the map, so the deferred bridge cannot surface it.
        """
        from tools import mcp_tool
        from tools.mcp_tool import (
            MCP_TOOL_NAME_PREFIX,
            _mcp_tool_server_names,
            _register_server_tools,
        )

        server_name = "moa_filter_stricter_policy_test"
        blocked = f"{MCP_TOOL_NAME_PREFIX}{server_name}__dangerous"
        # Pre-condition: the provenance map is clean for this server.
        with mcp_tool._lock:
            for tn in [tn for tn, sn in _mcp_tool_server_names.items() if sn == server_name]:
                _mcp_tool_server_names.pop(tn, None)

        server = _StubMCPServer(
            server_name,
            [_StubMCPTool("dangerous", "Dangerous operation")],
        )
        try:
            registered = _register_server_tools(server_name, server, policy)
            assert blocked not in registered, (
                "_register_server_tools must not register a tool the policy rejects"
            )
            assert (
                mcp_tool.is_mcp_tool_filtered_in_session(blocked) is True
            ), (
                "after initial registration with a policy that filters the tool, "
                "the helper must report it as filtered (#72553)"
            )
            assert (
                mcp_tool.is_mcp_tool_filtered_in_session(
                    f"{MCP_TOOL_NAME_PREFIX}{server_name}__safe"
                )
                is True  # not registered in this test -> True (filtered / unknown)
            )  # sanity: helper never returns False for an unregistered name
        finally:
            _cleanup_registry(server_name, registered)
