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


class TestCrossOperationRemapGuard:
    """Regression for #94506: a tool name that the full registry knows
    but check_fn gated for this turn must NOT be fuzzy-matched onto a
    sibling tool.  Returning None lets the caller fall through to the
    normal 'unknown tool' error path."""

    def test_gated_tool_not_fuzzy_remappped_to_sibling(self, monkeypatch):
        """kanban_list is in the registry but gated — must not remap to
        kanban_link."""
        from run_agent import AIAgent

        # valid_tool_names excludes kanban_list (check_fn gated) but
        # includes the nearby kanban_link.
        stub = SimpleNamespace(valid_tool_names=VALID | {"kanban_link"})

        # Mock the registry to return kanban_list as a known tool.
        class _FakeRegistry:
            def get_all_tool_names(self):
                return sorted(VALID | {"kanban_list", "kanban_link",
                                       "kanban_complete", "kanban_block"})

        import tools.registry as _reg_mod
        monkeypatch.setattr(_reg_mod, "registry", _FakeRegistry())

        repair_fn = AIAgent._repair_tool_call.__get__(stub, AIAgent)
        # kanban_list is in the registry but not valid_tool_names —
        # must NOT be remapped to kanban_link.
        assert repair_fn("kanban_list") is None

    def test_unknown_tool_still_fuzzy_matched(self, monkeypatch):
        """A tool that is NEITHER in valid_tool_names NOR in the registry
        should still be fuzzy-matched as before (e.g. a typo)."""
        from run_agent import AIAgent

        stub = SimpleNamespace(valid_tool_names=VALID)

        class _FakeRegistry:
            def get_all_tool_names(self):
                return sorted(VALID)

        import tools.registry as _reg_mod
        monkeypatch.setattr(_reg_mod, "registry", _FakeRegistry())

        repair_fn = AIAgent._repair_tool_call.__get__(stub, AIAgent)
        # "brower_click" is a typo — fuzzy match should still find
        # "browser_click".
        assert repair_fn("brower_click") == "browser_click"

    def test_registry_import_failure_falls_through_to_fuzzy(self, monkeypatch):
        """If the registry import fails, the guard must not crash —
        fall through to the normal fuzzy match."""
        from run_agent import AIAgent

        stub = SimpleNamespace(valid_tool_names=VALID)

        # Make the import fail.
        import builtins
        real_import = builtins.__import__

        def _fail_tools_import(name, *args, **kwargs):
            if name == "tools.registry":
                raise ImportError("mocked failure")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail_tools_import)

        repair_fn = AIAgent._repair_tool_call.__get__(stub, AIAgent)
        # Should still fuzzy-match despite the import failure.
        assert repair_fn("brower_click") == "browser_click"
