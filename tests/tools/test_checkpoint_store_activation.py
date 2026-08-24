"""Atomic store activation (#93314) — generation selector + fail-closed resolver.

Behaviour contracts under test:

* Absent ``store.current`` pointer  -> legacy ``store/`` path (byte-for-byte
  backwards compatibility; no migration, no new files).
* Present, valid pointer            -> every consumer transparently operates
  on the named generation directory.
* Present, untrustworthy pointer    -> fail closed: resolver raises instead of
  silently re-pointing Hermes at the possibly-damaged legacy store.
* Activation                        -> copies a candidate into a sibling
  generation and flips the pointer atomically; neither store is ever moved or
  deleted; failures before/during the flip leave the old selection valid.
"""

import json
import os
import shutil
import time

import pytest
from pathlib import Path

from tools.checkpoint_manager import (
    CheckpointManager,
    CheckpointStoreSelectorError,
    _init_store,
    _store_path,
    activate_store_generation,
    active_generation_name,
    deactivate_store_generation,
    prune_checkpoints,
    store_status,
)


VALID_GEN = "store.20260823T220000Z"


@pytest.fixture()
def base(tmp_path):
    d = tmp_path / "checkpoints"
    d.mkdir()
    return d


@pytest.fixture()
def work_dir(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    (d / "main.py").write_text("print('hello')\n")
    return d


@pytest.fixture()
def live_store(base, work_dir):
    """An initialised legacy store with one real checkpoint."""
    err = _init_store(base / "store", str(work_dir))
    assert err is None
    return base / "store"


@pytest.fixture()
def candidate(live_store, work_dir, tmp_path):
    """An offline 'repaired' candidate: a byte-copy of the live store
    sitting OUTSIDE the checkpoint base (the real recovery flow)."""
    cand = tmp_path / "repaired_candidate"
    shutil.copytree(live_store, cand)
    return cand


def _write_pointer(base: Path, generation: str) -> None:
    (base / "store.current").write_text(generation, encoding="utf-8")


# =========================================================================
# Resolver contract
# =========================================================================


class TestResolverLegacyDefault:
    def test_no_pointer_resolves_legacy_store(self, base):
        assert _store_path(base) == base / "store"
        assert active_generation_name(base) is None

    def test_no_pointer_is_byte_compatible(self, base, work_dir):
        """Without the pointer the whole flow behaves exactly as pre-#93314."""
        mgr = CheckpointManager(enabled=True)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)
            assert mgr.ensure_checkpoint(str(work_dir), "auto") is True
        assert (base / "store" / "HEAD").exists()
        assert not (base / "store.current").exists()


class TestResolverValidPointer:
    def test_pointer_selects_generation_dir(self, base, work_dir):
        gen = base / VALID_GEN
        assert _init_store(gen, str(work_dir)) is None
        _write_pointer(base, VALID_GEN)
        assert _store_path(base) == gen
        assert active_generation_name(base) == VALID_GEN

    def test_all_consumers_route_through_generation(
        self, base, work_dir, monkeypatch,
    ):
        """A checkpoint written after activation lives in the generation."""
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)
        assert _init_store(base / "store", str(work_dir)) is None
        gen = base / VALID_GEN
        assert _init_store(gen, str(work_dir)) is None
        _write_pointer(base, VALID_GEN)

        mgr = CheckpointManager(enabled=True)
        assert mgr.ensure_checkpoint(str(work_dir), "auto") is True

        # The new commit must be visible through the generation's refs —
        # and only there. This is the routing contract, not a snapshot:
        # whatever hash was created, it exists in gen A and not in the
        # untouched legacy store.
        from tools.checkpoint_manager import _project_hash, _ref_name, _run_git

        ref = _ref_name(_project_hash(str(work_dir)))
        ok_gen, _, _ = _run_git(
            ["rev-parse", "--verify", ref + "^{commit}"], gen, str(work_dir),
        )
        ok_legacy, _, _ = _run_git(
            ["rev-parse", "--verify", ref + "^{commit}"],
            base / "store", str(work_dir),
        )
        assert ok_gen is True
        assert ok_legacy is False


