from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys
from types import SimpleNamespace

from scripts.audit_external_tooling import (
    _summarize_json_output,
    build_commands,
    build_uv_export_command,
    run_audits,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_zizmor_summary_preserves_counts_when_raw_capture_is_bounded():
    output = json.dumps(
        [
            {
                "ident": "github-app",
                "determinations": {"severity": "High", "confidence": "High"},
                "locations": [{}, {}],
            },
            {
                "ident": "github-app",
                "determinations": {"severity": "High", "confidence": "Medium"},
                "locations": [{}],
            },
        ]
    )

    assert _summarize_json_output("zizmor", output) == {
        "finding_groups": 2,
        "locations": 3,
        "by_severity": {"High": 2},
        "by_confidence": {"High": 1, "Medium": 1},
        "by_ident": {"github-app": 2},
    }


def test_commands_are_pinned_read_only_and_offline_where_supported(tmp_path):
    tool_root = tmp_path / "bin"
    commands = build_commands(repo_root=tmp_path / "repo", tool_root=tool_root)

    assert [command.name for command in commands] == [
        "zizmor",
        "import-linter",
        "pip-audit",
    ]
    flattened = [argument for command in commands for argument in command.argv]
    assert "--fix" not in flattened
    assert "--offline" in commands[0].argv
    assert "--no-exit-codes" in commands[0].argv
    assert "--locked" not in commands[2].argv
    assert "-r" in commands[2].argv
    assert "--require-hashes" in commands[2].argv
    assert "--disable-pip" in commands[2].argv
    assert "--cache-dir" in commands[2].argv

    export_command = build_uv_export_command(
        requirements_path=tmp_path / "requirements.txt"
    )
    assert "--locked" in export_command
    assert "--cache-dir" in export_command


def test_receipt_binds_results_to_exact_git_identity(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tool_root = tmp_path / "bin"
    head = "a" * 40

    def fake_runner(argv, **kwargs):
        command = tuple(str(part) for part in argv)
        if command[:3] == ("git", "rev-parse", "HEAD"):
            return SimpleNamespace(returncode=0, stdout=f"{head}\n", stderr="")
        if command[:3] == ("git", "branch", "--show-current"):
            return SimpleNamespace(returncode=0, stdout="feature/audit\n", stderr="")
        if command[:3] == ("git", "status", "--porcelain"):
            return SimpleNamespace(returncode=0, stdout=" M local.txt\n", stderr="")
        return SimpleNamespace(returncode=0, stdout='{"ok": true}\n', stderr="")

    receipt = run_audits(repo_root=repo, tool_root=tool_root, runner=fake_runner)

    assert receipt["repository"]["head"] == head
    assert receipt["repository"]["branch"] == "feature/audit"
    assert receipt["repository"]["dirty"] is True
    assert receipt["ok"] is True
    assert len(receipt["audits"]) == 3
    assert all(row["stdout_sha256"].startswith("sha256:") for row in receipt["audits"])


def test_script_runs_directly_from_its_file_path():
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "audit_external_tooling.py"), "--plan"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["read_only"] is True
