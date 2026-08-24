from pathlib import Path
from subprocess import CalledProcessError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import config as hermes_config
from hermes_cli import main as hermes_main


# ---------------------------------------------------------------------------
# Managed-uv compatibility for tests that patch shutil.which
# ---------------------------------------------------------------------------
# The production code now uses ``ensure_uv()`` / ``update_managed_uv()``
# instead of ``shutil.which("uv")``.  Many tests in this file patch
# ``shutil.which`` to control whether uv is "available" — these autouse
# fixtures make the managed_uv functions delegate to the patched
# ``shutil.which`` so the existing test setup keeps working without
# per-test changes.
@pytest.fixture(autouse=True)
def _patch_managed_uv(request):
    """Make managed_uv helpers follow shutil.which mocking in tests."""
    import shutil

    # resolve_uv delegates to shutil.which("uv") so that test patches
    # on shutil.which flow through naturally.
    def _fake_resolve_uv(**kwargs):
        return shutil.which("uv")

    def _fake_ensure_uv(**kwargs):
        return shutil.which("uv")

    def _fake_update_managed_uv(**kwargs):
        return None  # never actually self-update in tests

    with patch("hermes_cli.managed_uv.resolve_uv", side_effect=_fake_resolve_uv), \
         patch("hermes_cli.managed_uv.ensure_uv", side_effect=_fake_ensure_uv), \
         patch("hermes_cli.managed_uv.update_managed_uv", side_effect=_fake_update_managed_uv):
        yield













# ---------------------------------------------------------------------------
# Update uses .[all] with fallback to .
# ---------------------------------------------------------------------------

def _setup_update_mocks(monkeypatch, tmp_path):
    """Common setup for cmd_update tests."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hermes_main, "_stash_local_changes_if_needed", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_main, "_restore_stashed_changes", lambda *a, **kw: True)
    monkeypatch.setattr(hermes_config, "get_missing_env_vars", lambda required_only=True: [])
    monkeypatch.setattr(hermes_config, "get_missing_config_fields", lambda: [])
    monkeypatch.setattr(hermes_config, "check_config_version", lambda: (5, 5))
    monkeypatch.setattr(hermes_config, "migrate_config", lambda **kw: {"env_added": [], "config_added": []})
    monkeypatch.setattr(hermes_main, "_upgrade_pip_before_lazy_refresh", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_main, "_refresh_active_lazy_features", lambda *a, **kw: True)
    # This suite stubs the gateway inventory below. A simulated successful
    # pull must not purge that stub from sys.modules and rediscover/terminate
    # a real gateway on the test host.
    monkeypatch.setattr(hermes_main, "_purge_stale_hermes_modules", lambda: None)




def test_refresh_active_memory_provider_dependencies_reinstalls_active_provider(monkeypatch):
    """#53272/#70636: update must re-run the active provider's dep install."""
    recorded = []

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": "mem0"}},
    )
    monkeypatch.setattr(
        "hermes_cli.memory_setup._install_dependencies",
        lambda provider_name, force=False: recorded.append((provider_name, force)),
    )

    hermes_main._refresh_active_memory_provider_dependencies()

    assert recorded == [("mem0", True)]




def test_reload_updated_runtime_modules_restores_new_hermes_constants_symbol(monkeypatch):
    """A pre-pull module object missing a new helper is repaired by reload."""
    import hermes_constants

    monkeypatch.delattr(hermes_constants, "apply_subprocess_home_env", raising=False)
    assert not hasattr(hermes_constants, "apply_subprocess_home_env")

    hermes_main._reload_updated_runtime_modules()

    assert callable(hermes_constants.apply_subprocess_home_env)






# ---------------------------------------------------------------------------
# ff-only fallback to reset --hard on diverged history
# ---------------------------------------------------------------------------

