"""The switch that decides whether Hermes may change a repository itself.

The claim it makes is strong -- "mechanically disallowed", not "discouraged" --
so these tests attack the three ways such a claim usually turns out to be false:
something fails and the guard quietly stops applying, the list of dangerous
things is incomplete, and a second path exists that never consults the guard.

All three were real. The first version of this feature failed open on any error,
its tool list was a pattern guess that missed desktop control, and nothing proved
that delegated children were covered.
"""

import pytest

from tools import delegate_routing
from tools.delegate_routing import filter_tools, routing_enabled, status_label

ON = {"delegate_wave": {"route_repo_changes": True}}
OFF = {"delegate_wave": {"route_repo_changes": False}}

# Everything a model could reach for to change files on this machine.
EVERY_MUTATOR = {"patch", "write_file", "execute_code", "terminal", "computer_use",
                 "cronjob", "browser_exec"}
HARMLESS = {"read_file", "search_files", "session_search", "web_search"}


def test_off_by_default_so_an_existing_install_is_unchanged():
    """The switch must be opt-in. Anyone who has not asked for it keeps every tool."""
    assert routing_enabled({}) is False
    everything = EVERY_MUTATOR | HARMLESS
    assert filter_tools(everything, {}) == everything
    assert filter_tools(everything, OFF) == everything
    assert status_label({}) == ""


class _FakeEntry:
    def __init__(self, repo_access, *, toolset="test", handler=None, origin="plugin"):
        self.repo_access = repo_access
        self.toolset = toolset
        self.handler = handler or (lambda: None)
        self.origin = origin


class _FakeRegistry:
    """A registry containing exactly the tools a test declares.

    The point of these tests is that the policy consults DECLARATIONS, so the
    fixture has to be able to invent a tool that has never existed. A test that
    could only use the real registry could never prove the interesting claim:
    that a tool nobody has written yet is handled correctly.
    """

    def __init__(self, declarations):
        self._entries = {n: _FakeEntry(a) for n, a in declarations.items()}

    def get_entry(self, name, scope=None):
        return self._entries.get(name)

    def _plugin_owner_of(self, handler):
        return None


class _ProvenanceRegistry(_FakeRegistry):
    """A registry whose entries can model extension provenance."""

    def __init__(self, entries, plugin_handlers=()):
        self._entries = entries
        self._plugin_handlers = set(plugin_handlers)

    def _plugin_owner_of(self, handler):
        return "hermes_plugins.test" if handler in self._plugin_handlers else None


def test_on_withholds_every_way_of_changing_a_repository():
    """Enumerated through declarations rather than through a list of names."""
    declarations = {t: "write" for t in EVERY_MUTATOR}
    declarations.update({t: "read" for t in HARMLESS})
    reg = _FakeRegistry(declarations)

    remaining = filter_tools(EVERY_MUTATOR | HARMLESS, ON, reg)
    assert remaining == HARMLESS, f"still offered: {sorted(remaining & EVERY_MUTATOR)}"


def test_a_brand_new_mutating_tool_is_blocked_without_being_named_anywhere():
    """THE REASON THE NAME LIST WAS REPLACED.

    `MUTATING_TOOLS` was correct only for the tools that existed when someone
    last read it. This tool has never existed; nothing anywhere knows its name;
    it declares write and is withheld for that reason alone.
    """
    reg = _FakeRegistry({
        "quantum_refactorer_9000": "write",
        "read_file": "read",
    })
    remaining = filter_tools({"quantum_refactorer_9000", "read_file"}, ON, reg)
    assert remaining == {"read_file"}
    assert "quantum_refactorer_9000" not in remaining, (
        "a newly registered mutating tool was offered because no list mentions it"
    )


def test_a_brand_new_read_only_tool_is_still_offered():
    """The other half. Withholding everything would be trivially 'safe' and useless."""
    reg = _FakeRegistry({
        "repository_archaeologist": "read",
        "weather_lookup": "none",
        "patch": "write",
    })
    remaining = filter_tools({"repository_archaeologist", "weather_lookup", "patch"}, ON, reg)
    assert remaining == {"repository_archaeologist", "weather_lookup"}


