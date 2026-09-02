"""Regression tests for the update-path TCC anchor pre-heal (#95759).

The revert of the TCC interpreter anchor (`2f9e187001`) kept a heal in
``hermes doctor`` (``check_macos_tcc_anchor_removed``), but the desktop
update handoff runs ``hermes update`` — never doctor — so pre-anchored
venvs bricked every update attempt with "No module named 'encodings'" and
the desktop retried forever. These pins keep the heal wired into
``cmd_update``'s apply path.
"""

from __future__ import annotations

import inspect
import sys
from unittest.mock import patch

import hermes_cli.main as main_mod


def test_cmd_update_apply_path_calls_the_heal_on_macos():
    """The apply path invokes the doctor heal before any stage runs."""
    source = inspect.getsource(main_mod.cmd_update)
    assert "check_macos_tcc_anchor_removed" in source, (
        "cmd_update must call the TCC anchor heal (#95759)"
    )
    # The call must precede the apply-path stages (gateway_mode marks the
    # start of the stage machinery after the --check/--plan early returns).
    heal_idx = source.index("check_macos_tcc_anchor_removed()")
    gate_idx = source.index("gateway_mode = getattr(args")
    assert heal_idx < gate_idx


def test_heal_failure_never_blocks_the_update(monkeypatch):
    """A crashing heal is swallowed — the update itself must still run."""
    calls = {}

    def boom():
        calls["healed"] = True
        raise RuntimeError("marker unreadable")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "hermes_cli.doctor.check_macos_tcc_anchor_removed", boom
    )
    # Drive the wiring through the real cmd_update entry with stubbed
    # downstream stages: an exception from the heal must not propagate.
    class _Args:
        plan = False
        check = False
        gateway = False
        yes = True
        branch = None

    reached_impl = {"v": False}

    class _Impl:
        def _cmd_update_impl(self, args, gateway_mode=False):
            reached_impl["v"] = True

    monkeypatch.setattr(main_mod, "_self", lambda: _Impl())
    import hermes_cli.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "detect_install_method", lambda root: "git")
    monkeypatch.setattr(cfg_mod, "is_managed", lambda: False)
    monkeypatch.setattr(cfg_mod, "is_nix_install_method", lambda m: False)
    monkeypatch.setattr(main_mod, "_install_hangup_protection", lambda gateway_mode=None: None)
    monkeypatch.setattr(main_mod, "_finalize_update_output", lambda state=None: None)

    class _AlwaysAcquire:
        def acquire(self):
            return True

        def release(self):
            return None

        holder = None

    import hermes_cli.update_lock as ul
    monkeypatch.setattr(ul, "UpdateLock", _AlwaysAcquire)
    monkeypatch.setattr(ul, "describe_holder", lambda h: "")

    main_mod.cmd_update(_Args())

    assert calls["healed"] is True
    assert reached_impl["v"] is True, "update must proceed past a failed heal"
