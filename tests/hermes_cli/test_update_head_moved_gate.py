"""Tests for the post-pull HEAD-movement gate in ``hermes update``.

Issue #79678: a detached/pinned checkout can report "N new commit(s)"
against origin, run the ff-only merge successfully, and still sit on the
old commit afterward (the branch-switch step re-detaches to the raw SHA).
Before this guard ``hermes update`` printed "✓ Code updated!" and
reinstalled deps + rebuilt the desktop app against the stale tree — no
error, no warning. The gate compares the pre-pull and post-pull HEAD SHA
and fails loudly when the update was a no-op.
"""

from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import update_cmd


def _make_head_moved_side_effect(pre_sha="abc123", post_sha="def456"):
    """Simulate git commands where HEAD advances from pre_sha to post_sha."""
    calls = {"n": 0}

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")

        # git rev-list HEAD..origin/main --count  (behind count)
        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

        # git rev-parse HEAD  — first call (pre-pull) returns pre_sha,
        # subsequent calls (post-pull) return post_sha.
        if joined.endswith("rev-parse HEAD"):
            if calls["n"] == 0:
                calls["n"] += 1
                return SimpleNamespace(returncode=0, stdout=f"{pre_sha}\n", stderr="")
            return SimpleNamespace(returncode=0, stdout=f"{post_sha}\n", stderr="")

        # Everything else (merge, checkout, etc.) succeeds quietly.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _make_head_pinned_side_effect(sha="abc123"):
    """Simulate a detached checkout pinned to ``sha``: HEAD never moves."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="HEAD\n", stderr="")

        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

        if joined.endswith("rev-parse HEAD"):
            return SimpleNamespace(returncode=0, stdout=f"{sha}\n", stderr="")

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _patch_update_deps(monkeypatch, tmp_path, run_side_effect):
    """Patch the hermes_cli.main helpers ``_cmd_update_impl`` touches.

    ``_m()`` in update_cmd.py lazily returns hermes_cli.main, so patching
    attributes on that module is the canonical test surface (matches
    tests/hermes_cli/test_cmd_update.py).
    """
    monkeypatch.setattr(hermes_main.subprocess, "run", run_side_effect)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()  # pass the "is a git repo" gate
    monkeypatch.setattr(
        hermes_main, "_resolve_update_branch", lambda args: "main"
    )
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
    monkeypatch.setattr(
        hermes_main, "_get_origin_url",
        lambda *a, **k: "https://github.com/NousResearch/hermes-agent.git",
    )
    monkeypatch.setattr(hermes_main, "_is_fork", lambda *a, **k: False)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", lambda *a, **k: 0)
    monkeypatch.setattr(
        hermes_main, "_record_bytecode_fingerprint", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_run_pre_update_backup", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_pause_windows_gateways_for_update", lambda: None
    )
    monkeypatch.setattr(
        hermes_main, "_resume_windows_gateways_after_update", lambda *a, **k: None
    )
    # Short-circuit the long tail: dependency install + desktop build.
    monkeypatch.setattr(hermes_main, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(hermes_main, "_clear_update_incomplete_marker", lambda: None)
    # Gateway restart path (called after a successful update).
    monkeypatch.setattr(hermes_main, "_finish_dashboard_update_cleanup", lambda *a: None)
    # Keep the (now surfaced — #78574) gateway auto-restart phase away from
    # this machine's real gateways: discovery returns nothing, systemd is
    # unsupported, so the phase is a clean no-op for both snapshots.
    import hermes_cli.gateway as hermes_gateway

    monkeypatch.setattr(
        hermes_gateway, "find_gateway_pids", lambda all_profiles=False: []
    )
    monkeypatch.setattr(
        hermes_gateway, "supports_systemd_services", lambda: False
    )
    monkeypatch.setattr(
        hermes_gateway, "find_profile_gateway_processes", lambda *a, **k: []
    )
    # Newer restart-phase seams (added after this file was written): without
    # these, a host with a live gateway leaks real PIDs into the run and the
    # conftest live-system guard blocks os.kill — same mocks the newer
    # test_cmd_update.py carries.
    monkeypatch.setattr(hermes_gateway, "_get_service_pids", lambda *a, **k: set())
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions", lambda *a, **k: []
    )
    # Pre-update plan inventory also scans the real host; an empty plan keeps
    # the fleet-expectation probe and plan-vs-execution reconciliation quiet.
    monkeypatch.setattr(
        "hermes_cli.update_inventory.collect_runtime_inventory",
        lambda: SimpleNamespace(runtimes=[]),
    )
    # Final hermeticity backstop: several post-update phases discover
    # processes via direct /proc scanning rather than find_gateway_pids.
    # Neutralise signalling entirely so a dev machine running a live
    # gateway can neither be signalled nor trip the conftest live-system
    # guard mid-test.
    import os as _os

    monkeypatch.setattr(_os, "kill", lambda pid, sig: None)


def test_update_success_when_head_moves(monkeypatch, tmp_path, capsys):
    """When the pull advances HEAD, the movement guard lets the update
    proceed through to the post-update pipeline."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    # Hermetic stop (same seam as test_cmd_update.py): reaching the
    # runtime-reload step proves the movement guard passed and the
    # post-update pipeline is running. Everything after it (web build,
    # desktop rebuild, skills sync, gateway restart, fleet check) acts on
    # the real host, so abort with a success exit instead.
    monkeypatch.setattr(
        hermes_main,
        "_reload_updated_runtime_modules",
        lambda *a, **k: (_ for _ in ()).throw(SystemExit(0)),
    )

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    # The pull ran and the guard did not mistake it for a no-op.
    assert "Pulling updates..." in out
    assert "Code did not move" not in out
    assert "Already up to date" not in out


