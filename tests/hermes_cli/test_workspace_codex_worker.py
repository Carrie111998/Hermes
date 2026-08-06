from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess

import pytest

from hermes_cli.workspace_codex_worker import (
    CodexAuditManifest,
    CodexWorker,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_codex(tmp_path: Path, *, version: str = "0.146.1") -> Path:
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"VERSION = {version!r}\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-cli ' + VERSION)\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "pathlib.Path('worker-env.json').write_text(json.dumps({\n"
        "    'args': sys.argv[1:],\n"
        "    'gh_token': os.environ.get('GH_TOKEN'),\n"
        "    'git_askpass': os.environ.get('GIT_ASKPASS'),\n"
        "    'git_config_global': os.environ.get('GIT_CONFIG_GLOBAL'),\n"
        "    'home': os.environ.get('HOME'),\n"
        "    'prompt': prompt,\n"
        "    'ssh_auth_sock': os.environ.get('SSH_AUTH_SOCK'),\n"
        "}, sort_keys=True))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _worker(tmp_path: Path, executable: Path) -> CodexWorker:
    digest = _sha256(executable)
    manifest = CodexAuditManifest(
        version="0.146.1",
        package_integrity="sha512-wrapper",
        platform_integrity="sha512-platform",
        artifact_sha256=frozenset({digest}),
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "path": str(executable),
                        "role": "executable",
                        "sha256": digest,
                    }
                ],
                "codex_home": str(tmp_path / "codex-home"),
                "package_integrity": "sha512-wrapper",
                "platform_integrity": "sha512-platform",
                "version": "0.146.1",
            }
        ),
        encoding="utf-8",
    )
    policy.chmod(0o600)
    return CodexWorker.from_policy(policy, manifest=manifest, state_dir=tmp_path / "state")


def test_codex_worker_uses_pinned_sandbox_and_strips_git_credentials(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    executable = _fake_codex(tmp_path)
    worker = _worker(tmp_path, executable)
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/secret/agent.sock")

    result = worker.run(prompt="Update the page", workdir=repository, timeout_seconds=10)

    assert result["ok"] is True
    assert result["version"] == "0.146.1"
    captured = json.loads((repository / "worker-env.json").read_text())
    assert captured["args"] == ["exec", "--json", "--sandbox", "workspace-write", "-"]
    assert captured["prompt"].endswith("Update the page")
    assert captured["gh_token"] is None
    assert captured["ssh_auth_sock"] is None
    assert captured["git_askpass"] == "/usr/bin/false"
    assert captured["git_config_global"] == "/dev/null"
    assert Path(captured["home"]).is_relative_to(tmp_path / "state")
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700


def test_codex_worker_rejects_binary_tampering_after_policy_load(tmp_path):
    executable = _fake_codex(tmp_path)
    worker = _worker(tmp_path, executable)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)

    with pytest.raises(ValueError, match="digest"):
        worker.run(prompt="do nothing", workdir=tmp_path, timeout_seconds=10)


def test_codex_worker_rejects_unpinned_cli_version(tmp_path):
    executable = _fake_codex(tmp_path, version="9.9.9")
    worker = _worker(tmp_path, executable)

    with pytest.raises(ValueError, match="version"):
        worker.run(prompt="do nothing", workdir=tmp_path, timeout_seconds=10)
