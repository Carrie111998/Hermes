"""Remote sandbox paths must be POSIX regardless of the host OS.

The single-file upload helpers in the container/remote backends derive the
parent directory of a *remote* path in order to ``mkdir -p`` it before the
transfer.  Deriving it with :mod:`pathlib` yields ``WindowsPath`` semantics on
a Windows host, producing ``\\root\\.hermes`` for a remote Linux sandbox.

These tests pin the POSIX behaviour and the shell-quoting guarantee together:
the parent must stay a single quoted argument so metacharacters in a path
cannot break out into a second command.
"""

import asyncio
import shlex
from types import SimpleNamespace
from unittest.mock import MagicMock

from tools.environments.daytona import DaytonaEnvironment
from tools.environments.modal import ModalEnvironment
from tools.environments.ssh import SSHEnvironment

# A remote path whose directory component contains shell metacharacters.
EVIL_REMOTE = "/root/.hermes/skills/evil; touch /tmp/pwned/file.txt"
EVIL_PARENT = "/root/.hermes/skills/evil; touch /tmp/pwned"


def test_daytona_upload_uses_posix_parent(tmp_path):
    host_file = tmp_path / "token.txt"
    host_file.write_text("secret", encoding="utf-8")

    env = SimpleNamespace(_sandbox=MagicMock())
    DaytonaEnvironment._daytona_upload(env, str(host_file), EVIL_REMOTE)

    cmd = env._sandbox.process.exec.call_args_list[0][0][0]
    assert cmd == f"mkdir -p {shlex.quote(EVIL_PARENT)}"
    assert "\\" not in cmd


def test_modal_upload_uses_posix_parent(tmp_path):
    host_file = tmp_path / "token.txt"
    host_file.write_bytes(b"secret")

    captured = {}

    class _Stdin:
        def write(self, _data):
            pass

        def write_eof(self):
            pass

        @property
        def drain(self):
            async def _drain():
                return None
            return SimpleNamespace(aio=_drain)

    async def _fake_exec(_shell, _flag, cmd):
        captured["cmd"] = cmd

        async def _wait():
            return 0

        return SimpleNamespace(stdin=_Stdin(), wait=SimpleNamespace(aio=_wait))

    env = SimpleNamespace(
        _sandbox=SimpleNamespace(exec=SimpleNamespace(aio=_fake_exec)),
        _worker=SimpleNamespace(
            run_coroutine=lambda coro, timeout=None: asyncio.run(coro)
        ),
        _STDIN_CHUNK_SIZE=ModalEnvironment._STDIN_CHUNK_SIZE,
    )
    ModalEnvironment._modal_upload(env, str(host_file), EVIL_REMOTE)

    mkdir_part = captured["cmd"].split(" && ")[0]
    assert mkdir_part == f"mkdir -p {shlex.quote(EVIL_PARENT)}"
    assert "\\" not in mkdir_part


def test_scp_upload_uses_posix_parent(tmp_path, monkeypatch):
    host_file = tmp_path / "token.txt"
    host_file.write_text("secret", encoding="utf-8")

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("tools.environments.ssh.subprocess.run", _fake_run)

    env = SimpleNamespace(
        _build_ssh_command=lambda: ["ssh", "host"],
        control_socket="/tmp/cs",
        port=22,
        key_path=None,
        user="root",
        host="example.com",
    )
    SSHEnvironment._scp_upload(env, str(host_file), EVIL_REMOTE)

    mkdir_arg = calls[0][-1]
    assert mkdir_arg == f"mkdir -p {shlex.quote(EVIL_PARENT)}"
    assert "\\" not in mkdir_arg
