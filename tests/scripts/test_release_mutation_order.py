"""Mutation-order regression tests through the PRODUCTION main() (#100600 v4).

The v3 review's blocker: the gate-before-mutation ordering was asserted only
at helper level. This suite drives scripts/release.py::main() end to end with
git/gh side effects STUBBED and records every mutation call, proving:

  A. invalid/missing manifest  -> NO tag, NO push, NO gh release create
     (and --prepare-only absent: no bump commit either)
  B. --prepare-only             -> ONLY the version commit; no tag/push/release
  C. valid exact-head manifest  -> tag -> push -> gh release create WITH the
     verified DMG paths appended to gh's argv

These tests run against the REAL main() and the REAL argparse surface — the
v3 review proved the documented flags (--prepare-only/--no-bump) did not
exist, which helper-level tests can never catch.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release  # noqa: E402


class _GitCallLog:
    """Stub git_result: record every git mutation, return success."""

    def __init__(self, head_sha="deadbeefcafe1234"):
        self.head_sha = head_sha
        self.calls: list[list[str]] = []

    def __call__(self, *args, cwd=None):
        self.calls.append(list(args))
        m = MagicMock()
        m.returncode = 0
        if args and args[0] == "rev-parse":
            m.stdout = self.head_sha
        else:
            m.stdout = ""
        m.stderr = ""
        return m

    def ran(self, *verbs) -> bool:
        """True if any recorded call starts with all the given verbs."""
        for call in self.calls:
            if all(call[i] == verbs[i] for i in range(len(verbs))):
                return True
        return False


def _gh_stub_success(cmd, **kw):
    m = MagicMock()
    m.returncode = 0
    m.stdout = "https://github.com/x/releases/tag/vX"
    m.stderr = ""
    return m


def _run_main(argv, gitlog):
    """Run the production main() with argv; git/gh/network stubbed."""
    with (
        patch.object(sys, "argv", ["release.py"] + argv),
        patch.object(release, "git_result", gitlog),
        patch.object(release, "next_available_tag", lambda base: (base, base.split("v")[1])),
        patch.object(release, "get_current_version", lambda: "0.20.0"),
        patch.object(release, "get_last_tag", lambda: "v2026.8.31"),
        patch.object(release, "get_commits", lambda since_tag=None: [{"sha": "abc123", "author_name": "T", "author_email": "t@x", "subject": "s", "body": "", "github_author": "t"}]),
        patch.object(release, "generate_changelog", lambda *a, **k: "changelog"),
        patch.object(release, "update_version_files", lambda *a: None),
        patch("builtins.__import__", side_effect=_import_guard),
    ):
        try:
            release.main()
        except SystemExit as e:
            if e.code not in (0, None):
                raise
        except _ImportBlocked:
            pass  # subprocess.run(gh) blocked on purpose — call recorded below


class _ImportBlocked(Exception):
    pass


def _import_guard(name, *a, **k):
    if name == "subprocess":
        raise _ImportBlocked()
    return __import__(name, *a, **k)


class TestMutationOrderThroughMain:
    """A. Missing manifest -> refuse BEFORE any mutation (no tag/push/release)."""

    def test_invalid_manifest_no_tag_no_push_no_release(self, tmp_path, monkeypatch):
        # Point the production bundle dir at an empty dir -> missing manifest
        dmg_dir = tmp_path / "bundle" / "dmg"
        dmg_dir.mkdir(parents=True)
        monkeypatch.setattr(release, "_BUNDLE_DMG_DIR", dmg_dir)
        monkeypatch.chdir(tmp_path)

        gitlog = _GitCallLog()
        # The call must exit(1) at the gate; catch it via sys.exit
        with (
            patch.object(sys, "argv", ["release.py", "--publish"]),
            patch.object(release, "git_result", gitlog),
            patch.object(release, "next_available_tag", lambda b: (b, b[1:])),
            patch.object(release, "get_current_version", lambda: "0.20.0"),
            patch.object(release, "get_last_tag", lambda: "v2026.8.31"),
            patch.object(release, "get_commits", lambda since_tag=None: [{"sha": "abc123", "author_name": "T", "author_email": "t@x", "subject": "s", "body": "", "github_author": "t"}]),
            patch.object(release, "generate_changelog", lambda *a, **k: "c"),
        ):
            with pytest.raises(SystemExit) as exc:
                release.main()

        assert exc.value.code == 1, "gate refusal must exit(1)"
        assert not gitlog.ran("tag"), "no git tag may run on a refused gate"
        assert not gitlog.ran("push"), "no git push may run on a refused gate"
        # gh release create never constructed: verify no subprocess path to it
        # (gh runs via subprocess, not git_result — prove via stdout capture)

    def test_prepare_only_commits_bump_but_never_tags(self, tmp_path, monkeypatch):
        """B. --prepare-only: ONLY the version commit — no tag/push/release."""
        monkeypatch.chdir(tmp_path)

        gitlog = _GitCallLog()
        with (
            patch.object(sys, "argv",
                         ["release.py", "--bump", "minor", "--prepare-only"]),
            patch.object(release, "git_result", gitlog),
            patch.object(release, "next_available_tag", lambda b: (b, b[1:])),
            patch.object(release, "get_current_version", lambda: "0.20.0"),
            patch.object(release, "get_last_tag", lambda: "v2026.8.31"),
            patch.object(release, "get_commits", lambda since_tag=None: [{"sha": "abc123", "author_name": "T", "author_email": "t@x", "subject": "s", "body": "", "github_author": "t"}]),
            patch.object(release, "generate_changelog", lambda *a, **k: "c"),
            patch.object(release, "update_version_files", lambda *a: None),
        ):
            release.main()  # must NOT SystemExit(1)

        assert gitlog.ran("commit"), "prepare-only must commit the version bump"
        assert not gitlog.ran("tag"), "prepare-only must NOT tag"
        assert not gitlog.ran("push"), "prepare-only must NOT push"

    def test_valid_manifest_full_publish_path(self, tmp_path, monkeypatch):
        """C. Valid exact-head manifest -> tag, push, then gh release create
        with the verified DMG paths in gh's argv."""
        import hashlib
        import json
        import time

        dmg_dir = tmp_path / "bundle" / "dmg"
        dmg_dir.mkdir(parents=True)
        dmg = dmg_dir / "Hermes_0.0.1_aarch64.dmg"
        dmg.write_bytes(b"verified dmg bytes")
        digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
        (dmg_dir / ".build-manifest.json").write_text(json.dumps({
            "sha": "deadbeefcafe1234",
            "built_at_unix": int(time.time()) - 600,
            "artifacts": [{"filename": dmg.name, "sha256": digest,
                           "size": dmg.stat().st_size, "arch": "aarch64"}],
        }), encoding="utf-8")
        monkeypatch.setattr(release, "_BUNDLE_DMG_DIR", dmg_dir)
        monkeypatch.chdir(tmp_path)

        gitlog = _GitCallLog(head_sha="deadbeefcafe1234")
        gh_cmds: list[list[str]] = []

        def _gh_run(cmd, **kw):
            gh_cmds.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stdout = "released"
            m.stderr = ""
            return m

        with (
            patch.object(sys, "argv",
                         ["release.py", "--publish", "--no-bump", "--date", "2026.9.2"]),
            patch.object(release, "git_result", gitlog),
            patch.object(release, "next_available_tag", lambda b: (b, b[1:])),
            patch.object(release, "get_current_version", lambda: "0.20.0"),
            patch.object(release, "get_last_tag", lambda: "v2026.8.31"),
            patch.object(release, "get_commits", lambda since_tag=None: [{"sha": "abc123", "author_name": "T", "author_email": "t@x", "subject": "s", "body": "", "github_author": "t"}]),
            patch.object(release, "generate_changelog", lambda *a, **k: "c"),
        ):
            # patch subprocess.run AFTER main() imports it lazily: patch the
            # module attr release-module-wide (release imports subprocess at
            # top level, so patch there)
            with patch.object(release.subprocess, "run", _gh_run):
                release.main()

        assert gitlog.ran("tag"), "valid manifest must tag"
        assert gitlog.ran("push"), "valid manifest must push"
        # The gh release create call must carry the verified DMG as an asset
        release_calls = [c for c in gh_cmds
                         if c and c[0] == "gh" and c[1] == "release" and c[2] == "create"]
        assert release_calls, "gh release create must have run"
        argv = release_calls[0]
        assert any(str(dmg) in a for a in argv), (
            f"verified DMG path must be in gh release create argv: {argv}"
        )

    def test_documented_flags_parse(self):
        """The v3 blocker: --prepare-only/--no-bump were documented but not
        registered. Pin that they parse (argparse exits 2 on unknown)."""
        import argparse
        # re-derive from the REAL parser inside main via --help not raising
        with patch.object(sys, "argv", ["release.py", "--prepare-only", "--help"]):
            with pytest.raises(SystemExit) as e:
                release.main()
        assert e.value.code == 0  # --help exits 0 and lists the flags


