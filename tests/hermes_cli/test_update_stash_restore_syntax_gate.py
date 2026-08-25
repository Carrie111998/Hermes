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

    def _run_probe_with_broken_module(self, tmp_path, module_name):
        """Execute the EXACT production probe text in a subprocess with
        ``module_name`` shadowed by a SyntaxError-broken file placed first on
        sys.path. Deterministic: no dependence on cwd resolution, installed
        packages, or interpreter flags."""
        import subprocess
        import sys

        from hermes_cli.update_cmd import _build_import_probe

        broken_dir = tmp_path / "broken"
        parts = module_name.split(".")
        pkg = broken_dir.joinpath(*parts[:-1])
        pkg.mkdir(parents=True)
        if parts[:-1]:
            # Regular-package marker: without it the directory is only a
            # NAMESPACE portion, and a regular package of the same name
            # further down sys.path (the installed checkout) wins.
            (pkg / "__init__.py").write_text("")
        (pkg / (parts[-1] + ".py")).write_text(
            "def broken(:\n    pass\n"  # invalid: orphan-paren SyntaxError
        )

        wrapper = (
            f"import sys; sys.path.insert(0, {str(broken_dir)!r})\n"
            + _build_import_probe()
        )
        result = subprocess.run(
            [sys.executable, "-c", wrapper],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result

    def test_probe_reports_syntax_error_module(self, tmp_path):
        """A first-party critical module that raises SyntaxError on import
        must exit 3 with the error surfaced — before the fix the probe's
        ``except Exception: pass`` swallowed it and reported healthy."""
        # Shadow hermes_cli.main — the FIRST name the probe tries — so no
        # earlier iteration can import the real module into sys.modules and
        # cache-hit past the broken file.
        result = self._run_probe_with_broken_module(tmp_path, "hermes_cli.main")

        assert result.returncode == 3, (
            f"expected exit 3, got {result.returncode}; "
            f"stdout={result.stdout!r} stderr={result.stderr[-400:]!r}"
        )
        first_line = (result.stdout or "").splitlines()[0]
        assert first_line == "hermes_cli.main"
        # The error detail is surfaced (exact wording varies by Python
        # version/entrypoint: "invalid syntax", "SyntaxError: ...").
        assert (result.stdout or "").split("\n", 1)[1].strip()

    def test_probe_ignores_unrelated_exception_module(self, tmp_path):
        """Non-import failures at import time (config/env) are still benign:
        a module raising RuntimeError must NOT trip exit 3."""
        import subprocess
        import sys

        from hermes_cli.update_cmd import _build_import_probe

        broken_dir = tmp_path / "broken2"
        broken_dir.mkdir()
        (broken_dir / "toolsets.py").write_text(
            "raise RuntimeError('config env not set')\n"
        )
        wrapper = (
            f"import sys; sys.path.insert(0, {str(broken_dir)!r})\n"
            + _build_import_probe()
        )
        result = subprocess.run(
            [sys.executable, "-c", wrapper],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0
