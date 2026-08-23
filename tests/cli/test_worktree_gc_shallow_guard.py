"""Branch reclaim must not act on a truncated commit graph.

`_deepen_shallow_repo`'s contract is explicit: it "returns whether the repo
is actually non-shallow afterwards; on failure (offline, no remote) callers
keep today's preserve-everything behavior." `audit_branches` called it and
discarded the answer, then read `git branch --merged`, `rev-list --count`
and `git cherry` as fact.

The tree path survives that: `_worktree_has_unpushed_commits` answers
conservatively True when history is unverifiable, and its docstring tells
callers to "check `_repo_is_shallow` before presenting this verdict as
fact". Branch reclaim had no such backstop, and deleting a branch ref is
what makes its commits unreachable.
"""

import pytest

from hermes_cli import worktree_gc


@pytest.fixture()
def stub_cli(monkeypatch):
    """Stand in for the lazily-imported `cli` module."""
    import cli as _cli

    calls = {"deepen": 0}

    def _set(shallow: bool, deepen_result: bool):
        monkeypatch.setattr(_cli, "_repo_is_shallow", lambda _r: shallow)

        def _deepen(_r):
            calls["deepen"] += 1
            return deepen_result

        monkeypatch.setattr(_cli, "_deepen_shallow_repo", _deepen)

    return _set, calls


def _no_git(monkeypatch):
    """Any git call after the guard would mean we proceeded — fail loudly."""
    def _boom(*a, **k):
        raise AssertionError("classification ran on an unverifiable graph")

    monkeypatch.setattr(worktree_gc, "_git", _boom)


class TestShallowGuard:
    def test_a_repo_that_stays_shallow_offers_nothing(self, stub_cli, monkeypatch, tmp_path):
        set_state, calls = stub_cli
        set_state(shallow=True, deepen_result=False)
        _no_git(monkeypatch)

        assert worktree_gc.audit_branches(str(tmp_path)) == [], (
            "branch reclaim classified branches against a truncated history"
        )
        assert calls["deepen"] == 1, "the deepen attempt should still be made"

    def test_a_successful_deepen_proceeds(self, stub_cli, monkeypatch, tmp_path):
        set_state, calls = stub_cli
        set_state(shallow=True, deepen_result=True)
        seen = []

        def _fake_git(args, **kw):
            seen.append(args[0])
            class _R:
                returncode = 1
                stdout = ""
            return _R()

        monkeypatch.setattr(worktree_gc, "_git", _fake_git)

        worktree_gc.audit_branches(str(tmp_path))
        assert calls["deepen"] == 1
        assert seen, "a deepened repo must be classified normally"

    def test_a_non_shallow_repo_never_attempts_to_deepen(self, stub_cli, monkeypatch, tmp_path):
        set_state, calls = stub_cli
        set_state(shallow=False, deepen_result=False)
        seen = []

        def _fake_git(args, **kw):
            seen.append(args[0])
            class _R:
                returncode = 1
                stdout = ""
            return _R()

        monkeypatch.setattr(worktree_gc, "_git", _fake_git)

        worktree_gc.audit_branches(str(tmp_path))
        assert calls["deepen"] == 0
        assert seen, "a normal repo must still be classified"

    def test_the_skip_is_reported(self, stub_cli, monkeypatch, tmp_path, caplog):
        """Silently offering nothing would look like 'no branches to reclaim'."""
        import logging

        set_state, _ = stub_cli
        set_state(shallow=True, deepen_result=False)
        _no_git(monkeypatch)

        with caplog.at_level(logging.INFO, logger="hermes_cli.worktree_gc"):
            worktree_gc.audit_branches(str(tmp_path))

        messages = [r.getMessage() for r in caplog.records]
        assert any("still shallow" in m for m in messages), messages
