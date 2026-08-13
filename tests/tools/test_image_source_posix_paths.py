"""Regression test for #85406: the vision container exec-read must use the
POSIX form of the path even when the resolver was handed a Windows-flavored
``Path`` (Windows hosts under the Docker terminal backend).

``PureWindowsPath`` is instantiable on every platform, so this unit test
reproduces the Windows-host shape on Linux CI: ``str()`` yields backslash
separators (which the Linux container cannot resolve) while ``as_posix()``
yields the POSIX form the exec-read can open. Without the fix the command
contains a backslash path and the test fails on the command assertion.
"""
import base64

import pytest

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _FakeEnv:
    """Records the exec-read command and returns canned base64 of a tiny PNG."""

    def __init__(self):
        self.commands = []

    def execute(self, cmd):
        self.commands.append(cmd)
        return {"returncode": 0, "output": base64.b64encode(_TINY_PNG).decode()}


@pytest.mark.asyncio
async def test_container_exec_read_uses_posix_paths_for_windows_flavored_path(monkeypatch):
    """A Windows-flavored path resolves via the in-container read, not backslashes."""
    from pathlib import PureWindowsPath

    from tools import image_source

    env = _FakeEnv()
    monkeypatch.setattr(image_source, "_ensure_container_env", lambda task_id: None)
    monkeypatch.setattr(image_source, "_get_active_env", lambda task_id: env)

    p = PureWindowsPath("/workspace/shot.png")
    res = await image_source._resolve_container_fallback(
        p, image_source.ResolveContext(task_id="test"), "/workspace/shot.png"
    )
    assert res.origin == "container"
    assert res.mime == "image/png"
    assert res.data == _TINY_PNG
    assert env.commands, "exec-read must have run"
    assert "/workspace/shot.png" in env.commands[0]
    assert "\\workspace" not in env.commands[0]