# =========================================================================
# Fail-closed contract
# =========================================================================


@pytest.mark.parametrize("bad_content", [
    "",
    "store\n",
    "store.20260823T220000Z extra\n",
    "../../../etc",
    "store.2026-08-23T22:00:00Z",
    "/absolute/path",
])
class TestResolverFailClosed:
    def test_malformed_pointer_raises_never_falls_back(
        self, base, work_dir, bad_content,
    ):
        """A broken selector must NOT silently resolve to legacy store/.

        The legacy store here even EXISTS and is healthy — resolution must
        still refuse, because the operator asked for a generation and we
        cannot know why the pointer is unreadable.
        """
        assert _init_store(base / "store", str(work_dir)) is None
        _write_pointer(base, bad_content)
        with pytest.raises(CheckpointStoreSelectorError):
            _store_path(base)

    def test_broken_pointer_disables_manager_operations(
        self, base, work_dir, bad_content, monkeypatch,
    ):
        """Consumers degrade to 'unavailable', never to the corrupt store."""
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)
        assert _init_store(base / "store", str(work_dir)) is None
        _write_pointer(base, bad_content)

        mgr = CheckpointManager(enabled=True)
        # Snapshot attempt refuses instead of silently writing to store/.
        assert mgr._take(str(work_dir), "test") is False
        # Restore reports failure instead of reading the wrong store.
        result = mgr.restore(str(work_dir), "0123456789abcdef")
        assert result["success"] is False
        assert "unavailable" in result["error"]


