"""Native Linux bot-mode updater handoff witnesses."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._active_profile_name = MagicMock(return_value="work")
    runner._voice_mode = {}
    runner._update_prompt_pending = {}
    runner._schedule_update_notification_watch = MagicMock()
    return runner


def _event(platform: Platform) -> MessageEvent:
    return MessageEvent(
        text="/update",
        source=SessionSource(
            platform=platform,
            user_id="linux-user",
            chat_id="linux-chat",
            user_name="Linux Bot Test",
            profile="work",
        ),
    )


def _wait_for_json(path: Path) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            time.sleep(0.01)
    raise AssertionError(f"detached updater did not publish {path}")


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
@pytest.mark.asyncio
@pytest.mark.parametrize("platform_name", ["telegram", "discord"])
async def test_bot_update_really_handoffs_to_independent_linux_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
) -> None:
    """Run the real setsid/Popen path against a non-mutating probe executable."""

    assert shutil.which("setsid") is not None
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "gateway").mkdir()
    fake_module = project / "gateway" / "slash_commands.py"
    fake_module.touch()
    control_home = tmp_path / "hermes"
    control_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(control_home))
    if platform_name == "discord":
        from hermes_cli.plugins import PluginManager

        PluginManager().discover_and_load(force=True)
        from gateway.platform_registry import platform_registry

        assert platform_registry.get("discord") is not None
        assert platform_registry.get("discord").allow_update_command is True
    from gateway.config import Platform as RuntimePlatform

    platform = RuntimePlatform(platform_name)
    runner = _runner()

    observation_path = tmp_path / f"{platform_name}-handoff.json"
    probe = tmp_path / "probe-python"
    probe.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

home = Path(os.environ["HERMES_HOME"])
pending = json.loads((home / ".update_pending.json").read_text(encoding="utf-8"))
observation = {
    "argv": sys.argv[1:],
    "pid": os.getpid(),
    "ppid": os.getppid(),
    "sid": os.getsid(0),
    "platform": pending["platform"],
    "correlation": os.environ["HERMES_UPDATE_CORRELATION_ID"],
    "pending_correlation": pending["correlation_id"],
    "origin_profile": os.environ["HERMES_UPDATE_ORIGIN_PROFILE"],
    "origin_home": os.environ["HERMES_UPDATE_ORIGIN_HOME"],
    "pending_profile_home": pending["profile_home"],
    "output_path": os.environ["HERMES_UPDATE_OUTPUT_PATH"],
}
Path(os.environ["HERMES_LINUX_HANDOFF_OBSERVATION"]).write_text(
    json.dumps(observation), encoding="utf-8"
)
print("linux-handoff-probe", flush=True)
""",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    monkeypatch.setenv("HERMES_LINUX_HANDOFF_OBSERVATION", str(observation_path))

    with (
        patch("gateway.run._hermes_home", control_home),
        patch("gateway.slash_commands.__file__", str(fake_module)),
        patch(
            "hermes_cli.runtime_launch.resolve_project_python",
            return_value=str(probe),
        ),
    ):
        result = await runner._handle_update_command(_event(platform))

    assert "Starting Hermes update" in result
    observation = _wait_for_json(observation_path)
    pending = json.loads(
        (control_home / ".update_pending.json").read_text(encoding="utf-8")
    )
    assert observation["platform"] == platform_name
    assert observation["pid"] == observation["sid"]
    assert observation["argv"][-2:] == ["update", "--gateway"]
    assert observation["correlation"] == observation["pending_correlation"]
    assert observation["correlation"] == pending["correlation_id"]
    assert pending["launch_state"] == "spawned"
    assert pending["launcher_pid"] == observation["pid"]
    assert observation["origin_profile"] == "work"
    assert observation["origin_home"] == observation["pending_profile_home"]
    assert observation["output_path"] == str(
        (control_home / ".update_output.txt").resolve()
    )

    output_path = control_home / ".update_output.txt"
    deadline = time.monotonic() + 10
    while "linux-handoff-probe" not in output_path.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    try:
        os.waitpid(int(observation["pid"]), 0)
    except ChildProcessError:
        pass


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
@pytest.mark.asyncio
async def test_bot_update_admission_uses_real_linux_file_lock(
    tmp_path: Path,
) -> None:
    """A second gateway process cannot pass the shared action-slot claim."""

    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "gateway").mkdir()
    fake_module = project / "gateway" / "slash_commands.py"
    fake_module.touch()
    control_home = tmp_path / "hermes"
    control_home.mkdir()
    owner = _runner()
    contender = _runner()
    handle = owner._try_update_notification_lock(control_home)
    assert handle is not None
    try:
        with (
            patch("gateway.run._hermes_home", control_home),
            patch("gateway.slash_commands.__file__", str(fake_module)),
            patch(
                "hermes_cli.runtime_launch.resolve_project_python",
                return_value="/project/python",
            ),
            patch("subprocess.Popen") as popen,
        ):
            result = await contender._handle_update_command(
                _event(Platform.TELEGRAM)
            )
    finally:
        owner._release_update_notification_lock(handle)

    assert "already in progress" in result
    popen.assert_not_called()
    assert not (control_home / ".update_pending.json").exists()