def test_update_fails_loudly_when_head_pinned(monkeypatch, tmp_path, capsys):
    """A detached/pinned HEAD that never moves must fail loudly, not print
    '✓ Code updated!' against the stale tree."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    _patch_update_deps(monkeypatch, tmp_path, _make_head_pinned_side_effect())

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "Code did not move" in out
    assert "✓ Code updated!" not in out
    assert "checkout main" in out


def _make_fork_sync_moves_head_side_effect(
    sync_from="aaaaaaa", sync_to="bbbbbbb"
):
    """Simulate a clean fork whose upstream-sync stage advances HEAD.

    rev-parse HEAD call order inside ``_cmd_update_impl``:
      1. pre-sync capture   -> sync_from
      2. post-sync capture  -> sync_to     (the sync DID move HEAD)
      3. pre-pull capture   -> sync_to
      4. post-pull capture  -> sync_to     (the merge is a legit zero-move)

    The first ``rev-list --count`` (behind vs the compare branch) returns 0
    so the fork-sync block triggers; later counts return 3.
    """
    head_reads = {"n": 0}
    rev_lists = {"n": 0}

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")

        if "rev-list" in joined:
            rev_lists["n"] += 1
            count = "0" if rev_lists["n"] == 1 else "3"
            return SimpleNamespace(returncode=0, stdout=f"{count}\n", stderr="")

        if joined.endswith("rev-parse HEAD"):
            head_reads["n"] += 1
            if head_reads["n"] == 1:
                return SimpleNamespace(
                    returncode=0, stdout=f"{sync_from}\n", stderr=""
                )
            return SimpleNamespace(returncode=0, stdout=f"{sync_to}\n", stderr="")

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def test_fork_sync_advances_head_and_update_completes(monkeypatch, tmp_path, capsys):
    """Regression (live incident 2026-08-25): on a pristine fork whose main
    mirrors upstream, the fork-sync stage itself fast-forwards HEAD. The
    subsequent merge legitimately does not move it, but the #79678 guard saw
    identical SHAs, aborted with a bogus 'detached checkout' diagnosis, and
    skipped dependency sync, skills sync, and gateway restart. The guard
    must treat a sync-stage move as a completed update."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    _patch_update_deps(
        monkeypatch,
        tmp_path,
        _make_fork_sync_moves_head_side_effect(),
    )
    # ``is_fork`` resolves in update_cmd's own namespace (line 6049), not
    # through _m() — patch it there.
    monkeypatch.setattr(update_cmd, "_is_fork", lambda *a, **k: True)
    # The sync's internal git dance is irrelevant here; the mocked
    # subprocess.run above is what reports HEAD movement around it.
    monkeypatch.setattr(
        hermes_main, "_sync_with_upstream_if_needed", lambda *a, **k: None
    )
    # Hermetic stop: reaching the runtime-reload step IS the proof the
    # movement guard passed and the post-update pipeline is running (same
    # seam as test_cmd_update.py). Everything after it (web build, desktop
    # rebuild, skills sync, gateway restart, fleet check) would act on the
    # real host, so abort with a success exit before any of that.
    monkeypatch.setattr(
        hermes_main,
        "_reload_updated_runtime_modules",
        lambda *a, **k: (_ for _ in ()).throw(SystemExit(0)),
    )

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    # The exact point where the bug used to abort now reports success.
    assert "Code updated by the upstream fork sync" in out
    assert "Code did not move" not in out
    assert "Already up to date" not in out


def test_already_on_target_branch_with_no_move_is_benign(monkeypatch, tmp_path, capsys):
    """Same live incident, second variant: HEAD attached to main, already at
    origin/main, yet a stale behind-count sent the updater through the pull
    path. pre == post with an ATTACHED target branch means the code is
    current — not a stuck detached checkout. Must complete, not exit 1."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")
        if joined.endswith("rev-parse HEAD"):
            return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _patch_update_deps(monkeypatch, tmp_path, side_effect)
    monkeypatch.setattr(
        hermes_main,
        "_reload_updated_runtime_modules",
        lambda *a, **k: (_ for _ in ()).throw(SystemExit(0)),
    )

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "already at origin/main" in out
    assert "Code did not move" not in out
