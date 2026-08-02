"""Tests for `hermes scope <subcommand>` (hermes_cli/scope.py).

Mirrors hermes_cli/curator.py's argparse subcommand structure and test
conventions (see tests/hermes_cli/test_curator_status.py).
"""
import io
from contextlib import redirect_stdout, redirect_stderr

import pytest

import hermes_scope as scope
from hermes_cli.scope import cli_main


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


class TestCreate:
    def test_create_with_explicit_identity(self, hermes_home):
        rc, out, _ = _run([
            "create", "--goal", "fix the bug",
            "--platform", "discord", "--chat-id", "chan-1", "--thread-id", "thread-A",
        ])
        assert rc == 0
        assert "fix the bug" in out

    def test_create_without_identity_fails_closed(self, hermes_home):
        rc, _, err = _run(["create", "--goal", "fix the bug"])
        assert rc == 1
        assert "could not resolve scope identity" in err


class TestStatusAndAudit:
    def _create(self, hermes_home):
        identity = scope.normalize_scope_identity(
            profile="main", platform="discord", chat_id="chan-1", thread_id="thread-A",
        )
        return scope.create_scope(identity, goal="fix the bug", hermes_home=hermes_home)["scope_id"]

    def test_status_without_scope_id_or_context_fails_closed(self, hermes_home):
        rc, _, err = _run(["status"])
        assert rc == 1
        assert "scope unknown" in err

    def test_status_shows_goal_and_owned_counts(self, hermes_home):
        scope_id = self._create(hermes_home)
        scope.link_artifact(scope_id, "branches", "feat/x", hermes_home=hermes_home)
        rc, out, _ = _run(["status", "--scope-id", scope_id])
        assert rc == 0
        assert "fix the bug" in out
        assert "branches: 1" in out

    def test_status_redacts_scope_id(self, hermes_home):
        scope_id = self._create(hermes_home)
        rc, out, _ = _run(["status", "--scope-id", scope_id])
        assert rc == 0
        assert scope_id not in out

    def test_audit_shows_unredacted_identity(self, hermes_home):
        scope_id = self._create(hermes_home)
        rc, out, _ = _run(["audit", "--scope-id", scope_id])
        assert rc == 0
        assert scope_id in out
        assert "chan-1" in out
        assert "thread-A" in out


class TestLinkUnlinkDependencyLifecycle:
    def _create(self, hermes_home):
        identity = scope.normalize_scope_identity(
            profile="main", platform="discord", chat_id="chan-1", thread_id="thread-A",
        )
        return scope.create_scope(identity, goal="fix the bug", hermes_home=hermes_home)["scope_id"]

    def test_link_and_unlink(self, hermes_home):
        scope_id = self._create(hermes_home)
        rc, out, _ = _run(["link", "prs", "https://x/1", "--scope-id", scope_id])
        assert rc == 0
        assert scope.owns(scope_id, "prs", "https://x/1", hermes_home=hermes_home)

        rc, out, _ = _run(["unlink", "prs", "https://x/1", "--scope-id", scope_id])
        assert rc == 0
        assert not scope.owns(scope_id, "prs", "https://x/1", hermes_home=hermes_home)

    def test_link_unknown_category_rejected_by_argparse(self, hermes_home):
        scope_id = self._create(hermes_home)
        with pytest.raises(SystemExit):
            _run(["link", "not_a_category", "x", "--scope-id", scope_id])

    def test_dependency_recorded_separately(self, hermes_home):
        scope_id = self._create(hermes_home)
        rc, _, _ = _run(["dependency", "waiting on infra team", "--scope-id", scope_id])
        assert rc == 0
        manifest = scope.load_scope(scope_id, hermes_home=hermes_home)
        assert manifest["external_dependencies"][0]["description"] == "waiting on infra team"

    def test_complete_and_archive(self, hermes_home):
        scope_id = self._create(hermes_home)
        rc, _, _ = _run(["complete", "--scope-id", scope_id])
        assert rc == 0
        assert scope.load_scope(scope_id, hermes_home=hermes_home)["lifecycle"] == "completed"

        rc, _, _ = _run(["archive", "--scope-id", scope_id])
        assert rc == 0
        assert scope.load_scope(scope_id, hermes_home=hermes_home)["lifecycle"] == "archived"


class TestParserWiring:
    def test_register_cli_builds_all_verbs(self):
        import argparse

        from hermes_cli.scope import register_cli

        parser = argparse.ArgumentParser()
        register_cli(parser)
        # argparse raises SystemExit(2) on an unknown subcommand -- if this
        # doesn't raise, "definitely-not-a-verb" was silently accepted.
        with pytest.raises(SystemExit):
            parser.parse_args(["definitely-not-a-verb"])
        for verb in ("status", "create", "link", "unlink", "dependency", "audit", "complete", "archive"):
            if verb == "create":
                extra = ["--goal", "x", "--platform", "p", "--chat-id", "c"]
            elif verb in ("link", "unlink"):
                extra = ["branches", "val"]
            elif verb == "dependency":
                extra = ["desc"]
            else:
                extra = []
            ns = parser.parse_args([verb] + extra)
            assert ns.func is not None
