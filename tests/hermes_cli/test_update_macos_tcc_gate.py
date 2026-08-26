"""Regression guard: the macOS TCC stale-grant notice gate must reference
only names resolvable in ``_cmd_update_impl``'s scope (#95309).

Commit 36c1755065 added the TCC stale-grant notice to the post-update path
of ``_cmd_update_impl`` but gated it on ``has_desktop_app`` — a local of
``_rebuild_desktop_after_update``, never defined in ``_cmd_update_impl``'s
scope. On macOS every ``hermes update`` crashed with
``NameError: name 'has_desktop_app' is not defined`` right after printing
``✓ Code updated!``, skipping the remaining post-update steps (state.db
integrity check, model-catalog seed, bundled-skills sync).

The guard asserts the runtime-resolution invariant rather than the exact
variable name, so it survives a future rename: whatever identifier gates
the darwin TCC notice must be a local/parameter of ``_cmd_update_impl``,
a module global, or a builtin — otherwise the update crashes with
NameError at runtime.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import re
import types

import hermes_cli.update_cmd as update_cmd

_DARWIN_TCC_GATE = re.compile(r'if sys\.platform == "darwin" and (\w+):')


def _gate_name() -> str:
    """Return the identifier gating the darwin TCC stale-grant notice."""
    src = inspect.getsource(update_cmd._cmd_update_impl)
    assert "tccutil reset ScreenCapture" in src, "TCC stale-grant notice missing"
    m = _DARWIN_TCC_GATE.search(src)
    assert m, "darwin TCC notice gate not found in _cmd_update_impl"
    return m.group(1)


def _resolves_in_update_scope(name: str) -> bool:
    """True if ``name`` resolves at runtime where the gate executes.

    Mirrors CPython's own resolution for the function body: a name is
    local/parameter (in the function's ``co_varnames``), or looked up as
    a global/builtin (``update_cmd`` module namespace or builtins). A name
    defined only in another function (the #95309 bug) matches neither —
    it would raise NameError exactly like the real runtime did.
    """
    func_src = inspect.getsource(update_cmd._cmd_update_impl)
    code = compile(ast.parse(func_src), "<update-scope-check>", "exec")
    fcode = next(
        c
        for c in code.co_consts
        if isinstance(c, types.CodeType) and c.co_name == "_cmd_update_impl"
    )
    if name in fcode.co_varnames:
        return True
    return name in dir(update_cmd) or name in dir(builtins)


def test_tcc_notice_gate_references_resolvable_name():
    """FAIL-BEFORE: gate used ``has_desktop_app`` (a local of
    ``_rebuild_desktop_after_update``) → NameError on every macOS update.
    """
    name = _gate_name()
    assert _resolves_in_update_scope(name), (
        f"TCC notice gate references {name!r}, which is not a local of "
        "_cmd_update_impl and not a module global/builtin — update would "
        "crash with NameError on macOS (#95309)"
    )