@pytest.mark.parametrize("declared", [None, "", "  ", "WRITE-ish", "readonly", 42, True, ["read"]])
def test_missing_or_nonsense_metadata_fails_closed(declared):
    """UNDECLARED AND MISLABELLED BOTH MEAN WITHHELD.

    A default of "harmless" would turn every oversight -- a new built-in nobody
    classified, a plugin, a typo in a config -- into a silent hole. Note that
    True is included: a truthy non-string must not be mistaken for permission.
    """
    reg = _FakeRegistry({"mystery_tool": declared, "read_file": "read"})
    remaining = filter_tools({"mystery_tool", "read_file"}, ON, reg)
    assert remaining == {"read_file"}, (
        f"repo_access={declared!r} was treated as permission to offer the tool"
    )


def test_a_tool_missing_from_the_registry_entirely_is_withheld():
    """A name that resolves to nothing is not evidence that it is safe."""
    reg = _FakeRegistry({"read_file": "read"})
    assert filter_tools({"ghost_tool", "read_file"}, ON, reg) == {"read_file"}


def test_undeclared_plugin_override_does_not_inherit_builtin_manifest_by_name():
    """A plugin replacing a harmless built-in does not inherit its permission."""
    plugin_handler = lambda: None
    reg = _ProvenanceRegistry(
        {"read_file": _FakeEntry(None, toolset="file", handler=plugin_handler)},
        plugin_handlers={plugin_handler},
    )

    assert filter_tools({"read_file"}, ON, reg) == set()


def test_plugin_cannot_launder_builtin_trust_through_a_core_handler(monkeypatch):
    """Registration records the plugin caller, not only handler provenance."""
    from tools.registry import ToolRegistry, registration_origin

    reg = ToolRegistry()
    monkeypatch.setattr(reg, "_caller_module", lambda: "hermes_plugins.test")
    reg.register(
        name="read_file",
        toolset="file",
        schema={"name": "read_file", "parameters": {}},
        handler=lambda: None,  # defined in this core test module, not the plugin
    )

    entry = reg.get_entry("read_file")
    assert registration_origin(entry, reg) == "plugin"
    assert filter_tools({"read_file"}, ON, reg) == set()


def test_undeclared_mcp_tool_does_not_inherit_builtin_manifest_by_name():
    """Dynamic tools also remain undeclared even when their name collides."""
    reg = _ProvenanceRegistry({
        "read_file": _FakeEntry(None, toolset="mcp-hostile", origin="mcp"),
    })

    assert filter_tools({"read_file"}, ON, reg) == set()


def test_explicit_extension_capability_is_used_despite_builtin_name_collision():
    plugin_handler = lambda: None
    reg = _ProvenanceRegistry(
        {"patch": _FakeEntry("read", toolset="file", handler=plugin_handler)},
        plugin_handlers={plugin_handler},
    )

    assert filter_tools({"patch"}, ON, reg) == {"patch"}


def test_builtin_manifest_wins_over_accidental_inline_declaration():
    """Core has one authority even if an old-style annotation reappears."""
    from tools.registry import ToolRegistry, repo_access_of

    reg = ToolRegistry()
    reg.register(
        name="read_file",
        toolset="file",
        schema={"name": "read_file", "parameters": {}},
        handler=lambda: None,
        repo_access="write",
    )

    assert repo_access_of("read_file", reg) == "read"


def test_case_and_whitespace_in_a_declaration_are_tolerated():
    """Declarations are written by hand, in config as well as in code."""
    reg = _FakeRegistry({"a": " Read ", "b": "NONE", "c": "Write"})
    assert filter_tools({"a", "b", "c"}, ON, reg) == {"a", "b"}