def _make_update_side_effect(
    current_branch="main",
    commit_count="3",
    ff_only_fails=False,
    reset_fails=False,
    fetch_fails=False,
    fetch_stderr="",
):
    """Build a subprocess.run side_effect for cmd_update tests."""
    recorded = []
    old_sha = "a" * 40
    new_sha = "b" * 40
    state = {"head": old_sha, "tracking": old_sha, "dirty": False}

    def side_effect(cmd, **kwargs):
        recorded.append(cmd)
        joined = " ".join(str(c) for c in cmd)
        if "check-ref-format --branch" in joined:
            return SimpleNamespace(stdout=f"{cmd[-1]}\n", stderr="", returncode=0)
        if "fetch" in joined and "origin" in joined:
            if fetch_fails:
                return SimpleNamespace(stdout="", stderr=fetch_stderr, returncode=128)
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "branch --show-current" in joined:
            return SimpleNamespace(stdout=f"{current_branch}\n", stderr="", returncode=0)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(stdout=f"{current_branch}\n", stderr="", returncode=0)
        if "rev-parse" in joined and "--is-shallow-repository" in joined:
            return SimpleNamespace(stdout="false\n", stderr="", returncode=0)
        if "rev-parse" in joined and "--verify" in joined:
            ref = str(cmd[-1]).removesuffix("^{commit}")
            if ref == "HEAD":
                sha = state["head"]
            elif ref.startswith("refs/hermes-update-fetches/"):
                sha = new_sha
            elif ref == "refs/remotes/origin/main":
                sha = state["tracking"]
            elif ref == "refs/heads/main":
                sha = state["head"]
            elif ref == "MERGE_HEAD":
                return SimpleNamespace(stdout="", stderr="", returncode=1)
            else:
                sha = old_sha
            return SimpleNamespace(stdout=f"{sha}\n", stderr="", returncode=0)
        if "rev-parse" in joined and str(cmd[-1]) == "HEAD":
            return SimpleNamespace(stdout=f"{state['head']}\n", stderr="", returncode=0)
        if "merge-base --is-ancestor" in joined:
            ancestor, descendant = str(cmd[-2]), str(cmd[-1])
            if ff_only_fails and ancestor == old_sha and descendant == new_sha:
                rc = 1  # confirmed remote rewrite for reset-failure tests
            elif ancestor == new_sha and descendant == old_sha:
                rc = 1
            else:
                rc = 0
            return SimpleNamespace(stdout="", stderr="", returncode=rc)
        if "update-ref refs/remotes/origin/main" in joined:
            state["tracking"] = new_sha
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "status --porcelain --untracked-files=no" in joined:
            output = " M concurrent.txt\n" if state["dirty"] else ""
            return SimpleNamespace(stdout=output, stderr="", returncode=0)
        if "checkout" in joined and "main" in joined:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "rev-list" in joined:
            return SimpleNamespace(stdout=f"{commit_count}\n", stderr="", returncode=0)
        if "--ff-only" in joined:
            if ff_only_fails:
                return SimpleNamespace(
                    stdout="",
                    stderr="fatal: Not possible to fast-forward, aborting.\n",
                    returncode=128,
                )
            state["head"] = new_sha
            return SimpleNamespace(stdout="Updating abc..def\n", stderr="", returncode=0)
        if "reset" in joined and "--keep" in joined:
            if reset_fails:
                state["dirty"] = True
                return SimpleNamespace(stdout="", stderr="error: unable to write\n", returncode=1)
            state["head"] = str(cmd[-1])
            return SimpleNamespace(stdout="HEAD is now at abc123\n", stderr="", returncode=0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect, recorded


# ---------------------------------------------------------------------------
# Non-main branch → auto-checkout main
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fetch failure — friendly error messages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reset --hard failure — don't attempt stash restore
# ---------------------------------------------------------------------------

def test_cmd_update_restores_stash_when_rewrite_refuses_before_mutation(
    monkeypatch, tmp_path, capsys
):
    """A pre-mutation rewrite refusal can restore the exact parked changes."""
    _setup_update_mocks(monkeypatch, tmp_path)
    # Re-enable stash so it actually returns a ref
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )

    side_effect, _ = _make_update_side_effect(ff_only_fails=True, reset_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace())

    assert len(restore_calls) == 1

    out = capsys.readouterr().out
    assert "Restored the exact pre-update index" in out


