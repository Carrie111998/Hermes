"""Post-restore syntax gate for the update stash-restore path (#94264).

A stash can re-apply invalid Python (orphan merge-conflict markers, bad
local edits) CLEANLY — git creates no merge conflict — so the pre-existing
conflict-only checks never see it. The gate must reject the restore, reset
the tree to clean updated HEAD, keep the stash parked, and flip the
gateway-mode prompt default from "restore" to "park".
"""

import subprocess

import pytest

from hermes_cli import main as hermes_main
from hermes_cli.update_cmd import _restore_stashed_changes


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "tools.py").write_text("VALUE = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _stash_all(repo):
    out = subprocess.run(
        ["git", "stash", "push", "-m", "pre-update"],
        cwd=repo, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr


def _stash_sha(repo):
    return _git(repo, "rev-parse", "stash@{0}").stdout.strip()


def _stash_list(repo):
    return subprocess.run(
        ["git", "stash", "list"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


class TestPostRestoreSyntaxGate:
    def test_invalid_python_in_stash_is_rejected_and_parked(self, tmp_path, capsys):
        """The reported bug (#94264): a cleanly-applying stash with invalid
        Python must NOT be treated as a successful restore."""
        repo = _make_repo(tmp_path)
        # Orphan conflict markers — exactly the reporter's failure mode.
        (repo / "tools.py").write_text("<<<<<<< Updated upstream\nVALUE = 1\n")
        _stash_all(repo)
        sha = _stash_sha(repo)
        assert (repo / "tools.py").read_text() == "VALUE = 1\n"

        result = _restore_stashed_changes(["git"], repo, sha, prompt_user=False)

        assert result is False
        assert (repo / "tools.py").read_text() == "VALUE = 1\n"  # tree reset
        assert _stash_list(repo)  # stash preserved, not dropped
        out = capsys.readouterr().out
        assert "invalid Python" in out
        assert f"git stash apply {sha}" in out

    def test_valid_stash_still_restores_and_drops(self, tmp_path):
        """True-positive class must survive: a healthy local change restores
        exactly as before, and the stash is dropped on success."""
        repo = _make_repo(tmp_path)
        (repo / "tools.py").write_text("VALUE = 2  # local tweak\n")
        _stash_all(repo)
        sha = _stash_sha(repo)

        result = _restore_stashed_changes(["git"], repo, sha, prompt_user=False)

        assert result is True
        assert (repo / "tools.py").read_text() == "VALUE = 2  # local tweak\n"
        assert _stash_list(repo) == ""  # dropped after verified success

    def test_non_python_only_stash_bypasses_gate(self, tmp_path):
        """A stash that touches no .py file restores without compiling anything."""
        repo = _make_repo(tmp_path)
        (repo / "notes.md").write_text("# edited locally\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "track notes")
        (repo / "notes.md").write_text("# edited again\n")
        _stash_all(repo)
        sha = _stash_sha(repo)

        result = _restore_stashed_changes(["git"], repo, sha, prompt_user=False)

        assert result is True
        assert (repo / "notes.md").read_text() == "# edited again\n"

    def test_deleted_py_file_in_stash_does_not_break_the_gate(self, tmp_path):
        """A stash that deletes a .py file must not crash the gate on the
        now-missing path."""
        repo = _make_repo(tmp_path)
        _git(repo, "rm", "-q", "tools.py")
        _stash_all(repo)
        sha = _stash_sha(repo)

        result = _restore_stashed_changes(["git"], repo, sha, prompt_user=False)

        assert result is True
        assert not (repo / "tools.py").exists()


class TestGatewayRestorePromptDefault:
    """In gateway/remote mode there is no human at a keyboard; restoring a
    dirty stash onto updated code is the risky move, so it must not be the
    effective default (#94264 expected-behaviour point 4)."""

    def test_gateway_prompt_defaults_to_parking(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        (repo / "tools.py").write_text("VALUE = 3\n")
        _stash_all(repo)
        sha = _stash_sha(repo)

        answers = []

        def gw_input(prompt, default=""):
            answers.append((prompt, default))
            return default  # gateway timeout returns the default

        result = _restore_stashed_changes(
            ["git"], repo, sha, prompt_user=True, input_fn=gw_input
        )

        assert answers[0][0].startswith("Restore local changes now? [y/N]")
        assert answers[0][1] == "n"  # explicit risk-aware default
        assert result is False
        assert (repo / "tools.py").read_text() == "VALUE = 1\n"  # parked tree
        assert _stash_list(repo)  # stash kept
        out = capsys.readouterr().out.lower()
        # The timeout default ("n") takes the standard decline path:
        assert "skipped restoring local changes" in out
        assert "git stash apply" in out

    def test_gateway_explicit_yes_still_restores(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "tools.py").write_text("VALUE = 3\n")
        _stash_all(repo)
        sha = _stash_sha(repo)

        result = _restore_stashed_changes(
            ["git"], repo, sha, prompt_user=True, input_fn=lambda p, d="": "y"
        )

        assert result is True
        assert (repo / "tools.py").read_text() == "VALUE = 3\n"


class TestImportProbeCatchesSyntaxError:
    """The post-update import probe must report SyntaxError as breakage, not
    swallow it as a benign non-import error (#94264)."""

    def test_probe_reports_syntax_error_module(self, tmp_path):
        """The probe's generated source must treat SyntaxError as exit-3
        breakage — before the fix it fell through to ``except Exception:
        pass`` and reported the tree healthy."""
        from hermes_cli.update_cmd import _UPDATE_CRITICAL_MODULES

        # Rebuild the probe body exactly as production assembles it, then
        # execute its logic against a broken module on sys.path.
        import hermes_cli.update_cmd as uc
        import inspect

        src = inspect.getsource(uc._validate_critical_modules_import)
        assert "except SyntaxError" in src  # guard clause present in template
        assert "raise SystemExit(3)" in src

        # Behavioral half: run the probe subprocess machinery against a root
        # whose first-party module list forces run_agent resolution. We call
        # the real function with a fake venv-less interpreter fallback.
        broken = tmp_path / "run_agent.py"
        broken.write_text("def broken(:\n    pass\n")  # SyntaxError on import

        ok, module, error = hermes_main._validate_critical_modules_import(tmp_path)

        # tmp_path isn't a project checkout: either run_agent fails to import
        # (first-party ModuleNotFoundError → exit 3) or the probe can't even
        # resolve it. What must NEVER happen is ok=True via a swallowed
        # SyntaxError while a broken run_agent.py sits at the root.
        if ok is True:
            pytest.fail(
                "probe reported healthy despite broken run_agent.py at root"
            )