def test_every_registered_builtin_has_manifest_capability():
    """WHERE THE COST OF FAILING CLOSED IS PAID.

    Undeclared means withheld, so an unclassified tool silently disappears while
    the switch is on. This test moves that cost onto whoever adds a tool, at the
    time they add it, instead of onto the person wondering where their tool went.
    """
    import model_tools  # noqa: F401  -- triggers builtin discovery
    from tools.registry import registration_origin, registry
    from tools.repo_access import BUILTIN_REPO_ACCESS

    registered = {
        name for name, entry in registry._tools.items()
        if registration_origin(entry, registry) == "builtin"
    }
    manifest_names = set(BUILTIN_REPO_ACCESS)
    missing = sorted(registered - manifest_names)
    stale = sorted(manifest_names - registered)
    assert not missing, (
        "these registered built-ins have no manifest capability and will be withheld "
        f"whenever delegate-wave routing is on: {missing}"
    )
    assert not stale, (
        "these manifest names no longer identify a registered built-in and could "
        f"grant stale trust if a different tool later reuses the name: {stale}"
    )

    inline = sorted(
        name for name, entry in registry._tools.items()
        if registration_origin(entry, registry) == "builtin"
        and entry.repo_access is not None
    )
    assert not inline, (
        "built-ins must not declare repo_access inline; the manifest is their "
        f"only authority: {inline}"
    )

    invalid = {
        name: access for name, access in BUILTIN_REPO_ACCESS.items()
        if access not in {"write", "delegated_write", "read", "none"}
    }
    assert not invalid, f"invalid built-in repository capabilities: {invalid}"


def test_missing_builtin_manifest_entry_is_withheld(monkeypatch):
    """The manifest is authorization, not documentation."""
    import model_tools  # noqa: F401
    from tools.repo_access import BUILTIN_REPO_ACCESS

    monkeypatch.delitem(BUILTIN_REPO_ACCESS, "read_file")
    assert filter_tools({"read_file"}, ON) == set()


def test_the_seven_known_mutators_still_declare_write():
    """The classification is new; the judgement behind it is not.

    These seven were chosen by hand, argued about, and one (computer_use) was
    missed by a pattern-matched first draft. Pinning them here means the move to
    metadata cannot quietly reclassify one as harmless.
    """
    import model_tools  # noqa: F401
    from tools.registry import registry

    for tool in EVERY_MUTATOR:
        entry = registry._tools.get(tool)
        if entry is None:
            continue  # not every install has every toolset available
        from tools.registry import repo_access_of
        assert repo_access_of(tool) == "write", (
            f"{tool} now resolves to {repo_access_of(tool)!r}; it is a mutation vector"
        )


def test_a_failure_while_the_switch_is_on_does_not_hand_the_tools_back(monkeypatch):
    """FAIL CLOSED. The property that makes the switch worth having.

    The first version wrapped the call in `except Exception: pass`, reasoning that
    a broken guard should not cost the user their tools. That is backwards here:
    losing the tools is visible and costs a turn, while silently regaining them
    costs the guarantee and is noticed only when a transcript shows Hermes editing
    a repository it was configured never to touch.
    """
    def explode(*args, **kwargs):
        raise RuntimeError("config store unavailable")

    monkeypatch.setattr(delegate_routing, "routing_enabled", explode)
    with pytest.raises(RuntimeError):
        filter_tools(EVERY_MUTATOR, ON)


def test_the_call_site_does_not_swallow_that_failure():
    """The guard is only as good as the one place that applies it.

    Asserted against the source, because the mistake was not in this module -- it
    was a try/except around the call in model_tools.py, where no unit test of
    delegate_routing would ever have seen it.
    """
    import ast
    import inspect
    import textwrap
    import model_tools

    # PARSED, NOT PATTERN-MATCHED.
    #
    # The first version of this test searched the 600 characters BEFORE the call for
    # "except Exception". A restored try/except puts its handler AFTER the call, so
    # the assertion looked at the wrong side of the statement and passed against the
    # exact defect it was written to catch.
    tree = ast.parse(textwrap.dedent(inspect.getsource(model_tools._compute_tool_definitions)))

    def mentions_filter(node):
        return any(
            isinstance(child, ast.Name) and child.id == "_route_filter"
            for child in ast.walk(node)
        )

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and mentions_filter(n)]
    assert calls, "the routing filter is no longer called at the tool-assembly point"

    for try_node in [n for n in ast.walk(tree) if isinstance(n, ast.Try)]:
        for guarded in try_node.body:
            assert not mentions_filter(guarded), (
                "the routing filter is inside a try block again; a failure there "
                "restores patch, write_file, terminal and computer_use silently"
            )


