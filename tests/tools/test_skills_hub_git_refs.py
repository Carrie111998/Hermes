"""Focused coverage for immutable GitHub skill installs."""

from unittest.mock import MagicMock

import pytest

from tools.skills_hub import GitHubAuth, GitHubSource, HubLockFile


FULL_SHA = "a" * 40


def _response(payload, *, content=b""):
    response = MagicMock(status_code=200, content=content)
    response.json.return_value = payload
    return response


def test_fetch_full_sha_uses_sha_tree_and_records_checkout_metadata(monkeypatch):
    source = GitHubSource(auth=GitHubAuth())
    calls = []

    def github_get(url, **kwargs):
        calls.append(url)
        if url.endswith(f"/git/trees/{FULL_SHA}"):
            return _response({"sha": FULL_SHA, "truncated": False, "tree": [
                {"path": "skills/demo/SKILL.md", "type": "blob", "mode": "100644", "sha": "skill"},
                {"path": "skills/demo/scripts/run.py", "type": "blob", "mode": "100644", "sha": "script"},
            ]})
        if url.endswith("/git/blobs/skill"):
            return _response({}, content=b"---\nname: demo\n---\n# Demo\n")
        if url.endswith("/git/blobs/script"):
            return _response({}, content=b"print('ok')\n")
        raise AssertionError(url)

    monkeypatch.setattr(source, "_github_get", github_get)
    bundle = source.fetch("owner/repo/skills/demo", ref=FULL_SHA)

    assert bundle is not None
    assert set(bundle.files) == {"SKILL.md", "scripts/run.py"}
    assert bundle.metadata["git"]["selector_type"] == "commit"
    assert bundle.metadata["git"]["requested_selector"] == FULL_SHA
    assert bundle.metadata["git"]["resolved_sha"] == FULL_SHA
    assert "resolved_at" in bundle.metadata["git"]
    assert all("/contents/" not in call for call in calls)


def test_fetch_branch_resolves_once_then_reads_immutable_sha_tree(monkeypatch):
    source = GitHubSource(auth=GitHubAuth())
    calls = []

    def github_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/commits/release/v1"):
            return _response({"sha": FULL_SHA})
        if url.endswith(f"/git/trees/{FULL_SHA}"):
            return _response({"truncated": False, "tree": [
                {"path": "demo/SKILL.md", "type": "blob", "mode": "100644", "sha": "skill"},
            ]})
        if url.endswith("/git/blobs/skill"):
            return _response({}, content=b"# Demo")
        raise AssertionError(url)

    monkeypatch.setattr(source, "_github_get", github_get)
    bundle = source.fetch("owner/repo/demo", ref="release/v1")

    assert bundle is not None
    assert bundle.metadata["git"]["selector_type"] == "ref"
    assert bundle.metadata["git"]["requested_selector"] == "release/v1"
    assert bundle.metadata["git"]["resolved_sha"] == FULL_SHA
    assert sum("/commits/release/v1" in call for call in calls) == 1


@pytest.mark.parametrize("short_sha", ["a" * 7, "A" * 39])
def test_fetch_rejects_short_commit_sha(short_sha):
    source = GitHubSource(auth=GitHubAuth())
    with pytest.raises(ValueError, match="full 40-character commit SHA"):
        source.fetch("owner/repo/demo", ref=short_sha)


def test_fetch_pr_locks_same_repo_head_provenance(monkeypatch):
    source = GitHubSource(auth=GitHubAuth())

    def github_get(url, **kwargs):
        if url.endswith("/pulls/42"):
            return _response({"number": 42, "html_url": "https://github.com/owner/repo/pull/42", "head": {
                "sha": FULL_SHA, "ref": "feature", "label": "owner:feature",
                "repo": {"full_name": "owner/repo"},
            }})
        if url.endswith(f"/git/trees/{FULL_SHA}"):
            return _response({"truncated": False, "tree": [
                {"path": "demo/SKILL.md", "type": "blob", "mode": "100644", "sha": "skill"},
            ]})
        if url.endswith("/git/blobs/skill"):
            return _response({}, content=b"# Demo")
        raise AssertionError(url)

    monkeypatch.setattr(source, "_github_get", github_get)
    bundle = source.fetch("owner/repo/demo", pr="https://github.com/owner/repo/pull/42")

    assert bundle is not None
    git = bundle.metadata["git"]
    assert git["selector_type"] == "pr"
    assert git["requested_selector"] == "https://github.com/owner/repo/pull/42"
    assert git["pr"]["number"] == 42
    assert git["pr"]["head_repo"] == "owner/repo"
    assert git["pr"]["head_sha"] == FULL_SHA


def test_fetch_rejects_fork_pr_by_default(monkeypatch):
    source = GitHubSource(auth=GitHubAuth())
    monkeypatch.setattr(source, "_github_get", lambda *_args, **_kwargs: _response({
        "head": {"sha": FULL_SHA, "repo": {"full_name": "fork/repo"}},
    }))

    with pytest.raises(ValueError, match="Fork PR"):
        source.fetch("owner/repo/demo", pr="42")


def test_lock_file_preserves_immutable_git_provenance(tmp_path):
    metadata = {
        "git": {
            "selector_type": "pr",
            "requested_selector": "42",
            "resolved_sha": FULL_SHA,
            "resolved_at": "2026-08-10T00:00:00+00:00",
            "pr": {"number": 42, "head_sha": FULL_SHA, "head_repo": "owner/repo"},
        }
    }
    lock = HubLockFile(path=tmp_path / "lock.json")
    lock.record_install(
        name="demo", source="github", identifier="owner/repo/demo", trust_level="community",
        scan_verdict="pass", skill_hash="hash", install_path="demo", files=["SKILL.md"],
        metadata=metadata,
    )
    assert lock.get_installed("demo")["metadata"] == metadata
