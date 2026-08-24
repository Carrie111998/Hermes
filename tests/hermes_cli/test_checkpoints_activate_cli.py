"""E2E: the real operator path for atomic store activation (#93314).

Exercises ``hermes checkpoints activate`` / ``deactivate`` through the real
argparse wiring (register_cli) — not by calling cmd_* directly — plus a full
checkpoint → activate → checkpoint → deactivate cycle against real git
stores in a temp HERMES_HOME.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.checkpoint_manager import (
    CheckpointManager,
    _init_store,
    _read_store_selector,
    active_generation_name,
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    base = tmp_path / "home" / ".hermes" / "checkpoints"
    work = tmp_path / "project"
    work.mkdir(parents=True)
    (work / "app.py").write_text("print('v1')\n")
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)
    return base, work


def _run_cli(argv):
    """Invoke `hermes checkpoints ...` through the real parser wiring."""
    import argparse

    import hermes_cli.checkpoints as checkpoints_cli

    parser = argparse.ArgumentParser(prog="hermes checkpoints")
    checkpoints_cli.register_cli(parser)
    ns = parser.parse_args(argv)
    return ns.func(ns)


@pytest.mark.e2e
class TestActivateCliEndToEnd:
    def test_full_recovery_cycle_through_cli(self, env, monkeypatch):
        base, work = env

        # 1. Healthy life before the incident: one real checkpoint.
        mgr = CheckpointManager(enabled=True)
        assert mgr.ensure_checkpoint(str(work), "before") is True
        assert (base / "store" / "HEAD").exists()

        # 2. Offline "repair": candidate copy outside the base.
        candidate = work.parent / "store.repaired"
        shutil.copytree(base / "store", candidate)

        # 3. Operator activates it via the CLI. No directory swapping.
        rc = _run_cli(["activate", str(candidate)])
        assert rc == 0
        gen = active_generation_name(base)
        assert gen and gen.startswith("store.")
        # Legacy store untouched; generation is a sibling copy.
        assert (base / "store" / "HEAD").exists()
        assert (base / gen).is_dir() and not (base / gen).is_symlink()

        # 4. Hermes keeps checkpointing — into the activated generation.
        mgr.new_turn()
        (work / "app.py").write_text("print('v2')\n")
        assert mgr.ensure_checkpoint(str(work), "after") is True
        entries = mgr.list_checkpoints(str(work))
        assert [e["reason"] for e in entries][:1] == ["after"]

        # 5. Status surfaces the layout for the operator.
        from tools.checkpoint_manager import store_status

        info = store_status(checkpoint_base=base)
        assert info["active_generation"] == gen
        assert any(g["name"] == gen and g["is_active"]
                   for g in info["generations"])

        # 6. Rollback to legacy via CLI. Each store keeps its OWN history:
        # commits made while the generation was active live in the
        # generation; the legacy store still has its pre-repair history.
        rc = _run_cli(["deactivate", "--force"])
        assert rc == 0
        assert _read_store_selector(base) is None
        after = mgr.list_checkpoints(str(work))
        reasons = [e["reason"] for e in after]
        assert "before" in reasons and "after" not in reasons

    def test_activate_rejects_broken_candidate_with_clear_error(
        self, env, capsys,
    ):
        base, work = env
        broken = work.parent / "broken"
        broken.mkdir()
        rc = _run_cli(["activate", str(broken)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "Activation failed" in out
        assert not list(base.glob("store.*"))

    def test_deactivate_without_pointer_is_a_clean_noop(self, env, capsys):
        base, work = env
        rc = _run_cli(["deactivate"])
        assert rc == 0
        assert "Nothing to deactivate" in capsys.readouterr().out
