"""Tests for tools/self_repo_guard.py — the running-source-checkout git guard."""

import subprocess
from pathlib import Path

import pytest

from tools.self_repo_guard import (
    detect_self_repo_git_mutation,
    get_running_source_root,
)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "hermes-agent"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "agent").mkdir()
    return root.resolve()


def _detect(command, cwd, root):
    return detect_self_repo_git_mutation(command, str(cwd), source_root=root)


class TestBlocksMutationsInSourceRepo:
    @pytest.mark.parametrize(
        "sub",
        [
            "checkout pr-51020",
            "switch main",
            "bisect start",
            "bisect good HEAD~10",
            "reset --hard origin/main",
            "reset --har origin/main",
            "rebase origin/main",
            "merge origin/main",
            "pull",
            "restore .",
            "stash",
            "stash pop",
            "clean -fd",
            "cherry-pick abc123",
            "revert HEAD",
        ],
    )
    def test_cwd_inside_repo(self, repo, sub):
        hit, msg = _detect(f"git {sub}", repo, repo)
        assert hit is True
        assert str(repo) in msg

    def test_cwd_in_repo_subdirectory(self, repo):
        hit, _ = _detect("git checkout main", repo / "agent", repo)
        assert hit is True

    def test_dash_c_targeting_repo_from_outside(self, repo, tmp_path):
        hit, _ = _detect(f"git -C {repo} checkout pr-51020", tmp_path, repo)
        assert hit is True

    def test_cd_into_repo_then_checkout(self, repo, tmp_path):
        hit, _ = _detect(f"cd {repo} && git checkout pr-51020", tmp_path, repo)
        assert hit is True

    def test_relative_cd_into_repo(self, repo):
        hit, _ = _detect("cd hermes-agent && git pull", repo.parent, repo)
        assert hit is True

    def test_mutation_after_safe_command(self, repo):
        hit, _ = _detect("git status; git reset --hard HEAD~1", repo, repo)
        assert hit is True

    def test_wrapped_in_sudo_env(self, repo):
        hit, _ = _detect("sudo env GIT_PAGER=cat git checkout main", repo, repo)
        assert hit is True

    @pytest.mark.parametrize(
        "command",
        [
            "sudo -u root git checkout main",
            "env -u GIT_PAGER git switch main",
            "/usr/bin/git checkout main",
            "sh -c 'git checkout main'",
            "bash -lc 'git switch main'",
            "bash -o pipefail -c 'git checkout main'",
            "bash +O extglob -c 'git checkout main'",
            "zsh -yc 'git checkout main'",
            "dash -Vc 'git checkout main'",
            "ksh -Gc 'git checkout main'",
        ],
    )
    def test_wrappers_and_nested_shells(self, repo, command):
        hit, _ = _detect(command, repo, repo)
        assert hit is True

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr checkout 51020",
            "hub pr checkout 51020",
        ],
    )
    def test_pr_checkout_clients(self, repo, command):
        hit, _ = _detect(command, repo, repo)
        assert hit is True

    def test_explicit_work_tree_targeting_repo(self, repo, tmp_path):
        command = f"git --git-dir={repo / '.git'} --work-tree={repo} checkout main"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_git_environment_targeting_repo(self, repo, tmp_path):
        command = f"GIT_DIR={repo / '.git'} GIT_WORK_TREE={repo} git checkout main"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_inline_git_alias(self, repo):
        hit, _ = _detect("git -c alias.co=checkout co main", repo, repo)
        assert hit is True

    def test_configured_git_alias(self, repo):
        subprocess.run(
            ["git", "-C", str(repo), "config", "alias.co", "checkout"],
            check=True,
        )
        hit, _ = _detect("git co main", repo, repo)
        assert hit is True

    def test_alias_config_read_error_fails_closed(self, repo, monkeypatch):
        """A subcommand that *could* be a mutating alias must block if the
        alias config read itself errors (OSError/timeout) -- not be treated
        as "no such alias" the way a clean returncode=1 is. Regression test
        for the fail-open gap flagged in PR #81664 review: _read_git_alias
        must not collapse "config read failed" into the same None as
        "alias not configured".
        """
        import subprocess as subprocess_mod
        from tools import self_repo_guard as guard_mod

        original_run = subprocess_mod.run

        def _flaky_run(cmd, *args, **kwargs):
            # Only break the alias config lookup; let git init / other
            # setup calls through untouched.
            if len(cmd) >= 4 and cmd[-3:-1] == ["config", "--get"] or (
                "config" in cmd and "--get" in cmd
            ):
                raise OSError("simulated alias config read failure")
            return original_run(cmd, *args, **kwargs)

        monkeypatch.setattr(guard_mod.subprocess, "run", _flaky_run)

        # "co" is not a known git builtin, so a successful read would return
        # None (no such alias) and the command would be allowed. With the
        # read forced to error, the guard must fail closed and block.
        hit, _ = _detect("git co main", repo, repo)
        assert hit is True

    def test_alias_chain_at_recursion_boundary_blocks(self, repo):
        """A 5-level alias chain (a1->a2->a3->a4->a5->checkout) means that
        by the time depth reaches _MAX_RECURSION (4), the current subcommand
        (a5) is still an unresolved alias name -- not yet known to be
        'checkout'. Before the fix, the depth-limit branch returned None
        ("no mutation found"), silently allowing an alias chain that would
        have resolved to a destructive command if expansion had continued.
        """
        from tools.self_repo_guard import _MAX_RECURSION
        assert _MAX_RECURSION == 4  # test assumes this exact boundary

        chain = ["a1", "a2", "a3", "a4", "a5"]
        for i in range(len(chain) - 1):
            subprocess.run(
                ["git", "-C", str(repo), "config", f"alias.{chain[i]}", chain[i + 1]],
                check=True,
            )
        subprocess.run(
            ["git", "-C", str(repo), "config", f"alias.{chain[-1]}", "checkout"],
            check=True,
        )

        hit, reason = _detect(f"git {chain[0]} main", repo, repo)
        assert hit is True
        assert "depth" in (reason or "").lower()

    def test_alias_chain_below_recursion_boundary_still_blocks_via_mutation(self, repo):
        """Control case: a shorter alias chain that fully resolves to a
        destructive subcommand within the recursion budget must still block
        -- via the direct _mutates_worktree() path, not the depth guard.
        """
        chain = ["b1", "b2"]
        for i in range(len(chain) - 1):
            subprocess.run(
                ["git", "-C", str(repo), "config", f"alias.{chain[i]}", chain[i + 1]],
                check=True,
            )
        subprocess.run(
            ["git", "-C", str(repo), "config", f"alias.{chain[-1]}", "checkout"],
            check=True,
        )

        hit, reason = _detect(f"git {chain[0]} main", repo, repo)
        assert hit is True
        assert "depth" not in (reason or "").lower()

    def test_nested_shell_recursion_beyond_boundary_blocks(self, repo):
        """_find_mutation's own recursion counter (independent of the git
        alias depth counter in _inspect_git) must fail closed once it
        exceeds _MAX_RECURSION, rather than silently returning None
        ("no mutation found") for an unanalyzed nested command.

        Calls _find_mutation directly at depth=_MAX_RECURSION+1 to exercise
        the cutoff deterministically -- constructing an actual shell string
        with 5+ levels of nested quoting to hit this via _detect() is
        fragile (each level needs distinct, correctly-escaped quote
        characters), so the depth parameter is exercised directly instead.
        """
        from tools.self_repo_guard import _find_mutation, _MAX_RECURSION

        result = _find_mutation(
            "git checkout main", repo, repo, depth=_MAX_RECURSION + 1
        )
        assert result is not None
        assert "depth" in result.lower() or "recursion" in result.lower()

    def test_find_mutation_within_recursion_boundary_still_inspects(self, repo):
        """Control case: at or below the boundary, _find_mutation must
        still actually inspect the command (not short-circuit), so a
        destructive command within budget is caught normally.
        """
        from tools.self_repo_guard import _find_mutation, _MAX_RECURSION

        result = _find_mutation(
            "git checkout main", repo, repo, depth=_MAX_RECURSION
        )
        assert result is not None
        assert "checkout" in result.lower()

    def test_alias_read_error_sentinel_distinct_from_no_alias(self):
        """_read_git_alias must return a distinct value for a config-read
        error vs. a clean "no such alias" result, so callers can tell them
        apart and fail closed only on the former.
        """
        from tools.self_repo_guard import _read_git_alias, _ALIAS_READ_ERROR
        import subprocess as subprocess_mod

        # Nonexistent git binary -> OSError inside _read_git_alias.
        result = _read_git_alias("this-binary-does-not-exist-xyz", Path("."), "co")
        assert result is _ALIAS_READ_ERROR
        assert result is not None

    def test_mutation_in_command_substitution(self, repo):
        hit, _ = _detect('echo "$(git checkout main)"', repo, repo)
        assert hit is True

    @pytest.mark.parametrize(
        "command",
        [
            'echo "$(echo ready && git checkout main)"',
            "echo `git checkout main`",
            'echo "`git checkout main`"',
        ],
    )
    def test_nested_command_lists(self, repo, command):
        hit, _ = _detect(command, repo, repo)
        assert hit is True

    def test_shell_heredoc_is_executed(self, repo):
        command = "bash <<'EOF'\ngit checkout main\nEOF\n"
        hit, _ = _detect(command, repo, repo)
        assert hit is True

    def test_tilde_dash_c_path(self, repo, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(repo.parent))
        hit, _ = _detect("git -C ~/hermes-agent checkout main", tmp_path, repo)
        assert hit is True


