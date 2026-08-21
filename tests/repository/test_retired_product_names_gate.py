"""Adversarial tests for the fail-closed source hygiene gate."""

import os
import subprocess
from pathlib import Path

import pytest

import scripts.check_retired_product_names as retired_names_gate
from scripts.check_retired_product_names import find_violations, tracked_entries


def _git(
    repo: Path, *args: str, input_data: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_data,
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "gate@example.invalid")
    _git(tmp_path, "config", "user.name", "Gate Test")
    return tmp_path


def _commit(repo: Path, path: str, content: bytes, message: str = "snapshot") -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    _git(repo, "add", "--", path)
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip().decode("ascii")


def _lstat_identity(path: Path) -> tuple[int, int, int]:
    info = os.lstat(path)
    return info.st_dev, info.st_ino, info.st_mode


def test_detects_both_names_case_insensitively_in_content():
    entries = [(b"safe.txt", b"prefix OpEnClAw suffix PAPERCLIP")]
    assert find_violations(entries) == [
        ("safe.txt", "content", "openclaw"),
        ("safe.txt", "content", "paperclip"),
    ]


def test_detects_names_in_paths_even_when_content_is_clean():
    entries = [(b"docs/PaperClip-notes.md", b"clean")]
    assert find_violations(entries) == [
        ("docs/PaperClip-notes.md", "path", "paperclip"),
    ]


def test_allows_clean_entries():
    assert find_violations([(b"src/module.py", b"print('clean')")]) == []


def test_only_the_two_enforcement_files_are_exempt():
    payload = b"openclaw paperclip"
    entries = [
        (b"scripts/check_retired_product_names.py", payload),
        (b"tests/repository/test_retired_product_names_gate.py", payload),
        (b"tests/repository/copy.py", payload),
    ]
    assert find_violations(entries) == [
        ("tests/repository/copy.py", "content", "openclaw"),
        ("tests/repository/copy.py", "content", "paperclip"),
    ]


def test_literal_backslash_path_does_not_bypass_exact_exemption():
    assert find_violations(
        [(b"scripts\\check_retired_product_names.py", b"openclaw")]
    ) == [
        ("scripts\\check_retired_product_names.py", "content", "openclaw"),
    ]


