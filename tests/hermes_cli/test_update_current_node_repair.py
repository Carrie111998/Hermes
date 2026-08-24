"""The commit_count == 0 path must repair Node deps, not just Python (#77211).

A previous ``hermes update`` whose npm install failed printed "Fix npm and
re-run `hermes update`" — but re-running hit the "Already up to date!" early
return before the Node refresh, so the advice could never work. The repair
now runs through ``_repair_node_deps_on_current_checkout``, which delegates
to ``_update_node_dependencies`` (self-gating on the lockfile hash, recorded
only after a successful install, so healthy installs stay a cheap no-op).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from hermes_cli import update_cmd


def test_update_web_build_is_fatal_and_receipt_visible(tmp_path, monkeypatch):
    build = MagicMock(return_value=False)
    record_step = MagicMock()
    monkeypatch.setattr(
        update_cmd,
        "_m",
        lambda: SimpleNamespace(PROJECT_ROOT=tmp_path, _build_web_ui=build),
    )

    with patch("hermes_cli.update_receipt.record_step", record_step):
        assert update_cmd._build_web_ui_for_update() is False

    build.assert_called_once_with(
        tmp_path / "web", fatal=True, require_fresh=True
    )
    record_step.assert_called_once_with(
        "web_ui_build", False, "dashboard bundle rebuild failed"
    )


def test_current_checkout_repairs_failed_node_deps(capsys):
    """A recorded failure surfaces the fix-npm hint, not 'Already up to date!'."""
    completion = MagicMock()
    with (
        patch.object(
            update_cmd,
            "_update_node_dependencies",
            return_value=["ui-tui, web workspaces"],
        ),
        patch.object(update_cmd, "_build_web_ui_for_update") as build_web,
    ):
        assert update_cmd._repair_node_deps_on_current_checkout(completion) is False

    build_web.assert_not_called()
    completion.assert_called_once()
    assert "could not be repaired" in completion.call_args[0][0]
    out = capsys.readouterr().out
    assert "Node.js refresh failed for: ui-tui, web workspaces" in out
    assert "Fix npm and re-run `hermes update`." in out


def test_current_checkout_healthy_node_deps_reports_up_to_date():
    """A clean refresh (or lockfile-hash no-op) still says 'Already up to date!'."""
    completion = MagicMock()
    with (
        patch.object(update_cmd, "_update_node_dependencies", return_value=[]),
        patch.object(
            update_cmd, "_build_web_ui_for_update", return_value=True
        ) as build_web,
    ):
        assert update_cmd._repair_node_deps_on_current_checkout(completion) is True

    # The refresh pairs with the web build like every other call site.
    build_web.assert_called_once_with()
    completion.assert_called_once_with("✓ Already up to date!")


def test_current_checkout_reports_failed_web_bundle_repair():
    completion = MagicMock()
    with (
        patch.object(update_cmd, "_update_node_dependencies", return_value=[]),
        patch.object(update_cmd, "_build_web_ui_for_update", return_value=False),
    ):
        assert update_cmd._repair_node_deps_on_current_checkout(completion) is False

    completion.assert_called_once_with(
        "⚠ Checkout is current, but the web dashboard bundle could not be repaired."
    )