class TestAllowsSafeCommands:
    @pytest.mark.parametrize(
        "cmd",
        [
            "git status",
            "git log --oneline -5",
            "git diff main...HEAD",
            "git branch --show-current",
            "git stash list",
            "git stash show -p",
            "git stash create",
            "git stash store abc123",
            "git stash drop",
            "git stash clear",
            "git reset --soft HEAD~1",
            "git reset --mixed HEAD~1",
            "git restore --staged pyproject.toml",
            "git clean --dry-run -fd",
            "git clean -nd",
            "git commit -m 'msg'",
            "git add -A",
            "git fetch origin main",
            "git worktree add /tmp/wt feature-branch",
            "git push fork feature-branch",
            "ls -la",
            "grep -rn checkout tools/",
        ],
    )
    def test_read_only_and_dev_loop_in_repo(self, repo, cmd):
        hit, _ = _detect(cmd, repo, repo)
        assert hit is False

    def test_mutation_in_other_repo(self, repo, tmp_path):
        other = tmp_path / "other-project"
        other.mkdir()
        hit, _ = _detect("git checkout main", other, repo)
        assert hit is False

    def test_dash_c_redirects_out_of_repo(self, repo, tmp_path):
        hit, _ = _detect(f"git -C {tmp_path} checkout main", repo, repo)
        assert hit is False

    def test_cd_out_of_repo_then_checkout(self, repo, tmp_path):
        hit, _ = _detect(f"cd {tmp_path} && git checkout main", repo, repo)
        assert hit is False

    def test_mentioning_repo_path_without_targeting_it(self, repo, tmp_path):
        hit, _ = _detect(f"echo {repo} && git checkout main", tmp_path, repo)
        assert hit is False

    def test_checkout_as_grep_pattern_not_git(self, repo):
        hit, _ = _detect("grep checkout file.txt", repo, repo)
        assert hit is False

    def test_pr_checkout_words_in_other_gh_command_are_safe(self, repo):
        hit, _ = _detect("gh api /repos/example/pr/checkout", repo, repo)
        assert hit is False

    @pytest.mark.parametrize(
        "command",
        [
            'echo "safe | git checkout main"',
            "echo '$(git checkout main)'",
            "printf '%s\\n' 'git checkout main'",
        ],
    )
    def test_quoted_git_text_is_not_executed(self, repo, command):
        hit, _ = _detect(command, repo, repo)
        assert hit is False

    @pytest.mark.parametrize(
        "command",
        [
            "cat > script.sh <<'EOF'\ngit checkout main\nEOF\n",
            "python - <<'PY'\nprint('git checkout main')\nPY\n",
        ],
    )
    def test_data_heredoc_is_not_executed_as_shell(self, repo, command):
        hit, _ = _detect(command, repo, repo)
        assert hit is False

    def test_subshell_cd_does_not_leak(self, repo):
        command = f"(cd {repo} && git status); git checkout main"
        hit, _ = _detect(command, repo.parent, repo)
        assert hit is False

    def test_pipeline_cd_does_not_leak(self, repo):
        command = f"cd {repo} | cat; git checkout main"
        hit, _ = _detect(command, repo.parent, repo)
        assert hit is False

    def test_successful_cd_or_branch_does_not_run(self, repo):
        command = f"cd {repo} || git checkout main"
        hit, _ = _detect(command, repo.parent, repo)
        assert hit is False

    def test_empty_command(self, repo):
        hit, _ = _detect("", repo, repo)
        assert hit is False

    def test_packaged_install_is_inert(self, monkeypatch, tmp_path):
        import tools.self_repo_guard as mod

        monkeypatch.setattr(mod, "get_running_source_root", lambda: None)
        hit, msg = mod.detect_self_repo_git_mutation("git checkout main", str(tmp_path))
        assert hit is False
        assert msg is None