def test_delegated_children_are_covered_by_construction():
    """delegate_task must not become the escape hatch.

    A child is a fresh AIAgent, and if it assembled its tools by another route it
    would inherit none of this. It does not: agent_init builds every agent --
    parent and child alike -- through get_tool_definitions, which is where the
    filter lives. Asserted structurally rather than by spawning a real subagent,
    which would need a provider and would prove the same thing more slowly.
    """
    import inspect
    import agent.agent_init as agent_init
    import model_tools

    assert "get_tool_definitions" in inspect.getsource(agent_init), (
        "children are built somewhere that does not consult the tool assembly path"
    )
    # And that path is the one carrying the filter.
    assert "_route_filter" in inspect.getsource(model_tools._compute_tool_definitions)


def test_the_guidance_names_the_route_that_remains():
    """Withholding without explanation produces a model that retries and blames its tools."""
    assert "session_start" in delegate_routing.GUIDANCE
    assert "delegate_wave" in delegate_routing.GUIDANCE
    assert status_label(ON) == "Delegate Wave ON"


# --- MCP: capability is declared per tool, never per server --------------------

def test_a_new_tool_on_an_already_declared_mcp_server_is_withheld():
    """THE FALSIFICATION THAT CLOSES THE FUTURE-TOOL HOLE ON THE MCP SIDE.

    Replacing the built-in name list removed the "correct only for the tools that
    existed when someone last read it" failure -- and a server-wide
    `repo_access: none` would have reintroduced it one layer out. delegate-wave
    shipping `direct_apply_patch` tomorrow would inherit "harmless" from a line
    written today, and Hermes would be handed a direct mutator by a switch whose
    entire purpose is to withhold them.

    So the server's declaration is a MAPPING, and a tool missing from it is
    undeclared. This test asks for a tool that no config has ever mentioned.
    """
    from tools.mcp_tool import _resolve_mcp_repo_access

    config = {
        "repo_access": {
            "session_start": "delegated_write",
            "session_poll": "none",
        }
    }

    # Everything the server declares today resolves as declared.
    assert _resolve_mcp_repo_access(config, "session_start", "mcp__dw__session_start") == "delegated_write"
    assert _resolve_mcp_repo_access(config, "session_poll", "mcp__dw__session_poll") == "none"

    # A tool that appears on that same server tomorrow does not.
    newcomer = _resolve_mcp_repo_access(config, "direct_apply_patch", "mcp__dw__direct_apply_patch")
    assert newcomer is None, (
        "a newly discovered MCP tool inherited a capability nobody declared for it"
    )

    # And the routing filter withholds it for exactly that reason.
    reg = _FakeRegistry({
        "mcp__dw__session_start": "delegated_write",
        "mcp__dw__direct_apply_patch": newcomer,
    })
    remaining = filter_tools(
        {"mcp__dw__session_start", "mcp__dw__direct_apply_patch"}, ON, reg
    )
    assert remaining == {"mcp__dw__session_start"}


def test_a_permissive_server_wide_default_is_refused():
    """A server-wide value may make things STRICTER, never laxer.

    `repo_access: none` on the server entry is the blanket grant this design
    exists to prevent, so it is ignored rather than honoured -- loudly, because
    somebody wrote it intending it to do something.
    """
    from tools.mcp_tool import _resolve_mcp_repo_access

    for permissive in ("none", "read", "delegated_write", " NONE "):
        assert _resolve_mcp_repo_access({"repo_access": permissive}, "x", "mcp__s__x") is None, (
            f"server-wide repo_access={permissive!r} granted permission to every future tool"
        )

    # Restrictive is allowed: it can only withhold more.
    assert _resolve_mcp_repo_access({"repo_access": "write"}, "x", "mcp__s__x") == "write"


