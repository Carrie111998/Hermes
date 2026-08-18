"""Post-swap import safety for ``hermes update``.

Regression test for #88371: after the working tree is swapped to the new
revision, a post-update step importing a module chain for the first time
mixes new-revision files with ``sys.modules`` entries cached from the old
revision — the dashboard-cleanup step's ``gateway.status`` import crashed
with ``ImportError`` after ``✓ Update complete!`` because the new
``hermes_cli.auth`` expected a symbol the old cached ``hermes_cli.config``
lacked.

The fix preloads the cleanup chains BEFORE the swap so every post-swap
import is served from ``sys.modules`` and the process stays on one
coherent revision.
"""

import builtins
import inspect
import sys

from hermes_cli import update_cmd


def test_preload_imports_gateway_status():
    sys.modules.pop("gateway.status", None)
    update_cmd._preload_post_update_imports()
    assert "gateway.status" in sys.modules


def test_preload_never_raises_on_import_failure(monkeypatch):
    def _boom(name, *args, **kwargs):
        if name == "gateway.status" or name == "gateway":
            raise ImportError("simulated broken new-revision tree")
        return builtins.__import__(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    # Must be best-effort: a failed preload can never block the update.
    update_cmd._preload_post_update_imports()


def test_both_update_paths_preload_before_any_work():
    """The preload must run in BOTH update paths, before the tree is swapped.

    Structural guard: asserts the call site exists in each entry function so
    a future refactor cannot silently drop it (the crash only reappears on
    the next cross-revision update, far from the change that caused it).
    """
    zip_src = inspect.getsource(update_cmd._update_via_zip)
    git_src = inspect.getsource(update_cmd._cmd_update_impl)
    assert "_preload_post_update_imports()" in zip_src
    assert "_preload_post_update_imports()" in git_src
    # And it must be near the top of each function body (before the tree
    # swap, which happens well after these early snapshot lines).
    for src, early_marker in (
        (zip_src, "_capture_active_tool_dependencies"),
        (git_src, "_read_project_version"),
    ):
        assert src.index("_preload_post_update_imports()") < src.index(
            early_marker
        ) or src.index("_preload_post_update_imports()") < 500, (
            "preload drifted away from the top of the update path"
        )