@pytest.mark.parametrize(
    "github_sha",
    [
        "",
        "HEAD",
        "HEAD~1",
        "refs/heads/main",
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "a" * 40 + "^{tree}",
    ],
)
def test_main_rejects_invalid_github_sha_before_git(monkeypatch, github_sha):
    called = False

    def unexpected_tracked_entries(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid GITHUB_SHA reached Git-backed scan")

    monkeypatch.setenv("GITHUB_SHA", github_sha)
    monkeypatch.setattr(retired_names_gate, "tracked_entries", unexpected_tracked_entries)

    assert retired_names_gate.main() == 2
    assert not called


def test_ci_revision_validator_rejects_nul_without_git():
    with pytest.raises(RuntimeError, match="GITHUB_SHA"):
        retired_names_gate._scan_revision({"GITHUB_SHA": "a" * 40 + "\x00"})


@pytest.mark.parametrize("github_sha", ["a" * 40, "b" * 64])
def test_main_accepts_full_lowercase_github_oid(monkeypatch, github_sha):
    seen = []

    def clean_tracked_entries(root, revision):
        seen.append(revision)
        return []

    monkeypatch.setenv("GITHUB_SHA", github_sha)
    monkeypatch.setattr(retired_names_gate, "tracked_entries", clean_tracked_entries)

    assert retired_names_gate.main() == 0
    assert seen == [github_sha]


def test_main_uses_local_head_only_when_github_sha_is_absent(monkeypatch):
    seen = []

    def clean_tracked_entries(root, revision):
        seen.append(revision)
        return []

    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(retired_names_gate, "tracked_entries", clean_tracked_entries)

    assert retired_names_gate.main() == 0
    assert seen == ["HEAD"]


def test_reads_only_the_pinned_commit_tree_not_index_or_worktree(tmp_path):
    repo = _repo(tmp_path)
    commit = _commit(repo, "safe.txt", b"clean")
    (repo / "safe.txt").write_bytes(b"openclaw in worktree")
    _git(repo, "add", "--", "safe.txt")
    (repo / "safe.txt").write_bytes(b"paperclip in worktree")

    assert tracked_entries(repo, commit) == [(b"safe.txt", b"clean")]


def test_tree_oid_is_pinned_before_ref_moves(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    original_commit = _commit(repo, "safe.txt", b"clean", "original")
    original_run_git = retired_names_gate._run_git
    moved = False

    def move_head_before_tree_read(root, *args, **kwargs):
        nonlocal moved
        if args[:1] == ("ls-tree",) and not moved:
            _commit(repo, "safe.txt", b"openclaw", "replacement")
            moved = True
        return original_run_git(root, *args, **kwargs)

    monkeypatch.setattr(retired_names_gate, "_run_git", move_head_before_tree_read)
    assert tracked_entries(repo, "HEAD") == [(b"safe.txt", b"clean")]
    assert moved
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() != original_commit.encode()


def test_disables_git_replace_objects(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "safe.txt", b"clean")
    clean_blob = _git(repo, "rev-parse", "HEAD:safe.txt").stdout.strip()
    replacement_blob = _git(
        repo, "hash-object", "-w", "--stdin", input_data=b"openclaw"
    ).stdout.strip()
    _git(repo, "replace", clean_blob.decode(), replacement_blob.decode())

    assert tracked_entries(repo) == [(b"safe.txt", b"clean")]


@pytest.mark.parametrize("lock_kind", ["regular", "hardlink", "symlink", "dangling"])
def test_never_interacts_with_preexisting_index_lock(tmp_path, lock_kind):
    repo = _repo(tmp_path / "repo")
    _commit(repo, "safe.txt", b"clean")
    lock = repo / ".git" / "index.lock"
    target = tmp_path / "foreign-target"
    target.write_bytes(b"other operation")
    if lock_kind == "regular":
        lock.write_bytes(b"other operation")
    elif lock_kind == "hardlink":
        os.link(target, lock)
    elif lock_kind == "symlink":
        lock.symlink_to(target)
    else:
        lock.symlink_to(tmp_path / "missing-target")
    before = _lstat_identity(lock)

    assert tracked_entries(repo) == [(b"safe.txt", b"clean")]
    assert _lstat_identity(lock) == before
    if lock_kind == "dangling":
        assert lock.is_symlink() and not lock.exists()
    else:
        assert lock.read_bytes() == b"other operation"


def test_lock_replacement_during_scan_survives_at_index_lock(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    _commit(repo, "safe.txt", b"clean")
    lock = repo / ".git" / "index.lock"
    lock.write_bytes(b"first operation")
    replacement = tmp_path / "replacement-lock"
    replacement.write_bytes(b"second operation")
    replacement_identity = _lstat_identity(replacement)
    original_run_git = retired_names_gate._run_git
    replaced = False

    def replace_before_blob_read(root, *args, **kwargs):
        nonlocal replaced
        if args[:2] == ("cat-file", "--batch") and not replaced:
            os.replace(replacement, lock)
            replaced = True
        return original_run_git(root, *args, **kwargs)

    monkeypatch.setattr(retired_names_gate, "_run_git", replace_before_blob_read)
    assert tracked_entries(repo) == [(b"safe.txt", b"clean")]
    assert replaced
    assert _lstat_identity(lock) == replacement_identity
    assert lock.read_bytes() == b"second operation"


def test_explicit_commit_oid_remains_authoritative_after_head_moves(tmp_path):
    repo = _repo(tmp_path)
    clean_commit = _commit(repo, "safe.txt", b"clean", "clean")
    _commit(repo, "safe.txt", b"openclaw", "forbidden")

    assert find_violations(tracked_entries(repo, clean_commit)) == []
    assert find_violations(tracked_entries(repo, "HEAD")) == [
        ("safe.txt", "content", "openclaw")
    ]


def test_reads_symlink_blob_from_tree_without_following_target(tmp_path):
    repo = _repo(tmp_path)
    os.symlink("openclaw-target", repo / "safe-link")
    _git(repo, "add", "safe-link")
    _git(repo, "commit", "-qm", "symlink")

    assert find_violations(tracked_entries(repo)) == [
        ("safe-link", "content", "openclaw")
    ]


def test_rejects_gitlinks(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "seed", b"clean")
    oid = _git(repo, "rev-parse", "HEAD").stdout.strip().decode("ascii")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},nested")
    _git(repo, "commit", "-qm", "gitlink")

    with pytest.raises(RuntimeError, match="gitlink.*nested"):
        tracked_entries(repo)


def test_invalid_revision_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "safe.txt", b"clean")

    with pytest.raises(RuntimeError, match="Git command failed|cannot resolve"):
        tracked_entries(repo, "refs/heads/missing")


@pytest.mark.parametrize("payload", [b"safe\x00openclaw", b"safe\xffopenclaw"])
def test_rejects_forbidden_name_in_binary_or_invalid_utf8_blob(tmp_path, payload):
    repo = _repo(tmp_path)
    _commit(repo, "safe.bin", payload)

    assert find_violations(tracked_entries(repo)) == [
        ("safe.bin", "content", "openclaw")
    ]
