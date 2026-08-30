"""The formatter hook must not change generic persistence in any way.

The hook is consulted only AFTER tools/tool_result_storage.py has already
decided to persist a result. Thresholds, previews, persist decisions, aggregate
candidate order and the storage write are none of its business, and with an
empty registry the storage layer must behave exactly as it did before this
module existed.

The invariance checks are written as with-registry vs without-registry
comparisons rather than against a literal receipt, because the receipt names
the path the result was persisted to and that path is backend-dependent. What
must hold is that registering a formatter for one tool changes nothing for any
other tool, byte for byte.
"""

import json

import pytest

from tools import oversized_result_formatters as orf
from tools.budget_config import (
    DEFAULT_BUDGET,
    DEFAULT_PREVIEW_SIZE_CHARS,
    DEFAULT_RESULT_SIZE_CHARS,
    PINNED_THRESHOLDS,
)
from tools.tool_result_storage import (
    PERSISTED_OUTPUT_CLOSING_TAG,
    PERSISTED_OUTPUT_TAG,
    _BUDGET_TOOL_NAME,
    enforce_turn_budget,
    generate_preview,
    maybe_persist_tool_result,
)


@pytest.fixture
def empty_registry(monkeypatch):
    """No formatters at all -- the shipping default for every tool but one."""
    monkeypatch.setattr(orf, "_FORMATTERS", {})
    return orf


def _persist(tool, content, tool_use_id, env=None):
    return maybe_persist_tool_result(
        content=content,
        tool_name=tool,
        tool_use_id=tool_use_id,
        env=env,
        config=DEFAULT_BUDGET,
    )


def _without_registry(monkeypatch, fn):
    """Run *fn* with the registry emptied, then restore it."""
    saved = dict(orf._FORMATTERS)
    monkeypatch.setattr(orf, "_FORMATTERS", {})
    try:
        return fn()
    finally:
        monkeypatch.setattr(orf, "_FORMATTERS", saved)


class _SandboxEnv:
    """A remote-looking env whose sandbox write and readability probe succeed."""

    def get_temp_dir(self):
        return "/tmp"

    def execute(self, cmd, timeout=30, stdin_data=None):
        return {"returncode": 0}


class TestGenericPersistenceIsUnchanged:
    def test_non_skill_oversized_result_gets_the_generic_receipt(self, empty_registry):
        content = "x" * 100_001
        delivered = _persist("search_files", content, "tu_generic")
        preview, has_more = generate_preview(
            content, max_chars=DEFAULT_PREVIEW_SIZE_CHARS
        )
        assert len(preview) == 1_500 and has_more is True
        assert delivered != content
        assert "[SKILL_INCOMPLETE:" not in delivered

    def test_a_registered_formatter_leaves_a_non_skill_receipt_byte_identical(
        self, monkeypatch
    ):
        import tools.skills_tool  # noqa: F401  (registers skill_view's formatter)

        content = "x" * 100_001
        with_registry = _persist("search_files", content, "tu_generic")
        without = _without_registry(
            monkeypatch, lambda: _persist("search_files", content, "tu_generic")
        )
        assert with_registry == without

    def test_a_registered_formatter_leaves_a_sandbox_receipt_byte_identical(
        self, monkeypatch
    ):
        import tools.skills_tool  # noqa: F401  (registers skill_view's formatter)

        content = "y" * 100_001
        with_registry = _persist(
            "search_files", content, "tu_sandbox_generic", env=_SandboxEnv()
        )
        without = _without_registry(
            monkeypatch,
            lambda: _persist(
                "search_files", content, "tu_sandbox_generic", env=_SandboxEnv()
            ),
        )
        assert with_registry == without
        assert with_registry.startswith(PERSISTED_OUTPUT_TAG)
        assert with_registry.endswith(PERSISTED_OUTPUT_CLOSING_TAG)

    def test_a_result_at_the_threshold_is_still_returned_untouched(self, empty_registry):
        content = "z" * 100_000
        assert _persist("search_files", content, "tu_at_cap") == content


class TestAggregateSelectionIsUnchanged:
    def _messages(self):
        from agent.tool_dispatch_helpers import make_tool_result_message

        sizes = [("search_files", 90_000), ("terminal", 70_000), ("web_search", 55_000)]
        return [
            make_tool_result_message(n, "q" * s, f"tu_{i}")
            for i, (n, s) in enumerate(sizes)
        ]

    def test_empty_registry_selects_the_same_candidates_in_the_same_order(
        self, empty_registry
    ):
        messages = self._messages()
        before = [m["content"] for m in messages]
        enforce_turn_budget(messages, env=None, config=DEFAULT_BUDGET)
        after = [m["content"] for m in messages]

        # Largest-first until under budget: 215,000 total, 200,000 budget, so
        # exactly the single largest result is spilled and nothing else moves.
        assert after[0] != before[0]
        assert after[1] == before[1]
        assert after[2] == before[2]
        assert "[SKILL_INCOMPLETE:" not in after[0]

    def test_a_registered_formatter_changes_no_non_skill_aggregate_spill(
        self, monkeypatch
    ):
        import tools.skills_tool  # noqa: F401  (registers skill_view's formatter)

        def _run():
            messages = self._messages()
            enforce_turn_budget(messages, env=None, config=DEFAULT_BUDGET)
            return [m["content"] for m in messages]

        with_registry = _run()
        without = _without_registry(monkeypatch, _run)
        assert with_registry == without

    def test_under_budget_turns_are_never_touched(self, empty_registry):
        from agent.tool_dispatch_helpers import make_tool_result_message

        messages = [make_tool_result_message("search_files", "q" * 10, "tu_small")]
        before = [m["content"] for m in messages]
        enforce_turn_budget(messages, env=None, config=DEFAULT_BUDGET)
        assert [m["content"] for m in messages] == before

    def test_aggregate_enforcement_still_forces_threshold_zero(
        self, empty_registry, monkeypatch
    ):
        """The synthetic tool name and threshold=0 are what decide an aggregate
        spill. The real tool name is passed for the formatter lookup ONLY."""
        import tools.tool_result_storage as trs
        from agent.tool_dispatch_helpers import make_tool_result_message

        seen = []
        real = trs.maybe_persist_tool_result

        def _spy(**kw):
            seen.append(kw)
            return real(**kw)

        monkeypatch.setattr(trs, "maybe_persist_tool_result", _spy)
        messages = [
            make_tool_result_message("skill_view", "a" * 150_000, "tu_x"),
            make_tool_result_message("search_files", "b" * 60_000, "tu_y"),
        ]
        trs.enforce_turn_budget(messages, env=None, config=DEFAULT_BUDGET)

        assert seen, "no candidate was persisted"
        for call in seen:
            assert call["threshold"] == 0
            assert call["tool_name"] == _BUDGET_TOOL_NAME
        assert seen[0]["formatter_name"] == "skill_view"