class TestWorktreeTargetingSourceRoot:
    @pytest.mark.parametrize(
        "sub",
        [
            "remove .",
            "remove -f .",
            "remove --force .",
            "remove -- .",
            "move . {other}",
            "move -f . {other}",
        ],
    )
    def test_blocks_relative_target_from_inside(self, repo, tmp_path, sub):
        command = f"git worktree {sub.format(other=tmp_path / 'moved')}"
        hit, msg = _detect(command, repo, repo)
        assert hit is True
        assert str(repo) in msg

    @pytest.mark.parametrize("action", ["remove", "remove -f", "remove --force"])
    def test_blocks_absolute_target_from_outside(self, repo, tmp_path, action):
        hit, _ = _detect(f"git worktree {action} {repo}", tmp_path, repo)
        assert hit is True

    def test_blocks_move_of_root_from_outside(self, repo, tmp_path):
        command = f"git worktree move {repo} {tmp_path / 'moved'}"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_blocks_dash_c_worktree_remove(self, repo, tmp_path):
        hit, _ = _detect(f"git -C {tmp_path} worktree remove {repo}", tmp_path, repo)
        assert hit is True

    def test_blocks_parent_relative_target_from_subdirectory(self, repo):
        hit, _ = _detect("git worktree remove ..", repo / "agent", repo)
        assert hit is True

    def test_blocks_sibling_relative_target(self, repo):
        hit, _ = _detect(f"git worktree remove ../{repo.name}", repo, repo)
        assert hit is True

    @pytest.mark.parametrize(
        "sub",
        [
            "add {other}",
            "add -b feature {other}",
            "list",
            "list --porcelain",
            "prune",
            "lock {other}",
            "unlock {other}",
            "remove {other}",
            "move {other} {other}-dest",
        ],
    )
    def test_allows_other_worktrees_and_add(self, repo, tmp_path, sub):
        command = f"git worktree {sub.format(other=tmp_path / 'other-wt')}"
        hit, _ = _detect(command, repo, repo)
        assert hit is False

    @pytest.mark.parametrize("sub", ["", "remove", "move", "-f"])
    def test_incomplete_worktree_command_is_not_blocked(self, repo, sub):
        hit, _ = _detect(f"git worktree {sub}".strip(), repo, repo)
        assert hit is False