def test_the_sanctioned_route_is_allowed_by_category_not_by_name():
    """session_start survives the switch because of WHAT IT DECLARES.

    Nothing in the policy knows the string "session_start", "delegate_wave", or
    "mcp__". A tool is allowed through because it declared delegated_write, and
    any tool that declares it would be -- which is the property that makes this a
    rule rather than an exception.
    """
    reg = _FakeRegistry({
        "mcp__delegate_wave__session_start": "delegated_write",
        "mcp__delegate_wave__session_poll": "none",
        "mcp__other_vendor__commission_work": "delegated_write",
        "mcp__delegate_wave__direct_apply_patch": "write",
        "patch": "write",
    })
    remaining = filter_tools(set(reg._entries), ON, reg)
    assert remaining == {
        "mcp__delegate_wave__session_start",
        "mcp__delegate_wave__session_poll",
        "mcp__other_vendor__commission_work",
    }
    assert "mcp__delegate_wave__direct_apply_patch" not in remaining, (
        "a directly-mutating tool was allowed because of the server it came from"
    )


def test_delegated_write_is_withheld_when_the_switch_is_off_only_in_the_sense_that_nothing_is():
    """Sanity: the switch off is still a no-op for every category."""
    reg = _FakeRegistry({"a": "delegated_write", "b": "write", "c": None})
    assert filter_tools({"a", "b", "c"}, OFF, reg) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Two holes found by review of the shipped classification, kept closed here.
# ---------------------------------------------------------------------------


def test_a_config_that_cannot_be_read_is_not_treated_as_off():
    """CANNOT DETERMINE is not the same answer as OFF.

    routing_enabled used to catch every config-read failure and return False.
    That reads as "the user did not ask for routing", but the actual state is
    "we do not know what the user asked for" -- and answering False there hands
    back patch, write_file, terminal and computer_use on the strength of a YAML
    syntax error, silently, which is the exact fail-open this module exists to
    prevent.
    """
    import tools.delegate_routing as dr

    class Unreadable:
        def __call__(self):
            raise OSError("config.yaml is not readable")

    import sys
    import types

    stub = types.ModuleType("hermes_cli.config")
    stub.load_config_readonly = Unreadable()
    saved = sys.modules.get("hermes_cli.config")
    sys.modules["hermes_cli.config"] = stub
    try:
        with pytest.raises(dr.RoutingConfigUnreadable):
            routing_enabled()

        # And the filter must not answer either -- it must not silently keep
        # every mutating tool because the switch could not be evaluated.
        with pytest.raises(dr.RoutingConfigUnreadable):
            filter_tools(EVERY_MUTATOR | HARMLESS)
    finally:
        if saved is not None:
            sys.modules["hermes_cli.config"] = saved
        else:
            del sys.modules["hermes_cli.config"]


def test_a_non_mapping_config_is_not_treated_as_off():
    """Same reasoning as above for a config that loaded but is not a mapping."""
    import tools.delegate_routing as dr

    with pytest.raises(dr.RoutingConfigUnreadable):
        routing_enabled(["not", "a", "mapping"])


def test_process_cannot_be_used_to_reach_a_shell_that_is_already_running():
    """`process` sends arbitrary stdin, so withholding `terminal` is not enough.

    It was declared "none" on the reasoning that it only manages processes
    STARTED by `terminal`, so withholding `terminal` left it nothing to act on.
    That misses the case that matters: a shell started BEFORE the switch went
    on, or from another surface, is still attached, and `process` action
    `submit` is stdin plus Enter -- which is a command line.
    """
    import model_tools  # noqa: F401
    from tools.registry import registry

    entry = registry._tools.get("process")
    if entry is None:
        pytest.skip("process tool not registered in this install")

    from tools.registry import repo_access_of

    assert repo_access_of("process") == "write", (
        "process sends arbitrary stdin via its write/submit actions; declaring "
        "it harmless leaves a route to a running shell while terminal is withheld"
    )
    assert "process" not in filter_tools({"process", "read_file"}, ON)
    # Still available when the switch is off, like everything else.
    assert "process" in filter_tools({"process", "read_file"}, OFF)
