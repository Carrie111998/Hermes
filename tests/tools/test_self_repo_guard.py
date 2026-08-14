"""Tests for tools/self_repo_guard.py — the running-source-checkout git guard."""

import subprocess
from pathlib import Path

import pytest

from tools.approval import _deobfuscate_shell_word_for_detection
from tools.self_repo_guard import (
    _explicit_git_path,
    _is_mangled_windows_drive,
    _normalize_git_path_operand,
    _path_aware_shell_word,
    _shell_words_at,
    _strip_quotes_preserve_windows_path,
    _windows_git_bash_to_drive,
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


def _explicit_target_operands(
    path: Path, path_style: str, cwd: Path
) -> tuple[str, Path, Path]:
    """Return distinct target spellings that share one guard-visible root."""
    native_path = path
    source_root = native_path
    target_cwd = cwd
    if native_path.drive:
        drive = native_path.drive.removesuffix(":")
        native_target = str(native_path)
    else:
        # On a host without drive semantics, the production resolver maps
        # /c/... to the logical c:/... path relative to /. Use that same
        # controlled logical root for both spellings instead of mixing the
        # real fixture with an unrelated synthetic root.
        drive = "c"
        source_root = Path(f"{drive}:{native_path.as_posix()}")
        target_cwd = Path("/")
        native_target = str(source_root)

    if path_style == "native":
        target = native_target
    else:
        if native_path.drive:
            tail = native_path.as_posix()[len(native_path.drive) :]
        else:
            tail = native_path.as_posix()
        target = f"/{drive.lower()}{tail}"
        assert target.startswith(f"/{drive.lower()}/")
        assert target != native_target
    return target, target_cwd, source_root


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

    @pytest.mark.parametrize("path_style", ["native", "git_bash"])
    def test_dash_c_quoted_windows_targeting_repo_from_outside(
        self, repo, tmp_path, path_style
    ):
        target, cwd, source_root = _explicit_target_operands(repo, path_style, tmp_path)
        hit, _ = _detect(f'git -C "{target}" checkout pr-51020', cwd, source_root)
        assert hit is True

    def test_colon_bearing_posix_target_reaches_explicit_target_path(self):
        colon_path = Path("/tmp/worker:123/hermes-agent")
        target, cwd, source_root = _explicit_target_operands(
            colon_path, "git_bash", Path("/")
        )
        assert colon_path.drive == ""
        assert ":" in colon_path.as_posix()
        hit, message = _detect(
            f'git -C "{target}" checkout pr-51020', cwd, source_root
        )
        assert hit is True
        assert message is not None

    def test_rejects_chained_explicit_targets(self, repo, tmp_path):
        other = tmp_path / "other-project"
        other.mkdir()
        command = f'git -C "{repo}" -C "{other}" checkout pr-51020'
        hit, _ = _detect(command, tmp_path, repo)
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
        (root / ".git").write_text(
            "gitdir: /somewhere/.git/worktrees/wt\n", encoding="utf-8"
        )
        (root / "tools").mkdir()
        fake_file = root / "tools" / "self_repo_guard.py"
        fake_file.write_text("", encoding="utf-8")
        monkeypatch.setattr(mod, "__file__", str(fake_file))
        assert mod.get_running_source_root() == root.resolve()


class TestUnparseableCommands:
    def test_unbalanced_quotes_fall_back(self, repo):
        hit, _ = _detect('git checkout "unterminated', repo, repo)
        assert hit is True

    def test_subshell_syntax_does_not_crash(self, repo):
        hit, _ = _detect("VAL=$(git rev-parse HEAD) git checkout main", repo, repo)
        assert hit is True


class TestGitDirAndEnvTargeting:
    """Explicit git-dir selectors must resolve to the worktree they govern.

    Covers the residual fail-open sinks shared by main/#82586/#82636:
    ``--git-dir`` (bare and glued) and ``GIT_DIR`` never resolved into the
    effective target, so ``git --git-dir <src>/.git checkout`` switched the
    source branch with rc=0 from any directory.
    """

    def test_git_dir_bare_operand_targeting_repo(self, repo, tmp_path):
        command = f"git --git-dir {repo / '.git'} checkout main"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_git_dir_glued_operand_targeting_repo(self, repo, tmp_path):
        command = f"git --git-dir={repo / '.git'} checkout main"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_git_dir_glued_git_bash_form_targeting_repo(self, repo, tmp_path):
        target, cwd, source_root = _explicit_target_operands(
            repo, "git_bash", tmp_path
        )
        command = f"git --git-dir={target}/.git checkout main"
        hit, _ = _detect(command, cwd, source_root)
        assert hit is True

    def test_git_dir_env_targeting_repo(self, repo, tmp_path):
        command = f"GIT_DIR={repo / '.git'} git checkout main"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_git_work_tree_env_targeting_repo(self, repo, tmp_path):
        command = f"GIT_WORK_TREE={repo} git checkout main"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_git_dir_and_work_tree_env_targeting_repo(self, repo, tmp_path):
        command = f"GIT_DIR={repo / '.git'} GIT_WORK_TREE={repo} git checkout main"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_git_dir_other_repo_mutation_allowed(self, repo, tmp_path):
        other = tmp_path / "other-project"
        other.mkdir()
        (other / ".git").mkdir()
        command = f"git --git-dir {other / '.git'} checkout main"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is False

    def test_git_dir_safe_read_in_repo_allowed(self, repo, tmp_path):
        command = f"git --git-dir {repo / '.git'} status"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is False

    def test_git_dir_bare_repo_mutation_fail_closed(self, repo, tmp_path):
        command = f"git --git-dir {tmp_path / 'bare-repo.git'} checkout main"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True


class TestExplicitTargetSinkClosure:
    """Operand forms that must not fail open (glued, single-quoted, mangled)."""

    def test_glued_dash_c_native_blocks(self, repo, tmp_path):
        command = f"git -C{repo} checkout pr-51020"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_glued_work_tree_native_blocks(self, repo, tmp_path):
        command = f"git --work-tree={repo} checkout pr-51020"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_single_quoted_dash_c_native_blocks(self, repo, tmp_path):
        command = f"git -C '{repo}' checkout pr-51020"
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_mangled_drive_operand_fail_closed(self, repo, tmp_path):
        hit, _ = _detect("git -C D:workhermes checkout main", tmp_path, repo)
        assert hit is True

    def test_glued_mangled_drive_fail_closed(self, repo, tmp_path):
        hit, _ = _detect("git -CD:workhermes checkout main", tmp_path, repo)
        assert hit is True

    def test_chained_dash_c_all_outside_status_allowed(self, repo, tmp_path):
        outside_a = tmp_path / "outside-a"
        outside_b = tmp_path / "outside-b"
        outside_a.mkdir()
        outside_b.mkdir()
        hit, _ = _detect(
            f"git -C {outside_a} -C {outside_b} status", tmp_path, repo
        )
        assert hit is False

    def test_chained_dash_c_all_outside_checkout_allowed(self, repo, tmp_path):
        outside_a = tmp_path / "outside-a"
        outside_b = tmp_path / "outside-b"
        outside_a.mkdir()
        outside_b.mkdir()
        hit, _ = _detect(
            f"git -C {outside_a} -C {outside_b} checkout main", tmp_path, repo
        )
        assert hit is False

    def test_chained_dash_c_in_root_safe_read_allowed(self, repo, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        hit, _ = _detect(f"git -C {repo} -C {outside} status", tmp_path, repo)
        assert hit is False


class TestExplicitGitTargetPaths:
    """Windows / Git-Bash explicit ``-C`` operands must not fail open.

    Salvaged from #82586 (olympusbuildz): operand-only path-aware words that
    keep native separators, normalize Git-Bash drive spellings, and never
    rewrite non-path tokens.
    """

    def test_preserves_windows_backslash_in_dash_c_operand(self):
        raw = r'"D:\work\hermes-agent"'
        preserved = _strip_quotes_preserve_windows_path(raw)
        assert preserved == r"D:\work\hermes-agent"
        assert "\\" in preserved
        # Path preserve applies in -C operand position via _shell_words_at,
        # not to bare quoted Windows paths in arbitrary token slots.
        words = _shell_words_at(f"git -C {raw} status", 0)
        assert words[2] == r"D:\work\hermes-agent"
        assert _path_aware_shell_word(raw) == r"D:\work\hermes-agent"
        normalized = _normalize_git_path_operand(r"D:\work\hermes-agent")
        assert normalized == r"D:\work\hermes-agent"
        path = _explicit_git_path(r"D:\work\hermes-agent", Path("/tmp"))
        assert path == _explicit_git_path("D:/work/hermes-agent", Path("/tmp"))
        assert path.is_absolute()
        text = str(path).replace("\\", "/")
        assert "work" in text and "hermes-agent" in text

    def test_git_bash_drive_path_normalizes(self):
        assert _windows_git_bash_to_drive("/d/work/hermes-agent") == "D:/work/hermes-agent"
        assert _windows_git_bash_to_drive("/D/work/x") == "D:/work/x"
        assert _windows_git_bash_to_drive("/c") == "C:/"
        assert _normalize_git_path_operand("/d/work/hermes-agent") == "D:/work/hermes-agent"
        path = _explicit_git_path("/d/work/hermes-agent", Path("/var/tmp"))
        assert path == _explicit_git_path("D:/work/hermes-agent", Path("/var/tmp"))
        assert path.is_absolute()
        text = str(path).replace("\\", "/")
        assert "work/hermes-agent" in text
        # Use non-escape string forms: "/tmp/..." embeds a TAB via \t in
        # ordinary Python literals and would accidentally pass a looser regex.
        assert _windows_git_bash_to_drive("/" + "tmp/foo") is None
        assert _windows_git_bash_to_drive("/" + "dev/null") is None
        assert _windows_git_bash_to_drive("/" + "Users/hermes") is None
        assert _windows_git_bash_to_drive("/" + "etc/passwd") is None

    def test_dash_c_windows_path_blocks_mutation_when_normalized_target_is_repo(
        self, repo, tmp_path, monkeypatch
    ):
        import tools.self_repo_guard as mod

        win_path = r"D:\work\hermes-agent"

        def fake_explicit(path_str, base):
            if path_str.replace("\\", "/") in {
                win_path.replace("\\", "/"),
                "D:/work/hermes-agent",
            } or path_str == win_path:
                return repo
            return mod._resolve_git_target(
                mod._normalize_git_path_operand(path_str), base
            )

        monkeypatch.setattr(mod, "_explicit_git_path", fake_explicit)
        # Double-quoted native Windows path — path-aware word keep separators,
        # then our stub maps the operand onto the real temp repo.
        command = f'git -C "{win_path}" checkout main'
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_multiple_dash_c_fail_closed_when_first_is_repo(self, repo, tmp_path):
        # Intermediate -C lands in the source root even though a later -C
        # leaves it — conservative rule blocks the mutating subcommand.
        hit, _ = _detect(
            f"git -C {repo} -C {tmp_path} checkout main",
            tmp_path,
            repo,
        )
        assert hit is True

    def test_multiple_dash_c_all_outside_still_allowed(self, repo, tmp_path):
        outside_a = tmp_path / "outside-a"
        outside_b = tmp_path / "outside-b"
        outside_a.mkdir()
        outside_b.mkdir()
        hit, _ = _detect(
            f"git -C {outside_a} -C {outside_b} checkout main",
            tmp_path,
            repo,
        )
        assert hit is False

    def test_mangled_drive_path_operand_fail_closed(self, repo, tmp_path):
        assert _is_mangled_windows_drive("D:workhermes")
        assert not _is_mangled_windows_drive(r"D:\work\hermes")
        assert not _is_mangled_windows_drive("D:/work/hermes")
        # Escape-stripped drive form must fail closed for mutations regardless
        # of ambient cwd (do not fall open to outside-the-repo ambient path).
        hit, _ = _detect("git -C D:workhermes checkout main", tmp_path, repo)
        assert hit is True

    def test_path_aware_word_recovers_quoted_windows_path(self):
        # Simulates the raw token _read_shell_word returns for a double-quoted
        # Windows path; approval deobfuscation would mangle it. Recovery is
        # via the path-operand helper / -C position — not every shell token.
        raw = r'"D:\work\hermes-agent"'
        mangled = _deobfuscate_shell_word_for_detection(raw)
        assert mangled == "D:workhermes-agent" or "\\" not in mangled
        words = _shell_words_at(f"git -C {raw} checkout main", 0)
        assert words[2] == r"D:\work\hermes-agent"
        assert not _is_mangled_windows_drive(words[2])
        assert _path_aware_shell_word(raw) == r"D:\work\hermes-agent"

    def test_configured_alias_still_blocks_in_source_repo(self, repo):
        """Regression: operand-only path-aware words must not break aliases."""
        subprocess.run(
            ["git", "-C", str(repo), "config", "alias.co", "checkout"],
            check=True,
        )
        hit, _ = _detect("git co main", repo, repo)
        assert hit is True

    def test_non_path_words_use_normal_deobfuscation(self):
        """Path-aware preserve must not blanket every shell token."""
        raw_path = r'"D:\work\hermes-agent"'
        # Outside a Git path-operand position, the quoted Windows path is
        # normal-deobfuscated (backslashes stripped as shell escapes).
        words = _shell_words_at(f"echo {raw_path}", 0)
        assert words[0] == "echo"
        assert words[1] == _deobfuscate_shell_word_for_detection(raw_path)
        assert "\\" not in words[1]
        # Contrast: the same token after bare -C keeps separators.
        git_words = _shell_words_at(f"git -C {raw_path} status", 0)
        assert git_words[2] == r"D:\work\hermes-agent"
        assert "\\" in git_words[2]


class TestClassBoundaryClosure:
    """Regression coverage for every explicit-target boundary in the class."""

    def test_balanced_dotdot_into_source_blocks_all_selectors(self, repo, tmp_path):
        parent = repo.parent
        balanced = parent.parent / parent.name / ".." / parent.name / repo.name
        commands = [
            f"git -C {balanced} checkout main",
            f"git --git-dir {balanced / '.git'} checkout main",
            f"git --git-dir={balanced / '.git'} --work-tree={balanced} checkout main",
            f"GIT_DIR={balanced / '.git'} GIT_WORK_TREE={balanced} git checkout main",
            f"git worktree remove {balanced}",
            f"git worktree move {balanced} {tmp_path / 'moved'}",
        ]
        for command in commands:
            hit, _ = _detect(command, tmp_path, repo)
            assert hit is True, command

    def test_dotdot_escape_outside_source_remains_allowed(self, repo, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        spelling = repo.parent / ".." / "outside"
        hit, _ = _detect(f"git -C {spelling} checkout main", tmp_path, repo)
        assert hit is False

    def test_drive_and_git_bash_spellings_are_equivalent_without_host_paths(self):
        root = Path("c:/tmp/hermes-drive-fixture")
        native = _explicit_git_path("C:/tmp/hermes-drive-fixture", Path("/"))
        git_bash = _explicit_git_path("/c/tmp/hermes-drive-fixture", Path("/"))
        expected = _resolve_for_test(root)
        assert native == git_bash == expected
        for spelling in ("C:/tmp/hermes-drive-fixture", "/c/tmp/hermes-drive-fixture"):
            hit, _ = _detect(
                f"git -C {spelling} checkout main", Path("/"), root
            )
            assert hit is True

    def test_unc_path_is_preserved_in_git_and_environment_positions(self):
        raw = r'"\\server\share\hermes-agent"'
        words = _shell_words_at(f"git -C {raw} checkout main", 0)
        assert words[2] == r"\\server\share\hermes-agent"
        env_words = _shell_words_at(f"GIT_WORK_TREE={raw} git checkout main", 0)
        assert env_words[0] == r"GIT_WORK_TREE=\\server\share\hermes-agent"

    def test_native_environment_path_preserves_separators_at_tokenization(self):
        raw = r'"D:\work\hermes-agent"'
        words = _shell_words_at(f"GIT_WORK_TREE={raw} git checkout main", 0)
        assert words[0] == r"GIT_WORK_TREE=D:\work\hermes-agent"
        assert "\\" in words[0]

    def test_mangled_windows_and_unc_forms_fail_closed(self, repo, tmp_path):
        assert _is_mangled_windows_drive("D:workhermes")
        hit, _ = _detect("git -C D:workhermes checkout main", tmp_path, repo)
        assert hit is True
        # A raw UNC operand must remain identifiable rather than becoming a
        # generic shell word whose separators can be lost.
        raw = r'"\\server\share\hermes-agent"'
        words = _shell_words_at(f"git -C {raw} checkout main", 0)
        assert words[2].startswith("\\\\server\\")

    def test_env_chdir_changes_effective_git_cwd(self, repo, tmp_path):
        for option in ("-C", "--chdir"):
            hit, _ = _detect(f"env {option} {repo} git checkout main", tmp_path, repo)
            assert hit is True
        hit, _ = _detect(f"env --chdir={repo} git checkout main", tmp_path, repo)
        assert hit is True

    def test_inline_alias_explicit_target_from_outside_blocks(self, repo, tmp_path):
        command = f'git -c "alias.co=-C {repo} checkout" co main'
        hit, _ = _detect(command, tmp_path, repo)
        assert hit is True

    def test_configured_alias_explicit_target_from_outside_blocks(
        self, repo, tmp_path, monkeypatch
    ):
        import tools.self_repo_guard as mod

        monkeypatch.setattr(
            mod, "_read_git_alias", lambda *args: f"-C {repo} checkout"
        )
        hit, _ = _detect("git co main", tmp_path, repo)
        assert hit is True


def _resolve_for_test(path: Path) -> Path:
    """Match the guard's host-normalized logical path in a test-only helper."""
    import tools.self_repo_guard as mod

    return mod._resolve(str(path), Path("/"))
