"""Tests for AIAgent._repair_tool_call — tool-name normalization.

Regression guard for #14784: Claude-style models sometimes emit
class-like tool-call names (``TodoTool_tool``, ``Patch_tool``,
``BrowserClick_tool``, ``PatchTool``). Before the fix they returned
"Unknown tool" even though the target tool was registered under a
snake_case name. The repair routine now normalizes CamelCase,
strips trailing ``_tool`` / ``-tool`` / ``tool`` suffixes (up to
twice to handle double-tacked suffixes like ``TodoTool_tool``), and
falls back to fuzzy match.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


VALID = {
    "todo",
    "patch",
    "browser_click",
    "browser_navigate",
    "web_search",
    "read_file",
    "write_file",
    "terminal",
    "execute_code",
    "session_search",
}


@pytest.fixture
def repair():
    """Return a bound _repair_tool_call built on a minimal shell agent.

    We avoid constructing a real AIAgent (which pulls in credential
    resolution, session DB, etc.) because the repair routine only
    reads self.valid_tool_names. A SimpleNamespace stub is enough to
    bind the unbound function.
    """
    from run_agent import AIAgent
    stub = SimpleNamespace(valid_tool_names=VALID)
    return AIAgent._repair_tool_call.__get__(stub, AIAgent)


class TestExistingBehaviorStillWorks:
    """Pre-existing repairs must keep working (no regressions)."""

    def test_lowercase_already_matches(self, repair):
        assert repair("browser_click") == "browser_click"







class TestClassLikeEmissions:
    """Regression coverage for #14784 — CamelCase + _tool suffix variants."""

    def test_camel_case_no_suffix(self, repair):
        assert repair("BrowserClick") == "browser_click"









class TestEdgeCases:
    """Edge inputs that must not crash or produce surprising results."""

    def test_empty_string(self, repair):
        assert repair("") is None





class TestVolcEngineXmlPollution:
    """Regression coverage for #33007 — VolcEngine ``api/plan`` endpoint
    leaks raw XML attribute fragments into ``tool_use.name``.

    Observed in production with the ``anthropic_messages`` API mode:

        terminal" parameter="command" string="true
        execute_code" parameter="code" string="true
        session_search" parameter="session_id" string="true

    The fix trims at the first ``"``/``'``/``<``/``>`` so the rest of
    the repair pipeline can resolve the cleaned name to a real tool.
    """

    def test_terminal_with_xml_attribute_pollution(self, repair):
        # Exact pattern from the bug report (terminal call).
        polluted = 'terminal" parameter="command" string="true'
        assert repair(polluted) == "terminal"




    def test_tool_name_with_trailing_quote_only(self, repair):
        # Minimal leak — just a stray trailing quote, no full attribute.
        assert repair('terminal"') == "terminal"



    def test_clean_tool_name_unaffected_by_sanitizer(self, repair):
        # Pure passthrough — no XML/quote chars, no change.
        assert repair("execute_code") == "execute_code"
        assert repair("session_search") == "session_search"

    def test_space_separated_name_still_normalizes(self, repair):
        # Critical: the XML strip must NOT consume whitespace, or the
        # legitimate ``"write file" -> write_file`` repair path breaks.
        assert repair("write file") == "write_file"


    def test_leading_quote_falls_through_to_fuzzy_match(self, repair):
        # Sanitizer only trims when the XML char is at idx > 0 — a
        # name that *starts* with a quote is left untouched so the
        # rest of the pipeline (fuzzy match at 0.7 cutoff) can still
        # recover the obvious target.
        assert repair('"terminal"') == "terminal"


class TestGatedToolNotRemappedOntoSibling:
    """Regression coverage for #94506.

    A tool name the registry knows but ``check_fn`` withheld for this turn
    (e.g. ``kanban_list`` hidden from dispatcher workers via
    ``_check_kanban_orchestrator_mode``) is absent from ``valid_tool_names``.
    Before the fix, the step-5 fuzzy fallback remapped it onto the nearest
    available sibling (``kanban_list`` -> ``kanban_link``), silently turning a
    read into a different operation. The fix refuses to substitute a sibling
    for a real-but-gated name: it falls through to the normal "unknown tool"
    path (returns None).
    """

    @pytest.fixture
    def gated_repair(self):
        """Bind _repair_tool_call on a stub whose valid_tool_names excludes the
        orchestrator-only kanban tools, mimicking a dispatcher-spawned worker."""
        from run_agent import AIAgent
        from tools.registry import registry

        all_names = set(registry.get_all_tool_names())
        # A kanban worker's post-gating set omits the board-routing tools.
        assert "kanban_list" in all_names, "kanban_list must be registered for this test"
        assert "kanban_link" in all_names, "kanban_link must be registered for this test"
        worker_valid = all_names - {"kanban_list", "kanban_unblock"}
        stub = SimpleNamespace(valid_tool_names=worker_valid)
        return AIAgent._repair_tool_call.__get__(stub, AIAgent)

    def test_gated_exact_name_is_not_remapped_to_sibling(self, gated_repair):
        # A real tool that is gated this turn must NOT become its sibling.
        assert gated_repair("kanban_list") is None

    def test_gated_camel_case_name_is_not_remapped(self, gated_repair):
        # Normalized spellings of the gated name are caught too (not just the
        # exact lowercase form), so CamelCase can't dodge the guard.
        assert gated_repair("KanbanList") is None

    def test_genuine_typo_of_available_tool_still_repairs(self, gated_repair):
        # A typo of an *available* tool must still fuzzy-match — the guard only
        # covers names that resolve to a real registered tool.
        assert gated_repair("broswe_click") == "browser_click"