# ---------------------------------------------------------------------------
# Non-interactive update.non_interactive_local_changes setting
# (chat app / gateway): "discard" throws stashed changes away, "stash"
# (default) restores them. Interactive terminal updates ignore the setting
# and always go through the restore path.
# ---------------------------------------------------------------------------

def _setup_setting_test(monkeypatch, tmp_path, mode):
    """Common wiring: real stash returns a ref, restore + discard are
    recorded, and load_config reports the given non_interactive_local_changes
    mode."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    discard_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_discard_stashed_changes",
        lambda *a, **kw: discard_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda *a, **kw: {"updates": {"non_interactive_local_changes": mode}},
    )
    side_effect, recorded = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)
    return restore_calls, discard_calls, recorded


# ---------------------------------------------------------------------------
# --keep-stash (desktop updater): stash for the update, never re-apply.
# ---------------------------------------------------------------------------

def _setup_keep_stash_test(monkeypatch, tmp_path):
    """Wiring for --keep-stash tests: stash returns a ref; restore, discard,
    and park are all recorded."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    discard_calls = []
    park_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_discard_stashed_changes",
        lambda *a, **kw: discard_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_park_stashed_changes",
        lambda *a, **kw: park_calls.append(a) or None,
    )
    # Keep the update flow away from the real gateway fleet on this machine —
    # a live gateway PID would trip the test-suite kill guard and turn the
    # run into exit 1 (gateway_fleet_restart_incomplete).
    monkeypatch.setattr(
        "hermes_cli.gateway.find_gateway_pids", lambda **kw: [], raising=False
    )
    return restore_calls, discard_calls, park_calls


def test_update_keep_stash_parks_instead_of_restoring(monkeypatch, tmp_path):
    """--keep-stash: after a successful update, the autostash is parked (left
    in git stash) — never re-applied, never discarded."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=True))

    assert len(park_calls) == 1
    assert park_calls[0][0] == "abc123deadbeef"
    assert restore_calls == []
    assert discard_calls == []


def test_update_without_keep_stash_still_restores(monkeypatch, tmp_path):
    """Regression guard: default behavior (no --keep-stash) is unchanged —
    the autostash is auto-restored under --yes."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=False))

    assert restore_calls == [1]
    assert park_calls == []
    assert discard_calls == []


