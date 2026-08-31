"""Coverage for file/completion helper functions in ``tui_gateway/server.py``.

Exercises the module-level helpers used by the project-tree builder and the
C-command completer -- repo/junk classification, cached dir existence, repo
file listing, slash-completion ranking, absolute-path prefix sensing, and
headless CLI-exec guards.
"""

import os
import subprocess

from hermes_constants import get_hermes_home
from tui_gateway import server


# ── _is_repo_junk ────────────────────────────────────────────────────


def test_is_repo_junk_marks_non_workspace_and_hermes_home(tmp_path):
    home = os.path.expanduser("~")
    hermes_home = str(get_hermes_home())

    assert server._is_repo_junk("") is True
    assert server._is_repo_junk(home) is True
    assert server._is_repo_junk(os.sep) is True
    # HERMES_HOME itself and any descendant are config, not a workspace.
    assert server._is_repo_junk(hermes_home) is True
    assert server._is_repo_junk(os.path.join(hermes_home, "x")) is True

    # A plain temp dir (and any run-of-the-mill directory) is a valid root.
    assert server._is_repo_junk(str(tmp_path)) is False
    assert server._is_repo_junk(".git") is False
    assert server._is_repo_junk("node_modules") is False


# ── _is_session_cwd_junk ─────────────────────────────────────────────


def test_is_session_cwd_junk_marks_non_workspace_and_hermes_home(tmp_path):
    home = os.path.expanduser("~")
    hermes_home = str(get_hermes_home())

    assert server._is_session_cwd_junk("") is True
    assert server._is_session_cwd_junk(home) is True
    assert server._is_session_cwd_junk(hermes_home) is True

    # Unlike a repo root, an explicit DESCENDANT of HERMES_HOME can be a
    # legitimate prose/data workspace and must stay in flat Recents.
    assert server._is_session_cwd_junk(str(tmp_path)) is False
    assert server._is_session_cwd_junk(os.path.join(hermes_home, "x")) is False


# ── _dir_exists_cached ───────────────────────────────────────────────


def test_dir_exists_cached(tmp_path):
    server._DIR_EXISTS_CACHE.clear()
    try:
        assert server._dir_exists_cached(str(tmp_path)) is True
        assert server._dir_exists_cached(str(tmp_path / "missing")) is False

        f = tmp_path / "afile"
        f.write_text("x")
        assert server._dir_exists_cached(str(f)) is False
    finally:
        server._DIR_EXISTS_CACHE.clear()


def test_dir_exists_cached_memoizes_per_build(tmp_path):
    server._DIR_EXISTS_CACHE.clear()
    try:
        d = tmp_path / "created_later"
        assert server._dir_exists_cached(str(d)) is False
        # Created after the first read; the memo is per-build, so the value
        # stays cached until the next build clears it.
        d.mkdir()
        assert server._dir_exists_cached(str(d)) is False
        server._DIR_EXISTS_CACHE.clear()
        assert server._dir_exists_cached(str(d)) is True
    finally:
        server._DIR_EXISTS_CACHE.clear()


# ── _list_repo_files ─────────────────────────────────────────────────


def _in_junky_tree(root):
    (root / "src").mkdir()
    (root / "node_modules").mkdir()
    (root / "__pycache__").mkdir()
    (root / ".git").mkdir()
    (root / "main.py").write_text("x")
    (root / "src" / "app.py").write_text("x")
    (root / "node_modules" / "y.js").write_text("x")
    (root / "__pycache__" / "z.pyc").write_text("x")
    (root / ".git" / "config").write_text("x")
    (root / ".hidden").write_text("x")


def test_list_repo_files_walk_filters_junk_dirs(tmp_path):
    server._fuzzy_cache.clear()
    try:
        _in_junky_tree(tmp_path)
        files = server._list_repo_files(str(tmp_path))
        # node_modules / __pycache__ / dot-dirs are pruned from the walk while
        # files at the tree root and under real source dirs survive.
        assert sorted(files) == [".hidden", "main.py", "src/app.py"]
    finally:
        server._fuzzy_cache.clear()


def test_list_repo_files_git_backed(tmp_path):
    server._fuzzy_cache.clear()
    try:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        server._fuzzy_cache.clear()
        (tmp_path / "src").mkdir()
        (tmp_path / "main.py").write_text("x")
        (tmp_path / "src" / "app.py").write_text("x")
        files = server._list_repo_files(str(tmp_path))
        assert sorted(files) == ["main.py", "src/app.py"]
    finally:
        server._fuzzy_cache.clear()


def test_list_repo_files_missing_root_returns_empty(tmp_path):
    server._fuzzy_cache.clear()
    try:
        missing = str(tmp_path / "does_not_exist")
        assert server._list_repo_files(missing) == []
    finally:
        server._fuzzy_cache.clear()


# ── _abs_completion_prefix_exists ────────────────────────────────────


