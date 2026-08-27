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
from tools.delegate_routing import MUTATING_TOOLS, filter_tools, routing_enabled, status_label

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


def test_on_withholds_every_way_of_changing_a_repository():
    """The list is enumerated, so the test enumerates it too rather than sampling.

    A test that only checked `patch` and `write_file` would have passed against
    the first version, which left desktop control available.
    """
    remaining = filter_tools(EVERY_MUTATOR | HARMLESS, ON)
    assert remaining == HARMLESS, f"still offered: {sorted(remaining & EVERY_MUTATOR)}"
    for tool in EVERY_MUTATOR:
        assert tool in MUTATING_TOOLS, f"{tool} is a mutation vector and must be listed"


def test_desktop_control_is_not_forgotten():
    """Named on its own because it was, once.

    computer_use is "Universal desktop control": with it, an editor is a GUI away
    and every other entry in the list is decoration. It was missed by a
    pattern-matched first draft that looked for words like "write" and "edit".
    """
    assert "computer_use" in MUTATING_TOOLS
    assert "computer_use" not in filter_tools({"computer_use", "read_file"}, ON)


def test_deferred_and_indirect_execution_count_too():
    """A scheduled command runs later; a browser driver runs code now.

    Neither looks like an editor, and both end at a shell.
    """
    assert "cronjob" in MUTATING_TOOLS
    assert "browser_exec" in MUTATING_TOOLS
    assert "execute_code" in MUTATING_TOOLS, (
        "execute_code calls other tools programmatically -- leaving it would route "
        "around every other entry in this set"
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
