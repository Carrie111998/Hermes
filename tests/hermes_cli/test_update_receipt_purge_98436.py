"""Regression tests for #98436 — update receipt lost when the stale-module
purge evicts hermes_cli.update_receipt mid-run.

Windows `hermes update` hand-off child: the pending fleet-restart catch-up
starts with _purge_stale_hermes_modules(), which evicted the cached
hermes_cli.update_receipt module (it was not in _STALE_PURGE_PROTECTED).
The singleton receipt lives in that module, so the later
`from hermes_cli.update_receipt import finalize_pending_update_receipt`
in cmd_update's command-boundary safety net re-imported a FRESH module
with `_current is None` -> finalize hit its documented no-op -> the
hand-off child (the process that does the real dependency sync) wrote NO
receipt.

Fix: protect hermes_cli.update_receipt from the purge. Accurate invariant
for the protected set here: the module holds LIVE SINGLETON STATE that the
currently-executing update depends on — evicting its cache entry strands
that state in an orphaned module object.
"""

from __future__ import annotations

import sys

import pytest

from hermes_cli import main as cli_main


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    snapshot = dict(sys.modules)
    yield
    for name, mod in snapshot.items():
        sys.modules[name] = mod
    for name in list(sys.modules):
        if name not in snapshot:
            del sys.modules[name]


def test_purge_protects_update_receipt_module():
    """#98436: the receipt singleton lives in hermes_cli.update_receipt.

    Evicting it mid-update silently drops the in-flight receipt: a later
    from-import rebuilds the module with `_current is None` and finalize
    becomes a no-op, so the run that did the real work leaves no receipt.
    """
    import hermes_cli.update_receipt as update_receipt

    cli_main._purge_stale_hermes_modules()

    assert sys.modules.get("hermes_cli.update_receipt") is update_receipt, (
        "hermes_cli.update_receipt was purged — the in-flight receipt "
        "singleton dies with it (#98436)"
    )


def test_boundary_from_import_finalizes_after_purge(tmp_path, monkeypatch):
    """Drive the EXACT failure shape from cmd_update's boundary safety net:

        begin -> purge -> `from hermes_cli.update_receipt import
        finalize_pending_update_receipt` -> call it

    asserting the receipt is actually written. On main this is RED: the
    purge evicts the module, the from-import rebuilds it with
    `_current is None`, and finalize returns None (documented no-op).
    """
    import hermes_cli.update_receipt as update_receipt

    monkeypatch.setattr(
        update_receipt, "_receipt_dir", lambda: tmp_path / "update_receipts"
    )

    update_receipt.begin_update_receipt()
    assert update_receipt._current is not None, "precondition: receipt open"

    # This is what the fleet-restart catch-up does before its work:
    cli_main._purge_stale_hermes_modules()

    # And this is cmd_update's command-boundary safety net, verbatim in
    # import form — a from-import AFTER the purge, exactly where the real
    # failure happened:
    from hermes_cli.update_receipt import finalize_pending_update_receipt

    path = finalize_pending_update_receipt(exit_code=0, stop_reason="test boundary")
    assert path is not None, (
        "#98436: receipt silently lost across the purge — the boundary "
        "from-import rebuilt a fresh module with no open receipt"
    )
    assert path.exists()
