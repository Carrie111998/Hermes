from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from hermes_cli.subcommands.workspace import build_workspace_parser, cmd_workspace
from hermes_cli.workspace_lifecycle import collect_inventory, import_dry_run


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "workspace-test"],
        ["git", "config", "user.email", "workspace-test@example.invalid"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / "README").write_text("workspace lifecycle\n")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    return repo


def test_inventory_is_read_only_and_legacy_rows_never_become_removable(tmp_path):
    repo = _repo(tmp_path)
    before = subprocess.run(["git", "status", "--porcelain=v1", "-z"], cwd=repo, capture_output=True, check=True).stdout
    report = collect_inventory(repo)
    after = subprocess.run(["git", "status", "--porcelain=v1", "-z"], cwd=repo, capture_output=True, check=True).stdout

    assert before == after == b""
    assert report["schema_version"] == 1
    assert report["dry_run"] is True
    assert report["workspaces"]
    assert all(row["disposition"] != "removable" for row in report["workspaces"])


def test_import_dry_run_does_not_create_registry_or_modify_repo(tmp_path):
    repo = _repo(tmp_path)
    before_paths = sorted(item.name for item in repo.iterdir())
    report = import_dry_run(repo)
    assert report["operation"] == "import"
    assert report["dry_run"] is True
    assert sorted(item.name for item in repo.iterdir()) == before_paths


def test_workspace_subcommand_requires_json_and_dry_run_for_import(tmp_path, capsys):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_workspace_parser(subparsers, cmd_workspace=cmd_workspace)
    repo = _repo(tmp_path)

    args = parser.parse_args(["workspace", "inventory", "--repo", str(repo), "--json"])
    assert args.func(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 1

    try:
        parser.parse_args(["workspace", "import", "--repo", str(repo), "--json"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("import without --dry-run must be rejected")

    for forbidden in ("apply", "remove"):
        try:
            parser.parse_args(["workspace", forbidden, "--repo", str(repo)])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"workspace {forbidden} must not be exposed")


def test_manifest_binds_exact_observation_but_remains_read_only(tmp_path, capsys):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_workspace_parser(subparsers, cmd_workspace=cmd_workspace)
    repo = _repo(tmp_path)
    before = sorted(item.name for item in repo.iterdir())

    args = parser.parse_args(["workspace", "manifest", "--repo", str(repo), "--json"])
    assert args.func(args) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["operation"] == "closeout_manifest"
    assert len(report["manifest_hash"]) == 64
    assert report["apply_available"] is False
    assert report["entries"][0]["canonical_path"] == str(repo.resolve())
    assert sorted(item.name for item in repo.iterdir()) == before
