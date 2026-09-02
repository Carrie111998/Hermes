"""Reclaim must not act on — or lie about — a truncated commit graph.

`_deepen_shallow_repo`'s contract is explicit: it "returns whether the repo
is actually non-shallow afterwards; on failure (offline, no remote) callers
keep today's preserve-everything behavior." Both audits called it and
discarded the answer, then read `git branch --merged`, `rev-list --count`
and `git cherry` as fact — the verdicts that decide whether a tree is reaped
and whether a branch ref is deleted (the tree path deletes its branch too,
right after removal).

Preserving is only half of it: dropping the entries from the listing would
render as "0 reclaimable", which reads like a healthy no-op. Entries stay,
marked `keep`, with the reason.
"""

import logging

import pytest

from hermes_cli import worktree_gc


@pytest.fixture()
def stub_cli(monkeypatch):
    import cli as _cli

    calls = {"deepen": 0}

    def _set(shallow: bool, deepen_result: bool):
        monkeypatch.setattr(_cli, "_repo_is_shallow", lambda _r: shallow)

        def _deepen(_r):
            calls["deepen"] += 1
            return deepen_result

        monkeypatch.setattr(_cli, "_deepen_shallow_repo", _deepen)

    return _set, calls


def _git_returning(monkeypatch, stdout: str):
    seen = []

    def _fake(args, **kw):
        seen.append(args[0])

        class _R:
            returncode = 0
            stderr = ""

        _R.stdout = stdout if args[0] == "branch" else ""
        return _R()

    monkeypatch.setattr(worktree_gc, "_git", _fake)
    return seen


class TestTrustPredicate:
    def test_a_non_shallow_repo_needs_no_deepen(self, stub_cli):
        set_state, calls = stub_cli
        set_state(shallow=False, deepen_result=False)
        import cli as _cli

        assert worktree_gc._history_is_trustworthy("/r", _cli) is True
        assert calls["deepen"] == 0

    def test_a_successful_deepen_restores_trust(self, stub_cli):
        set_state, calls = stub_cli
        set_state(shallow=True, deepen_result=True)
        import cli as _cli

        assert worktree_gc._history_is_trustworthy("/r", _cli) is True
        assert calls["deepen"] == 1

    def test_a_failed_deepen_withholds_trust(self, stub_cli):
        set_state, calls = stub_cli
        set_state(shallow=True, deepen_result=False)
        import cli as _cli

        assert worktree_gc._history_is_trustworthy("/r", _cli) is False
        assert calls["deepen"] == 1

    def test_the_condition_is_warned_not_whispered(self, stub_cli, caplog):
        """The user asked for a reclaim; 'couldn't judge' deserves visibility."""
        set_state, _ = stub_cli
        set_state(shallow=True, deepen_result=False)
        import cli as _cli

        with caplog.at_level(logging.WARNING, logger="hermes_cli.worktree_gc"):
            worktree_gc._history_is_trustworthy("/r", _cli)

        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("still shallow" in m for m in warnings), warnings


class TestBranchAudit:
    def test_branches_are_listed_but_never_offered_for_deletion(
        self, stub_cli, monkeypatch, tmp_path
    ):
        set_state, _ = stub_cli
        set_state(shallow=True, deepen_result=False)
        _git_returning(monkeypatch, "feature/a\nfeature/b\n")

        records = worktree_gc.audit_branches(str(tmp_path))

        assert records, "branches vanished from the listing — reads as '0 reclaimable'"
        assert {r.verdict for r in records} == {"keep"}
        assert all("still shallow" in r.reason for r in records)

    def test_a_trustworthy_repo_classifies_normally(self, stub_cli, monkeypatch, tmp_path):
        set_state, _ = stub_cli
        set_state(shallow=True, deepen_result=True)
        seen = _git_returning(monkeypatch, "main\n")

        worktree_gc.audit_branches(str(tmp_path))
        assert "cherry" in seen or "branch" in seen, "classification never ran"