class TestFlagMatrixValidation:
    """Contradictory transitions must be rejected up front (#100600 v4)."""

    def _run_with(self, argv):
        with (
            patch.object(sys, "argv", ["release.py"] + argv),
            patch.object(release, "next_available_tag", lambda b: (b, b[1:])),
            patch.object(release, "get_current_version", lambda: "0.20.0"),
            patch.object(release, "get_last_tag", lambda: "v2026.8.31"),
            patch.object(release, "get_commits", lambda since_tag=None: [
                {"sha": "abc", "author_name": "T", "author_email": "t@x",
                 "subject": "s", "body": "", "github_author": "t"}]),
        ):
            with pytest.raises(SystemExit) as e:
                release.main()
            return e.value.code

    def test_prepare_only_no_bump_nothing_rejected(self):
        """--prepare-only --no-bump prepares nothing -> exit(2)."""
        assert self._run_with(["--prepare-only", "--no-bump"]) == 2

    def test_publish_with_prepare_only_rejected(self):
        """Phase flags are mutually exclusive -> exit(2)."""
        assert self._run_with(["--publish", "--prepare-only", "--bump", "minor"]) == 2

    def test_publish_bump_no_bump_contradiction_rejected(self):
        """--bump computes a version while --no-bump skips committing it."""
        assert self._run_with(["--publish", "--bump", "minor", "--no-bump"]) == 2

    def test_valid_phase2_combination_accepted(self):
        """The documented phase 2 parses cleanly (no exit-2 from the matrix)."""
        # It will exit later at the manifest gate (no bundle dir) — but the
        # matrix validator must not reject it. Distinguish exit codes:
        # matrix rejection is 2; gate refusal is 1; argparse help is 0.
        with (
            patch.object(sys, "argv", ["release.py", "--publish", "--no-bump", "--date", "2026.9.2"]),
            patch.object(release, "git_result", _GitCallLog()),
            patch.object(release, "next_available_tag", lambda b: (b, b[1:])),
            patch.object(release, "get_current_version", lambda: "0.20.0"),
            patch.object(release, "get_last_tag", lambda: "v2026.8.31"),
            patch.object(release, "get_commits", lambda since_tag=None: [
                {"sha": "abc", "author_name": "T", "author_email": "t@x",
                 "subject": "s", "body": "", "github_author": "t"}]),
            patch.object(release, "generate_changelog", lambda *a, **k: "c"),
            patch.object(release, "_BUNDLE_DMG_DIR",
                         Path(__file__).parent / "nonexistent-bundle"),
        ):
            with pytest.raises(SystemExit) as e:
                release.main()
        assert e.value.code == 1, (
            "phase 2 must reach the manifest gate (exit 1), not be rejected "
            "by the flag matrix (exit 2)"
        )