class TestThresholdsAreUnchanged:
    def test_skill_view_threshold_is_still_the_default(self):
        import tools.skills_tool  # noqa: F401  (registers the tool)
        from tools.registry import registry

        assert DEFAULT_BUDGET.resolve_threshold("skill_view") == 100_000
        assert DEFAULT_RESULT_SIZE_CHARS == 100_000
        assert registry.get_entry("skill_view").max_result_size_chars is None
        assert "skill_view" not in PINNED_THRESHOLDS
        assert "skill_view" not in DEFAULT_BUDGET.tool_overrides


class TestRegistryContract:
    def test_registry_is_empty_apart_from_skill_view(self):
        import tools.skills_tool  # noqa: F401  (registers on import)

        assert set(orf._FORMATTERS) == {"skill_view"}

    def test_no_formatter_means_no_opinion(self, empty_registry):
        assert orf.format_oversized_result("anything", "search_files") is None
        assert orf.format_oversized_result("anything", "") is None
        assert orf.has_formatter("search_files") is False

    def test_a_formatter_that_raises_degrades_to_the_generic_receipt(
        self, empty_registry, monkeypatch
    ):
        content = "x" * 100_001
        baseline = _persist("search_files", content, "tu_boom")

        def _boom(content, *, tool_name):
            raise RuntimeError("formatter is broken")

        orf.register_formatter("search_files", _boom)
        assert orf.format_oversized_result("anything", "search_files") is None
        assert _persist("search_files", content, "tu_boom") == baseline

    def test_a_formatter_that_declines_falls_through(self, empty_registry):
        orf.register_formatter("search_files", lambda content, *, tool_name: None)
        assert orf.format_oversized_result("anything", "search_files") is None
        orf.register_formatter("search_files", lambda content, *, tool_name: "")
        assert orf.format_oversized_result("anything", "search_files") is None

    def test_register_rejects_nonsense(self, empty_registry):
        with pytest.raises(ValueError):
            orf.register_formatter("", lambda c, *, tool_name: None)
        with pytest.raises(ValueError):
            orf.register_formatter("x", "not callable")


class TestRegisteredFormatterDoesNotLeakToOtherTools:
    """Run against the REAL registry, with skill_view's formatter registered.
    A lookup that falls back to "some formatter" instead of "this tool's
    formatter" would rewrite every other tool's receipt, so the byte-identity
    checks above must also hold while a formatter exists.
    """

    @pytest.fixture(autouse=True)
    def _real_registry(self):
        import tools.skills_tool  # noqa: F401  (registers skill_view)

        assert orf.has_formatter("skill_view")

    @pytest.mark.parametrize(
        "tool", ["search_files", "terminal", "web_search", "memory"]
    )
    def test_other_tools_still_get_the_generic_receipt(self, tool, monkeypatch):
        content = "x" * 100_001
        delivered = _persist(tool, content, f"tu_{tool}")
        without = _without_registry(
            monkeypatch, lambda: _persist(tool, content, f"tu_{tool}")
        )
        assert delivered == without
        assert "[SKILL_INCOMPLETE:" not in delivered
        assert orf.has_formatter(tool) is False

    def test_a_skill_shaped_payload_from_another_tool_is_not_rewritten(
        self, monkeypatch
    ):
        """Same bytes, different tool name: the lookup is by name, not shape."""
        payload = json.dumps(
            {
                "success": True,
                "name": "looks-like-a-skill",
                "content": "# H\n" + "b" * 100_001,
            }
        )
        delivered = _persist("read_file_alt", payload, "tu_shape")
        without = _without_registry(
            monkeypatch, lambda: _persist("read_file_alt", payload, "tu_shape")
        )
        assert "[SKILL_INCOMPLETE:" not in delivered
        assert delivered == without

    def test_aggregate_spill_of_a_non_skill_result_is_the_generic_block(
        self, monkeypatch
    ):
        from agent.tool_dispatch_helpers import make_tool_result_message

        def _run():
            messages = [
                make_tool_result_message("search_files", "q" * 150_000, "tu_ns1"),
                make_tool_result_message("terminal", "r" * 60_000, "tu_ns2"),
            ]
            enforce_turn_budget(messages, env=None, config=DEFAULT_BUDGET)
            return messages[0]["content"]

        delivered = _run()
        without = _without_registry(monkeypatch, _run)
        assert "[SKILL_INCOMPLETE:" not in delivered
        assert delivered == without