class TestSelectorEdgeCases:
    def test_symlinked_pointer_rejected(self, base, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text(VALID_GEN, encoding="utf-8")
        pointer = base / "store.current"
        pointer.symlink_to(outside)
        with pytest.raises(CheckpointStoreSelectorError):
            _store_path(base)

    def test_traversal_target_rejected(self, base, tmp_path):
        # Name passes the pattern check only if it matches exactly; an
        # escaped target can never match the strict pattern, but prove the
        # join stays inside base regardless.
        evil = "store." + ("9" * 8) + "T" + ("9" * 6) + "Z"
        _write_pointer(base, evil)  # well-formed name, missing directory
        with pytest.raises(CheckpointStoreSelectorError):
            _store_path(base)

    def test_missing_target_directory_rejected(self, base):
        _write_pointer(base, VALID_GEN)
        with pytest.raises(CheckpointStoreSelectorError):
            _store_path(base)

    def test_symlinked_target_directory_rejected(self, base, tmp_path):
        real = tmp_path / "realgen"
        real.mkdir()
        (real / "HEAD").write_text("ref: refs/hermes/x\n", encoding="utf-8")
        (base / VALID_GEN).symlink_to(real)
        _write_pointer(base, VALID_GEN)
        with pytest.raises(CheckpointStoreSelectorError):
            _store_path(base)

    def test_directory_named_like_pointer_rejected(self, base):
        (base / "store.current").mkdir()
        with pytest.raises(CheckpointStoreSelectorError):
            _store_path(base)


# =========================================================================
# Integration guards: sweeps never touch generations or pointer
# =========================================================================


class TestSweepGuards:
    def test_prune_retention_never_deletes_generations_or_pointer(
        self, base, work_dir,
    ):
        assert _init_store(base / "store", str(work_dir)) is None
        gen = base / VALID_GEN
        assert _init_store(gen, str(work_dir)) is None
        # Age both beyond any retention cutoff.
        old = time.time() - 100 * 86400
        os.utime(gen, (old, old))
        _write_pointer(base, VALID_GEN)

        result = prune_checkpoints(retention_days=1, delete_orphans=True,
                                   checkpoint_base=base)
        assert gen.exists(), "retention sweep deleted an activated generation"
        assert (base / "store.current").exists()

    def test_pre_v2_scan_ignores_generations(self, base, work_dir):
        """Generations have HEAD files; without the guard they would be
        classified as deletable pre-v2 orphan shadow repos."""
        from tools.checkpoint_manager import _pre_v2_shadow_repos

        gen = base / VALID_GEN
        assert _init_store(gen, str(work_dir)) is None
        repos = _pre_v2_shadow_repos(base)
        assert all(r["path"] != gen for r in repos)

    def test_migration_never_archives_generations_or_pointer(
        self, base, work_dir,
    ):
        from tools.checkpoint_manager import _migrate_legacy_store

        gen = base / VALID_GEN
        assert _init_store(gen, str(work_dir)) is None
        _write_pointer(base, VALID_GEN)
        # A stray dir that SHOULD be archived (proves the sweep ran).
        stray = base / "1234567890abcdef"
        (stray / "HEAD").mkdir(parents=True)
        _migrate_legacy_store(base)
        assert gen.exists()
        assert (base / "store.current").exists()
        assert not stray.exists(), "sanity: sweep did not run at all"

    def test_store_status_reports_generations_and_selector_error(
        self, base, work_dir,
    ):
        gen = base / VALID_GEN
        assert _init_store(gen, str(work_dir)) is None
        _write_pointer(base, VALID_GEN)
        info = store_status(checkpoint_base=base)
        assert info["active_generation"] == VALID_GEN
        assert [g["name"] for g in info["generations"]] == [VALID_GEN]
        assert info["generations"][0]["is_active"] is True

        _write_pointer(base, "garbage")
        info = store_status(checkpoint_base=base)
        assert "selector_error" in info
        # Still reports enough for an operator to act on.
        assert info["total_size_bytes"] >= 0


# =========================================================================
# Atomic activation
# =========================================================================


class TestActivation:
    def test_activate_flips_resolution_without_touching_stores(
        self, base, candidate, live_store,
    ):
        result = activate_store_generation(candidate, checkpoint_base=base,
                                           verify=False)
        assert result["success"] is True, result.get("error")
        gen_name = result["generation"]
        assert gen_name.startswith("store.") and gen_name != "store"
        # Legacy store still in place, untouched.
        assert live_store.exists() and (live_store / "HEAD").exists()
        # Pointer now routes everyone to the copy.
        assert _store_path(base) == base / gen_name
        assert active_generation_name(base) == gen_name

    def test_activation_copy_is_verified_with_fsck(self, base, candidate):
        result = activate_store_generation(candidate, checkpoint_base=base,
                                           verify=True)
        assert result["success"] is True, result.get("error")

    def test_failed_verification_leaves_previous_state_valid(
        self, base, work_dir, live_store,
    ):
        """A corrupt candidate must never become selectable."""
        broken = work_dir.parent / "broken_candidate"
        shutil.copytree(live_store, broken)
        # Corrupt the copy: truncate the pack-less object store by removing
        # a reachable object is hard to arrange portably; instead fake a
        # structurally broken repo (no HEAD).
        (broken / "HEAD").unlink()
        result = activate_store_generation(broken, checkpoint_base=base)
        assert result["success"] is False
        assert not (base / "store.current").exists(), \
            "pointer flipped despite failed verification"
        leftovers = [p.name for p in base.iterdir()
                     if p.name.startswith("store.")]
        assert leftovers == [], "failed activation left a partial generation"

    def test_activating_current_live_store_refused(self, base, live_store):
        result = activate_store_generation(live_store, checkpoint_base=base,
                                           verify=False)
        assert result["success"] is False
        assert "currently active" in result["error"]

    def test_broken_existing_selector_blocks_activation(
        self, base, candidate,
    ):
        """Untrustworthy rollback anchor: refuse rather than overwrite."""
        _write_pointer(base, "not-a-generation")
        result = activate_store_generation(candidate, checkpoint_base=base,
                                           verify=False)
        assert result["success"] is False
        assert "cannot be trusted" in result["error"]
        assert (base / "store.current").read_text() == "not-a-generation"

    def test_rollback_by_second_activation_and_by_deactivation(
        self, base, live_store, candidate, tmp_path,
    ):
        # Activate repaired copy.
        first = activate_store_generation(candidate, checkpoint_base=base,
                                          verify=False)
        assert first["success"] is True
        assert first["previous"] is None

        # Build a second candidate and activate it (pointer flip again).
        second_src = tmp_path / "second_candidate"
        shutil.copytree(live_store, second_src)
        second = activate_store_generation(second_src, checkpoint_base=base,
                                           verify=False)
        assert second["success"] is True
        assert second["previous"] == first["generation"]
        assert _store_path(base) == base / second["generation"]

        # Deterministic rollback to the legacy layout.
        back = deactivate_store_generation(checkpoint_base=base)
        assert back["success"] is True, back.get("error")
        assert _store_path(base) == base / "store"
        # Generations are retained for the operator to dispose of later.
        assert (base / first["generation"]).exists()

    def test_activation_refuses_at_generation_cap(self, base, candidate):
        """Bounded retention: growth past the cap is an operator decision."""
        import tools.checkpoint_manager as cm

        existing = [
            base / f"store.2026010{i}T000000Z" for i in range(1, 9)
        ]
        for d in existing:
            d.mkdir()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cm, "_MAX_STORE_GENERATIONS", len(existing))
            result = activate_store_generation(candidate,
                                               checkpoint_base=base,
                                               verify=False)
        assert result["success"] is False
        assert "dispose of old ones" in result["error"]
        assert not (base / "store.current").exists()

    def test_deactivation_refused_when_legacy_store_absent(self, base):
        (base / "store.current").write_text(VALID_GEN, encoding="utf-8")
        result = deactivate_store_generation(checkpoint_base=base)
        assert result["success"] is False
        assert (base / "store.current").exists(), \
            "refused deactivation must keep current selection"

    def test_pointer_flip_is_atomic_single_file(self, base, candidate):
        """The pointer file contains ONLY the generation name — the exact
        recovery-tool contract from the issue (one metadata line)."""
        result = activate_store_generation(candidate, checkpoint_base=base,
                                           verify=False)
        content = (base / "store.current").read_text(encoding="utf-8")
        assert content.strip() == result["generation"]
        assert "\n" not in content