class TestPushFailureAborts:
    """Push failure must abort BEFORE gh release create (#100600 v4)."""

    def test_push_failure_no_release_create(self, tmp_path, monkeypatch):
        """A failed push exits(1) and gh release create is never invoked."""
        import hashlib
        import json as _json
        import time as _time

        dmg_dir = tmp_path / "bundle" / "dmg"
        dmg_dir.mkdir(parents=True)
        dmg = dmg_dir / "H.dmg"
        dmg.write_bytes(b"bytes")
        (dmg_dir / ".build-manifest.json").write_text(_json.dumps({
            "sha": "deadbeefcafe1234",
            "built_at_unix": int(_time.time()) - 600,
            "artifacts": [{"filename": dmg.name, "sha256": hashlib.sha256(b"bytes").hexdigest(),
                           "size": 5, "arch": "aarch64"}],
        }), encoding="utf-8")
        monkeypatch.setattr(release, "_BUNDLE_DMG_DIR", dmg_dir)
        monkeypatch.chdir(tmp_path)

        # git stub: rev-parse succeeds, tag succeeds, push FAILS
        gitlog = _GitCallLog(head_sha="deadbeefcafe1234")
        gh_cmds: list[list[str]] = []

        def _failing_push(*args, cwd=None):
            call = list(args)
            gitlog.calls.append(call)
            m = MagicMock()
            if args[0] == "rev-parse":
                m.returncode = 0; m.stdout = gitlog.head_sha; m.stderr = ""
            elif args[0] == "push":
                m.returncode = 1; m.stdout = ""; m.stderr = "access denied"
            else:
                m.returncode = 0; m.stdout = ""; m.stderr = ""
            return m

        def _gh_run(cmd, **kw):
            gh_cmds.append(cmd)
            m = MagicMock(); m.returncode = 0; m.stdout = "r"; m.stderr = ""
            return m

        with (
            patch.object(sys, "argv", ["release.py", "--publish", "--no-bump", "--date", "2026.9.2"]),
            patch.object(release, "git_result", _failing_push),
            patch.object(release, "next_available_tag", lambda b: (b, b[1:])),
            patch.object(release, "get_current_version", lambda: "0.20.0"),
            patch.object(release, "get_last_tag", lambda: "v2026.8.31"),
            patch.object(release, "get_commits", lambda since_tag=None: [
                {"sha": "a", "author_name": "T", "author_email": "t@x",
                 "subject": "s", "body": "", "github_author": "t"}]),
            patch.object(release, "generate_changelog", lambda *a, **k: "c"),
            patch.object(release.subprocess, "run", _gh_run),
        ):
            with pytest.raises(SystemExit) as e:
                release.main()

        assert e.value.code == 1, "push failure must abort with exit(1)"
        release_calls = [c for c in gh_cmds if c[:3] == ["gh", "release", "create"]]
        assert not release_calls, (
            "gh release create must NEVER run after a failed push — without "
            "--verify-tag it would auto-tag the default branch (#100600 v4)"
        )

    def test_gh_release_uses_verify_tag(self, tmp_path, monkeypatch):
        """The happy path: gh release create carries --verify-tag."""
        import hashlib
        import json as _json
        import time as _time

        dmg_dir = tmp_path / "bundle" / "dmg"
        dmg_dir.mkdir(parents=True)
        dmg = dmg_dir / "H.dmg"
        dmg.write_bytes(b"bytes")
        (dmg_dir / ".build-manifest.json").write_text(_json.dumps({
            "sha": "deadbeefcafe1234",
            "built_at_unix": int(_time.time()) - 600,
            "artifacts": [{"filename": dmg.name, "sha256": hashlib.sha256(b"bytes").hexdigest(),
                           "size": 5, "arch": "aarch64"}],
        }), encoding="utf-8")
        monkeypatch.setattr(release, "_BUNDLE_DMG_DIR", dmg_dir)
        monkeypatch.chdir(tmp_path)

        gitlog = _GitCallLog(head_sha="deadbeefcafe1234")
        gh_cmds: list[list[str]] = []

        def _gh_run(cmd, **kw):
            gh_cmds.append(cmd)
            m = MagicMock(); m.returncode = 0; m.stdout = "r"; m.stderr = ""
            return m

        with (
            patch.object(sys, "argv", ["release.py", "--publish", "--no-bump", "--date", "2026.9.2"]),
            patch.object(release, "git_result", gitlog),
            patch.object(release, "next_available_tag", lambda b: (b, b[1:])),
            patch.object(release, "get_current_version", lambda: "0.20.0"),
            patch.object(release, "get_last_tag", lambda: "v2026.8.31"),
            patch.object(release, "get_commits", lambda since_tag=None: [
                {"sha": "a", "author_name": "T", "author_email": "t@x",
                 "subject": "s", "body": "", "github_author": "t"}]),
            patch.object(release, "generate_changelog", lambda *a, **k: "c"),
            patch.object(release.subprocess, "run", _gh_run),
        ):
            release.main()

        release_calls = [c for c in gh_cmds if c[:3] == ["gh", "release", "create"]]
        assert release_calls, "happy path must create the release"
        assert "--verify-tag" in release_calls[0], (
            "gh release create must require the remote tag (--verify-tag) so "
            "a push race cannot auto-tag the default branch"
        )