def test_update_keep_stash_failure_path_still_preserves(monkeypatch, tmp_path, capsys):
    """--keep-stash applies only after success; a clean refusal restores."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect(ff_only_fails=True, reset_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=True))

    assert restore_calls == [1]
    assert park_calls == []
    assert discard_calls == []
    assert "Restored the exact pre-update index" in capsys.readouterr().out


def test_update_parser_accepts_keep_stash():
    """The flag parses and defaults off."""
    import argparse

    from hermes_cli.subcommands.update import build_update_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    build_update_parser(subparsers, cmd_update=lambda args: None)

    args = parser.parse_args(["update", "--keep-stash"])
    assert args.keep_stash is True
    args = parser.parse_args(["update"])
    assert args.keep_stash is False






def test_bootstrap_marker_not_autostashed_by_update(tmp_path):
    """#38529: the Desktop bootstrap marker must be git-ignored so that
    ``hermes update``'s ``git stash push --include-untracked`` does not sweep it
    into an autostash on every run.

    Behavioral + hermetic: build a throwaway repo that adopts the project's real
    ``.gitignore`` (the contract under test), drop the marker, and confirm the
    same stash invocation the updater uses leaves it untouched.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")

    repo_gitignore = Path(hermes_main.__file__).resolve().parents[1] / ".gitignore"

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(repo_gitignore.read_text())
    (tmp_path / "tracked.txt").write_text("x\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    marker = tmp_path / ".hermes-bootstrap-complete"
    marker.write_text("")

    # Exact flags used by hermes update (hermes_cli/main.py).
    git("stash", "push", "--include-untracked", "-m", "hermes-update-autostash")

    assert marker.exists(), (
        ".hermes-bootstrap-complete was swept into the update autostash — it must "
        "be listed in .gitignore so `git stash -u` skips it (#38529)."
    )
    # It must not even register as a dirty/untracked change.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert ".hermes-bootstrap-complete" not in status


def test_transaction_marker_recovers_stash_after_created_callback_interrupt(
    tmp_path,
):
    """The journal can reclaim a stash even if control never returns to caller."""
    import shutil
    import subprocess

    import hermes_cli.update_cmd as update_cmd

    if shutil.which("git") is None:
        pytest.skip("git not available")

    class AbortAfterCreate(BaseException):
        pass

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("committed\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "init")
    (tmp_path / "tracked.txt").write_text("local edit\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("local file\n", encoding="utf-8")

    prepared: list[str] = []
    created: list[str] = []

    def interrupt_after_create(stash_ref: str) -> None:
        created.append(stash_ref)
        raise AbortAfterCreate()

    with pytest.raises(AbortAfterCreate):
        update_cmd._stash_local_changes_if_needed(
            ["git"],
            tmp_path,
            on_prepared=prepared.append,
            on_created=interrupt_after_create,
        )

    assert len(prepared) == 1
    assert len(created) == 1
    recovered = update_cmd._find_stash_by_transaction_marker(
        ["git"], tmp_path, prepared[0]
    )
    assert recovered == created[0]
    assert git("status", "--porcelain").stdout == ""

    assert update_cmd._restore_stashed_changes(
        ["git"],
        tmp_path,
        recovered,
        prompt_user=False,
    )
    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "local edit\n"
    assert (tmp_path / "untracked.txt").read_text(encoding="utf-8") == "local file\n"


def test_transaction_stash_ownership_survives_foreign_top_of_stack(
    tmp_path, monkeypatch
):
    import shutil
    import subprocess

    import hermes_cli.update_cmd as update_cmd

    if shutil.which("git") is None:
        pytest.skip("git not available")

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("committed\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "init")
    (tmp_path / "tracked.txt").write_text("local edit\n", encoding="utf-8")

    original_run = update_cmd.subprocess.run
    injected = False

    def interleaved_run(command, *args, **kwargs):
        nonlocal injected
        result = original_run(command, *args, **kwargs)
        if not injected and command[1:3] == ["stash", "push"]:
            injected = True
            (tmp_path / "foreign.txt").write_text("foreign\n", encoding="utf-8")
            original_run(
                ["git", "stash", "push", "--include-untracked", "-m", "foreign"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=True,
            )
        return result

    monkeypatch.setattr(update_cmd.subprocess, "run", interleaved_run)

    owned = update_cmd._stash_local_changes_if_needed(["git"], tmp_path)
    top = original_run(
        ["git", "rev-parse", "--verify", "refs/stash"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert owned
    assert owned != top
    assert update_cmd._restore_stashed_changes(
        ["git"], tmp_path, owned, prompt_user=False
    )
    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "local edit\n"


def test_failed_push_never_treats_foreign_stash_as_safe_capture(
    tmp_path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    commands: list[list[str]] = []

    def result(command, *, returncode=0, stdout="", stderr=""):
        return SimpleNamespace(
            args=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def fake_run(command, **kwargs):
        command = list(command)
        commands.append(command)
        if command[1:] == ["status", "--porcelain"]:
            return result(command, stdout=" M tracked.txt\n")
        if command[1:] == ["ls-files", "--unmerged"]:
            return result(command)
        if command[1:3] == ["stash", "push"]:
            return result(command, returncode=1, stderr="stash failed")
        if command[1:3] == ["stash", "list"]:
            return result(command, stdout=("f" * 40) + "\tOn main: foreign\n")
        raise AssertionError(command)

    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)

    with pytest.raises(CalledProcessError):
        update_cmd._stash_local_changes_if_needed(["git"], tmp_path)

    assert ["git", "reset", "--hard", "HEAD"] not in commands


def test_nonzero_stash_push_preserves_concurrent_tracked_edit(
    tmp_path, monkeypatch
):
    """A late tracked write makes a partial stash fail closed, without reset."""
    import shutil
    import subprocess

    import hermes_cli.update_cmd as update_cmd

    if shutil.which("git") is None:
        pytest.skip("git not available")

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("base\n")
    git("add", "tracked.txt")
    git("commit", "-qm", "base")
    original_head = git("rev-parse", "HEAD").stdout.strip()
    (tmp_path / "tracked.txt").write_text("original local edit\n")

    original_run = update_cmd.subprocess.run
    commands: list[list[str]] = []

    def race_after_stash(command, *args, **kwargs):
        command = list(command)
        commands.append(command)
        result = original_run(command, *args, **kwargs)
        if command[1:3] == ["stash", "push"]:
            assert result.returncode == 0
            (tmp_path / "tracked.txt").write_text("concurrent tracked edit\n")
            return subprocess.CompletedProcess(
                command,
                1,
                stdout=result.stdout,
                stderr="simulated partial cleanup failure",
            )
        return result

    monkeypatch.setattr(update_cmd.subprocess, "run", race_after_stash)

    with pytest.raises(
        RuntimeError,
        match="nonzero stash push left an unclean tracked checkout",
    ):
        update_cmd._stash_local_changes_if_needed(["git"], tmp_path)

    assert git("rev-parse", "HEAD").stdout.strip() == original_head
    assert (tmp_path / "tracked.txt").read_text() == "concurrent tracked edit\n"
    assert ["git", "reset", "--hard", "HEAD"] not in commands
    stash_sha = git("stash", "list", "--format=%H").stdout.splitlines()[0]
    assert git("show", f"{stash_sha}:tracked.txt").stdout == "original local edit\n"
    assert stash_sha in git(
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-stashes/",
    ).stdout.splitlines()


def test_nonzero_stash_push_resets_only_exactly_captured_tracked_state(
    tmp_path, monkeypatch
):
    """A complete stash may safely clear the identical captured checkout."""
    import shutil
    import subprocess

    import hermes_cli.update_cmd as update_cmd

    if shutil.which("git") is None:
        pytest.skip("git not available")

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("base\n")
    git("add", "tracked.txt")
    git("commit", "-qm", "base")
    original_head = git("rev-parse", "HEAD").stdout.strip()
    (tmp_path / "tracked.txt").write_text("captured local edit\n")

    original_run = update_cmd.subprocess.run
    commands: list[list[str]] = []

    def partial_cleanup_failure(command, *args, **kwargs):
        command = list(command)
        commands.append(command)
        result = original_run(command, *args, **kwargs)
        if command[1:3] == ["stash", "push"]:
            assert result.returncode == 0
            # Model Git versions that create the stash but leave its exact
            # tracked worktree state behind after untracked cleanup fails.
            (tmp_path / "tracked.txt").write_text("captured local edit\n")
            return subprocess.CompletedProcess(
                command,
                1,
                stdout=result.stdout,
                stderr="simulated untracked cleanup failure",
            )
        return result

    monkeypatch.setattr(update_cmd.subprocess, "run", partial_cleanup_failure)

    stash_ref = update_cmd._stash_local_changes_if_needed(["git"], tmp_path)

    assert stash_ref
    assert git("rev-parse", "HEAD").stdout.strip() == original_head
    assert (tmp_path / "tracked.txt").read_text() == "base\n"
    assert git("show", f"{stash_ref}:tracked.txt").stdout == "captured local edit\n"
    assert ["git", "reset", "--hard", original_head] in commands


def test_rollout_refuses_unmerged_index_without_resetting_it(
    tmp_path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    commands: list[list[str]] = []

    def result(command, *, returncode=0, stdout="", stderr=""):
        return SimpleNamespace(
            args=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def fake_run(command, **kwargs):
        command = list(command)
        commands.append(command)
        if command[1:] == ["status", "--porcelain"]:
            return result(command, stdout="UU tracked.txt\n")
        if command[1:] == ["ls-files", "--unmerged"]:
            return result(command, stdout="100644 deadbeef 1\ttracked.txt\n")
        raise AssertionError(command)

    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)

    with pytest.raises(
        RuntimeError,
        match="cannot transactionally stash an unmerged Git index",
    ):
        update_cmd._stash_local_changes_if_needed(
            ["git"],
            tmp_path,
            on_prepared=lambda marker: None,
        )

    assert commands == [
        ["git", "status", "--porcelain"],
        ["git", "ls-files", "--unmerged"],
    ]


def test_fork_sync_defers_remote_push_until_canary_commit(
    tmp_path, monkeypatch
):
    import shutil
    import subprocess

    import hermes_cli.update_cmd as update_cmd

    if shutil.which("git") is None:
        pytest.skip("git not available")

    upstream = tmp_path / "upstream"
    upstream.mkdir()

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )

    git(upstream, "init", "-q", "-b", "main")
    git(upstream, "config", "user.email", "t@example.com")
    git(upstream, "config", "user.name", "t")
    (upstream / "tracked.txt").write_text("base\n")
    git(upstream, "add", "tracked.txt")
    git(upstream, "commit", "-qm", "base")

    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(upstream), str(origin))
    local = tmp_path / "local"
    git(tmp_path, "clone", "-q", str(origin), str(local))
    git(local, "remote", "add", "upstream", str(upstream))
    git(local, "fetch", "-q", "upstream", "main")
    origin_sha = git(local, "rev-parse", "origin/main").stdout.strip()
    local_sha = git(local, "rev-parse", "HEAD").stdout.strip()

    (upstream / "tracked.txt").write_text("upstream advance\n")
    git(upstream, "commit", "-am", "upstream advance", "-q")
    upstream_sha = git(upstream, "rev-parse", "HEAD").stdout.strip()

    pushes: list[bool] = []
    monkeypatch.setattr(
        update_cmd,
        "_sync_fork_with_upstream",
        lambda *args: pushes.append(True) or True,
    )

    pending = update_cmd._sync_with_upstream_if_needed(
        ["git"],
        local,
        push_origin=False,
        expected_branch="main",
        expected_head_sha=local_sha,
        origin_sha=origin_sha,
    )

    assert pending is True
    assert pushes == []
    assert git(local, "rev-parse", "HEAD").stdout.strip() == upstream_sha
    assert git(local, "rev-parse", "origin/main").stdout.strip() == origin_sha
    private_refs = git(
        local,
        "for-each-ref",
        "--format=%(objectname)",
        "refs/hermes-update-fetches/",
    ).stdout.splitlines()
    assert private_refs == []


def test_fork_push_uses_immutable_source_and_exact_remote_lease(tmp_path):
    """A local tracking-ref refresh must not widen the verified push lease."""

    import shutil
    import subprocess

    import hermes_cli.update_cmd as update_cmd

    if shutil.which("git") is None:
        pytest.skip("git not available")

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.email", "t@example.com")
    git(seed, "config", "user.name", "t")
    (seed / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "-qm", "base")

    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    local = tmp_path / "local"
    concurrent = tmp_path / "concurrent"
    git(tmp_path, "clone", "-q", str(origin), str(local))
    git(tmp_path, "clone", "-q", str(origin), str(concurrent))
    for checkout in (local, concurrent):
        git(checkout, "config", "user.email", "t@example.com")
        git(checkout, "config", "user.name", "t")

    expected_origin_sha = git(local, "rev-parse", "origin/main").stdout.strip()
    (local / "tracked.txt").write_text("verified candidate\n", encoding="utf-8")
    git(local, "commit", "-qam", "verified candidate")
    source_sha = git(local, "rev-parse", "HEAD").stdout.strip()

    (concurrent / "tracked.txt").write_text("concurrent remote\n", encoding="utf-8")
    git(concurrent, "commit", "-qam", "concurrent remote")
    git(concurrent, "push", "-q", "origin", "main")
    concurrent_sha = git(concurrent, "rev-parse", "HEAD").stdout.strip()

    # Reproduce the dangerous race: an unrelated fetch advances the implicit
    # force-with-lease authority after candidate verification.
    git(local, "fetch", "-q", "origin", "main")
    assert git(local, "rev-parse", "origin/main").stdout.strip() == concurrent_sha

    pushed = update_cmd._sync_fork_with_upstream(
        ["git"],
        local,
        source_sha=source_sha,
        expected_origin_sha=expected_origin_sha,
        expected_branch="main",
    )

    assert pushed is False
    assert (
        git(origin, "rev-parse", "refs/heads/main").stdout.strip()
        == concurrent_sha
    )

    # Once the caller explicitly authorizes that exact remote generation, the
    # immutable candidate SHA (not a mutable branch name) is what gets pushed.
    assert update_cmd._sync_fork_with_upstream(
        ["git"],
        local,
        source_sha=source_sha,
        expected_origin_sha=concurrent_sha,
        expected_branch="main",
    )
    assert git(origin, "rev-parse", "refs/heads/main").stdout.strip() == source_sha


def test_bot_rollout_never_prompts_after_gateway_quiescence():
    import hermes_cli.update_cmd as update_cmd

    assert not update_cmd._should_prompt_for_stash_restore(
        has_stash=True,
        assume_yes=False,
        gateway_mode=True,
        rollout_enabled=True,
        stdin_tty=False,
        stdout_tty=False,
    )
    # The historical non-rollout bot updater still has a live watcher capable
    # of relaying the prompt, so its behavior is intentionally unchanged.
    assert update_cmd._should_prompt_for_stash_restore(
        has_stash=True,
        assume_yes=False,
        gateway_mode=True,
        rollout_enabled=False,
        stdin_tty=False,
        stdout_tty=False,
    )


def test_verified_rollout_stash_is_never_dropped_by_positional_selector(
    monkeypatch, tmp_path
):
    import hermes_cli.update_cmd as update_cmd

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        assert command[1:3] != ["stash", "drop"]
        return SimpleNamespace(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)

    assert not update_cmd._drop_verified_stash(
        ["git"], tmp_path, "a" * 40
    )
    assert any("for-each-ref" in command for command in commands)


# ---------------------------------------------------------------------------
# Permission-denied autostash class: undeletable untracked files (root-owned
# packaging/ etc.) must not abort the update when the stash entry was created.
# ---------------------------------------------------------------------------






def test_update_autostash_survives_undeletable_untracked_dir(tmp_path):
    """Behavioral E2E of the whole permission-denied class with real git:
    root-owned-style undeletable untracked dir → stash succeeds, update-style
    reset works, restore round-trips, nothing lost. (#70127 follow-up)"""
    import os
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")
    if os.name == "nt":
        pytest.skip("POSIX permission semantics")
    if os.geteuid() == 0:
        pytest.skip("root ignores directory write bits")

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=check
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("v1\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    (tmp_path / "tracked.txt").write_text("v2 local change\n")
    pkg = tmp_path / "packaging" / "homebrew"
    pkg.mkdir(parents=True)
    (pkg / "hermes-agent.rb").write_text("formula\n")
    os.chmod(pkg, 0o555)  # undeletable contents, like a root-owned dir
    try:
        stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
        assert stash_ref

        # The tracked change is stashed; simulate the updater's checkout window.
        assert (tmp_path / "tracked.txt").read_text() == "v1\n"

        restored = hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )
        assert restored is True
        assert (tmp_path / "tracked.txt").read_text() == "v2 local change\n"
        assert (pkg / "hermes-agent.rb").read_text() == "formula\n"
    finally:
        os.chmod(pkg, 0o755)