def test_abs_completion_prefix_exists(tmp_path):
    (tmp_path / "subdir").mkdir()
    (tmp_path / "foo.txt").write_text("x")
    (tmp_path / "subdir" / "file_x.txt").write_text("x")
    t = str(tmp_path)

    # Partial final segment matches an entry.
    assert server._abs_completion_prefix_exists(os.path.join(t, "f")) is True
    assert server._abs_completion_prefix_exists(os.path.join(t, "foo.txt")) is True
    assert server._abs_completion_prefix_exists(os.path.join(t, "subdir", "file_x")) is True

    # No matching entry, or a parent that doesn't exist.
    assert server._abs_completion_prefix_exists(os.path.join(t, "nope")) is False
    assert server._abs_completion_prefix_exists(os.path.join(t, "missing_dir", "f")) is False
    assert server._abs_completion_prefix_exists("/definitely/not/here") is False

    # A trailing slash means "the dir itself".
    assert server._abs_completion_prefix_exists(os.path.join(t, "subdir") + "/") is True


# ── _details_completion_item ─────────────────────────────────────────


def test_details_completion_item():
    assert server._details_completion_item("foo") == {
        "text": "foo",
        "display": "foo",
        "meta": "",
    }
    assert server._details_completion_item("foo", "meta") == {
        "text": "foo",
        "display": "foo",
        "meta": "meta",
    }


# ── _rank_slash_completions ──────────────────────────────────────────


def _slash_items():
    return [
        {"text": "/cmd1", "kind": "command"},
        {"text": "/cmd2", "kind": "command"},
        {"text": "/a", "kind": "skill"},
        {"text": "/b", "kind": "skill"},
        {"text": "/c", "kind": "skill"},
        {"text": "/d", "kind": "skill"},
    ]


def _slash_usage(name):
    return {"a": 5, "b": 0, "c": 3, "d": 0}.get(name, 0)


def _slash_origin(name):
    return "bundled" if name in ("a", "b") else "hub"


def test_rank_slash_completions_orders_skills_by_usage():
    ranked = server._rank_slash_completions(
        _slash_items(), _slash_usage, _slash_origin, browsing=False
    )
    assert [i["text"] for i in ranked] == ["/cmd1", "/cmd2", "/a", "/c", "/b", "/d"]


def test_rank_slash_completions_browsing_drops_bundled_unused():
    ranked = server._rank_slash_completions(
        _slash_items(), _slash_usage, _slash_origin, browsing=True
    )
    # `/b` is bundled with zero recorded usage -> noise in a browse; `/d` is
    # hub/local with zero usage but not bundled, so it survives.
    assert [i["text"] for i in ranked] == ["/cmd1", "/cmd2", "/a", "/c", "/d"]


def test_rank_slash_completions_caps_each_kind():
    items = [
        {"text": f"/cmd{i}", "kind": "command"} for i in range(40)
    ] + [{"text": f"/skill{i}", "kind": "skill"} for i in range(40)]
    ranked = server._rank_slash_completions(
        items, lambda n: 0, lambda n: "hub", browsing=False
    )
    assert len(ranked) == 60  # 30 commands + 30 skills (per-kind cap)
    assert [i["text"] for i in ranked[:3]] == ["/cmd0", "/cmd1", "/cmd2"]


def test_rank_slash_completions_score_of_leads_tie():
    items = [
        {"text": "/x", "kind": "skill"},
        {"text": "/y", "kind": "skill"},
    ]
    # Lower score = better match (exact basename beats a description hit), so
    # the scorer's ordering must lead usage/name ties. `/y` scores as the better
    # match and must come first even though names tie.
    ranked = server._rank_slash_completions(
        items,
        lambda n: 0,
        lambda n: "hub",
        browsing=False,
        score_of=lambda item: 0.0 if item["text"] == "/y" else 1.0,
    )
    assert [i["text"] for i in ranked] == ["/y", "/x"]


# ── _cli_exec_blocked ────────────────────────────────────────────────


def test_cli_exec_blocked_bare_hermes():
    hint = server._cli_exec_blocked([])
    assert hint is not None
    assert "interactive" in hint


def test_cli_exec_blocked_interactive_commands():
    assert server._cli_exec_blocked(["setup"]) is not None
    assert server._cli_exec_blocked(["gateway"]) is not None
    assert server._cli_exec_blocked(["sessions", "browse"]) is not None
    assert server._cli_exec_blocked(["config", "edit"]) is not None
    assert server._cli_exec_blocked(["config", "edit", "config.yaml"]) is not None


def test_cli_exec_blocked_case_insensitive_first_token():
    assert server._cli_exec_blocked(["Setup"]) is not None
    assert server._cli_exec_blocked(["GATEWAY"]) is not None


def test_cli_exec_blocked_allows_headless():
    assert server._cli_exec_blocked(["chat", "-q", "hi"]) is None
    assert server._cli_exec_blocked(["sessions", "list"]) is None
    assert server._cli_exec_blocked(["config", "get"]) is None
