"""Secret-bearing environment variables must never persist in shell snapshots."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess

import pytest

from tools.environments.base import (
    _SNAPSHOT_SENSITIVE_ENV_NAME_REGEX,
    _export_dump_excluding_session_vars,
)


def test_sensitive_name_regex_contract():
    regex = re.compile(_SNAPSHOT_SENSITIVE_ENV_NAME_REGEX)

    for name in (
        "CLOUDFLARE_API_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "HERMES_RPC_TOKEN",
        "MCP_COMPOSIO_API_KEY",
        "OPENCLAW_GATEWAY_TOKEN",
        "clientSecret",
        "database_password",
        "AWS_ACCESS_KEY_ID",
        "ssh_private_key_pem",
    ):
        assert regex.search(name), name
    for name in ("PATH", "HOME", "HERMES_HOME"):
        assert not regex.search(name), name


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_snapshot_excludes_sensitive_names_and_preserves_benign_names(tmp_path):
    """The real export dump filters sensitive names case-insensitively."""
    sensitive = {
        "CLOUDFLARE_API_TOKEN": "cloudflare-token-value",
        "GITHUB_PERSONAL_ACCESS_TOKEN": "github-token-value",
        "HERMES_RPC_TOKEN": "rpc-token-value",
        "MCP_COMPOSIO_API_KEY": "composio-key-value",
        "OPENCLAW_GATEWAY_TOKEN": "gateway-token-value",
        "READONLY_TOKEN": "readonly-token-value",
        "clientSecret": "secret-value",
        "database_password": "password-value",
        "AWS_ACCESS_KEY_ID": "access-key-value",
        "ssh_private_key_pem": "private-key-value",
    }
    benign = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / ".hermes"),
    }
    child_env = {
        name: os.environ[name]
        for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
        if name in os.environ
    }
    child_env.update(benign)
    child_env.update(sensitive)

    snapshot = "snapshot.sh"
    dump = _export_dump_excluding_session_vars(shlex.quote(snapshot))
    proc = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            f"set -e\nreadonly READONLY_TOKEN\n{dump}\ncat {shlex.quote(snapshot)}",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=child_env,
    )

    assert proc.returncode == 0, proc.stderr
    for name, value in sensitive.items():
        assert name not in proc.stdout
        assert value not in proc.stdout
    for name in benign:
        assert f" {name}=" in proc.stdout
