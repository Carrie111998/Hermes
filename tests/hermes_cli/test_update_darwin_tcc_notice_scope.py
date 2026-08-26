"""Regression guard for #95309: the macOS TCC stale-grant notice must use an
in-scope variable.

``hermes update`` crashed at the very end of a *successful* macOS update with
``NameError: name 'has_desktop_app' is not defined``. The update itself
completed (``✓ Code updated!`` was printed) but the command then exited via
traceback, silently skipping every post-update step (state.db integrity check,
model-catalog cache seed, bundled-skills sync).

Root cause: ``has_desktop_app`` is a *local* of ``_rebuild_desktop_after_update``;
the macOS TCC-notice block in ``_cmd_update_impl`` referenced it out of scope.
The intended variable, ``had_desktop_app_before_update``, is a genuine local of
``_cmd_update_impl`` (and is exactly the flag the notice needs — "a desktop app
was present before the update, so its re-signed bundle may hold a stale grant").

These are bytecode-scope invariants rather than a full drive of the update
pipeline: the crashing line only runs after git-pull + web/desktop rebuild on
darwin, which is impractical to stub end-to-end, whereas the defect is purely a
name-resolution boundary that ``co_names`` / ``co_varnames`` pin exactly.
"""

import hermes_cli.update_cmd as update_mod


def test_cmd_update_impl_never_references_out_of_scope_has_desktop_app():
    """``has_desktop_app`` is defined only inside the rebuild helper. If it
    appears in ``_cmd_update_impl``'s ``co_names`` it is being looked up as a
    global/free name — an unavoidable ``NameError`` on the darwin path."""
    code = update_mod._cmd_update_impl.__code__
    assert "has_desktop_app" not in code.co_names, (
        "_cmd_update_impl references `has_desktop_app`, a local of "
        "_rebuild_desktop_after_update; use `had_desktop_app_before_update` "
        "(an actual local of _cmd_update_impl). See #95309."
    )


def test_cmd_update_impl_owns_the_intended_presence_flag():
    """The correct, in-scope flag must be a real local of the update command."""
    code = update_mod._cmd_update_impl.__code__
    assert "had_desktop_app_before_update" in code.co_varnames


def test_rebuild_helper_still_owns_has_desktop_app():
    """Sanity: ``has_desktop_app`` legitimately lives in the rebuild helper, so
    the guard above asserts a real scoping boundary, not a renamed symbol."""
    code = update_mod._rebuild_desktop_after_update.__code__
    assert "has_desktop_app" in code.co_varnames