class TestSourceRootResolution:
    def test_resolves_to_repo_when_git_dir_present(self):
        root = get_running_source_root()
        if root is not None:
            assert (root / ".git").exists()

    def test_worktree_git_file_counts(self, tmp_path, monkeypatch):
        import tools.self_repo_guard as mod

        root = tmp_path / "wt"
        root.mkdir()
        (root / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
        (root / "tools").mkdir()
        fake_file = root / "tools" / "self_repo_guard.py"
        fake_file.write_text("")
        monkeypatch.setattr(mod, "__file__", str(fake_file))
        assert mod.get_running_source_root() == root.resolve()


class TestUnparseableCommands:
    def test_unbalanced_quotes_fall_back(self, repo):
        hit, _ = _detect('git checkout "unterminated', repo, repo)
        assert hit is True

    def test_subshell_syntax_does_not_crash(self, repo):
        hit, _ = _detect("VAL=$(git rev-parse HEAD) git checkout main", repo, repo)
        assert hit is True


class TestBlockMessageGuidance:
    """The block message must steer agents to a disk-backed scratch clone,
    not a bare "temporary clone" (agents defaulted to /tmp, which is tmpfs
    on most distros — parallel salvage clones running npm ci filled a 32GB
    tmpfs to 97% in one campaign)."""

    def test_message_recommends_shared_clone_on_disk(self, repo):
        hit, msg = _detect("git rebase origin/main", repo, repo)
        assert hit is True
        assert "git clone --shared" in msg
        assert "scratch" in msg

    def test_message_warns_against_tmp_for_dep_installs(self, repo):
        hit, msg = _detect("git rebase origin/main", repo, repo)
        assert hit is True
        assert "tmpfs" in msg
        assert "Delete the clone" in msg

    def test_scratch_hint_honors_hermes_home(self, repo, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", "/custom/hermes-home")
        hit, msg = _detect("git rebase origin/main", repo, repo)
        assert hit is True
        assert "/custom/hermes-home/scratch" in msg
