"""Secret-bearing environment variables must never persist in shell snapshots."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.environments.base import (
    BaseEnvironment,
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
@pytest.mark.parametrize("caller_ifs", (None, ":", ","))
def test_snapshot_excludes_sensitive_names_and_preserves_benign_names(
    tmp_path,
    caller_ifs,
):
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
    ifs_setup = "" if caller_ifs is None else f"IFS={shlex.quote(caller_ifs)}\n"
    proc = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            (
                f"set -e\n{ifs_setup}readonly READONLY_TOKEN\n{dump}\n"
                f"cat {shlex.quote(snapshot)}"
            ),
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


class _ExecutableEnvironment(BaseEnvironment):
    """Run the production command wrapper against a real Bash process."""

    def __init__(self, tmp_path: Path):
        self._temp_dir = str(tmp_path)
        super().__init__(cwd=str(tmp_path), timeout=30)

    def get_temp_dir(self) -> str:
        return self._temp_dir

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ) -> subprocess.Popen:
        args = [shutil.which("bash")]
        if login:
            args.append("-l")
        args.extend(["-c", cmd_string])
        return subprocess.Popen(
            args,
            cwd=self.cwd,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def cleanup(self):
        pass


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="POSIX bash snapshot path required",
)
@pytest.mark.parametrize("caller_ifs", (":", ","))
def test_post_command_snapshot_redacts_sensitive_env_with_altered_ifs(
    tmp_path: Path,
    caller_ifs: str,
):
    """The production post-command dump must ignore caller-controlled IFS."""
    env = _ExecutableEnvironment(tmp_path)
    env.init_session()
    assert env._snapshot_ready

    name = "ALTERED_IFS_API_TOKEN"
    value = "altered-ifs-sensitive-value"
    result = env.execute(
        f"IFS={shlex.quote(caller_ifs)}; export IFS; "
        f"export {name}={shlex.quote(value)}"
    )
    assert result["returncode"] == 0, result["output"]

    snapshot = Path(env._snapshot_path).read_text()
    assert name not in snapshot
    assert value not in snapshot

    follow_up = env.execute(f'printf "%s" "${{{name}:-missing}}"')
    assert follow_up["returncode"] == 0, follow_up["output"]
    assert follow_up["output"] == "missing"
