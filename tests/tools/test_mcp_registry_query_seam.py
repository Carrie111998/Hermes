"""Seam tests for the mcp_tool R5 registry-query extraction.

``has_registered_mcp_tools`` and ``get_registered_mcp_server_names`` were
moved byte-verbatim from ``tools/mcp_tool.py`` into
``tools/mcp_registry_query.py``; ``tools/mcp_tool`` now re-exports the same
function objects. These tests pin the seam contract:

* object identity: the names re-exported by ``tools.mcp_tool`` ARE the
  objects defined in ``tools.mcp_registry_query`` (a re-export, not
  wrappers), so existing callers in ``agent/turn_context.py`` /
  ``gateway/session.py`` keep resolving the same callables;
* shared state: the moved functions read the SAME ``_lock`` /
  ``_mcp_tool_server_names`` objects owned by ``tools.mcp_tool`` — writes
  made through the original module are visible through the new module and
  vice versa (the lazy-import seam binds the original module's objects);
* behavior: empty-registry and populated-registry results match the
  pre-extraction semantics.
"""

import threading

import pytest

SEAM_NAMES = ("has_registered_mcp_tools", "get_registered_mcp_server_names")


@pytest.fixture()
def mcp_state():
    """Snapshot and restore the shared registry map around each test."""
    import tools.mcp_tool as mcp_tool

    with mcp_tool._lock:
        saved_names = dict(mcp_tool._mcp_tool_server_names)
        mcp_tool._mcp_tool_server_names.clear()
    try:
        yield mcp_tool
    finally:
        with mcp_tool._lock:
            mcp_tool._mcp_tool_server_names.clear()
            mcp_tool._mcp_tool_server_names.update(saved_names)


def _populate(mcp_tool, mapping):
    with mcp_tool._lock:
        mcp_tool._mcp_tool_server_names.update(mapping)


def _clear(mcp_tool):
    with mcp_tool._lock:
        mcp_tool._mcp_tool_server_names.clear()


class TestReexportIdentity:
    def test_reexported_names_are_identical_objects(self, mcp_state):
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        for name in SEAM_NAMES:
            assert getattr(mcp_tool, name) is getattr(mcp_registry_query, name)
            # Canonical implementation lives in the new module.
            assert getattr(mcp_registry_query, name).__module__ == (
                "tools.mcp_registry_query"
            )

    def test_mcp_tool_has_no_duplicate_definition(self, mcp_state):
        """mcp_tool.py must not define the moved names itself anymore."""
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        for name in SEAM_NAMES:
            mcp_impl = getattr(mcp_tool, name)
            query_impl = getattr(mcp_registry_query, name)
            assert mcp_impl is query_impl
            assert mcp_impl.__code__.co_filename.endswith("mcp_registry_query.py")

    def test_lazy_seam_reads_state_from_original_module(self, mcp_state, monkeypatch):
        """The aggressive identity proof: swap the original module's state
        objects for sentinels and prove the moved functions resolve them
        through ``tools.mcp_tool`` at call time (a stale import-time copy
        would keep reporting the cleared fixture state and fail this)."""
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        sentinel_lock = threading.Lock()
        sentinel_map = {"mcp__sentinel__x": "sentinel"}
        monkeypatch.setattr(mcp_tool, "_lock", sentinel_lock)
        monkeypatch.setattr(mcp_tool, "_mcp_tool_server_names", sentinel_map)

        assert mcp_registry_query.has_registered_mcp_tools() is True
        assert mcp_registry_query.get_registered_mcp_server_names() == {"sentinel"}


class TestBehavioral:
    def test_empty_registry(self, mcp_state):
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        assert mcp_registry_query.has_registered_mcp_tools() is False
        assert mcp_registry_query.get_registered_mcp_server_names() == set()
        assert mcp_tool.has_registered_mcp_tools() is False
        assert mcp_tool.get_registered_mcp_server_names() == set()

    def test_writes_through_mcp_tool_visible_via_new_module(self, mcp_state):
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        _populate(
            mcp_tool,
            {
                "mcp__filesystem__read": "filesystem",
                "mcp__github__list_issues": "github",
            },
        )
        try:
            assert mcp_registry_query.has_registered_mcp_tools() is True
            assert mcp_registry_query.get_registered_mcp_server_names() == {
                "filesystem",
                "github",
            }
        finally:
            _clear(mcp_tool)

    def test_shared_map_readable_in_both_directions(self, mcp_state):
        """The moved functions read the SAME map object owned by tools.mcp_tool:
        writes to the shared registry are visible through both modules."""
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        with mcp_tool._lock:
            mcp_tool._mcp_tool_server_names["mcp__slack__post"] = "slack"
        try:
            # Reads through the new module reflect the write above...
            assert mcp_registry_query.get_registered_mcp_server_names() == {"slack"}
            # ...and the shared map is the one owned by tools.mcp_tool.
            with mcp_tool._lock:
                assert mcp_tool._mcp_tool_server_names["mcp__slack__post"] == "slack"
        finally:
            _clear(mcp_tool)

    def test_both_modules_report_identical_results(self, mcp_state):
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        _populate(
            mcp_tool,
            {
                "mcp__a__x": "alpha",
                "mcp__b__y": "beta",
                "mcp__b__z": "beta",
            },
        )
        try:
            assert (
                mcp_tool.has_registered_mcp_tools()
                == mcp_registry_query.has_registered_mcp_tools()
                is True
            )
            assert (
                mcp_tool.get_registered_mcp_server_names()
                == mcp_registry_query.get_registered_mcp_server_names()
                == {"alpha", "beta"}
            )
        finally:
            _clear(mcp_tool)

    def test_returned_set_is_a_fresh_copy(self, mcp_state):
        """Mutation of the returned set must not leak into the registry."""
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        _populate(mcp_tool, {"mcp__a__x": "alpha"})
        try:
            names = mcp_registry_query.get_registered_mcp_server_names()
            names.add("beta")
            assert mcp_registry_query.get_registered_mcp_server_names() == {"alpha"}
            with mcp_tool._lock:
                assert "beta" not in mcp_tool._mcp_tool_server_names.values()
        finally:
            _clear(mcp_tool)

    def test_registry_truthiness_tracks_nonempty_map(self, mcp_state):
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        # Re-additions after an empty state flip the cheap check back on.
        _populate(mcp_tool, {"mcp__a__x": "alpha"})
        try:
            assert mcp_registry_query.has_registered_mcp_tools() is True
            _clear(mcp_tool)
            assert mcp_registry_query.has_registered_mcp_tools() is False
            _populate(mcp_tool, {"mcp__c__q": "charlie"})
            assert mcp_registry_query.has_registered_mcp_tools() is True
        finally:
            _clear(mcp_tool)

    def test_patch_on_mcp_tool_still_intercepts_callers(self, mcp_state):
        """Existing callers patch ``tools.mcp_tool.has_registered_mcp_tools``;
        the re-export must keep that patch surface working."""
        from unittest.mock import patch

        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True):
            # The patch replaces the mcp_tool attribute; the re-export seam is
            # the attribute itself, so the new module's function object is no
            # longer what mcp_tool exposes while patched.
            assert mcp_tool.has_registered_mcp_tools() is True
            # And the canonical object is untouched by the patch.
            assert mcp_registry_query.has_registered_mcp_tools() is False