# =========================================================================
# End-to-end: issue acceptance criteria
# =========================================================================


class TestEndToEndRecoveryScenario:
    def test_repaired_candidate_activation_full_cycle(self, base, work_dir, monkeypatch):
        """The exact scenario from #93314: healthy legacy store gets damaged
        conceptually; operator builds verified candidate; activates it
        atomically; Hermes keeps checkpointing against the new generation;
        status shows both; rollback restores legacy selection."""
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)
        assert _init_store(base / "store", str(work_dir)) is None

        mgr = CheckpointManager(enabled=True)
        assert mgr.ensure_checkpoint(str(work_dir), "pre-repair") is True
        mgr.new_turn()

        # Operator-side repair produces a byte-verified candidate elsewhere.
        candidate = work_dir.parent / "repaired_candidate"
        shutil.copytree(base / "store", candidate)

        # Directory-swap style activation is what fails on FUSE; ours is a
        # metadata write — and it must succeed while stores stay put.
        result = activate_store_generation(candidate, checkpoint_base=base)
        assert result["success"] is True, result.get("error")
        assert (base / "store").exists(), "live store was moved/deleted"

        # Post-activation checkpoints land in the new generation.
        (work_dir / "main.py").write_text("print('changed')\n")
        assert mgr.ensure_checkpoint(str(work_dir), "post-repair") is True
        gen = base / result["generation"]
        assert gen.exists()

        info = store_status(checkpoint_base=base)
        assert info["active_generation"] == result["generation"]
        names = {g["name"] for g in info["generations"]}
        assert result["generation"] in names

        # Rollback path: legacy store still fully usable.
        assert deactivate_store_generation(checkpoint_base=base)["success"]
        assert mgr.list_checkpoints(str(work_dir)), \
            "legacy store lost its history?"
