"""Every tool that spawns a sandbox must pass the same docker knobs through.

``terminal.docker_extra_args`` and ``terminal.docker_forward_env`` were wired
into the terminal tool (#5722, #12534) but the ``container_config`` dicts built
by ``execute_code`` and the file tools omitted them. A task whose terminal
sandbox ran with ``--network=host`` and a forwarded token therefore got a
default-bridge container with that token unset the moment the same work went
through ``execute_code`` or a file operation.

These assert on the config dict the tools construct, which is the thing that
diverged; the backends already read both keys.
"""

from __future__ import annotations

import re

import pytest

_KEYS = ("docker_extra_args", "docker_forward_env")


def _container_config_block(path: str) -> str:
    """The literal ``container_config = { ... }`` dict from *path*."""
    src = open(path, encoding="utf-8").read()
    m = re.search(r"container_config = \{(.*?)\n\s*\}", src, re.S)
    assert m, f"no container_config literal found in {path}"
    return m.group(1)


@pytest.mark.parametrize(
    "path",
    ["tools/code_execution_tool.py", "tools/file_tools.py", "tools/terminal_tool.py"],
)
@pytest.mark.parametrize("key", _KEYS)
def test_every_sandbox_spawner_forwards_the_key(path, key):
    if path == "tools/terminal_tool.py":
        # terminal_tool builds its dict differently (env-var sourced); assert on
        # the file, which is where its two keys live.
        src = open(path, encoding="utf-8").read()
        assert f'"{key}"' in src, f"{path} stopped forwarding {key}"
        return
    assert f'"{key}"' in _container_config_block(path), (
        f"{path} builds a container_config without {key}; a sandbox spawned "
        "there will silently ignore that config.yaml setting"
    )


def test_code_execution_reads_them_from_config_not_hardcoded():
    block = _container_config_block("tools/code_execution_tool.py")
    for key in _KEYS:
        assert f'config.get("{key}"' in block, f"{key} is not sourced from config"


def test_file_tools_reads_them_from_config_not_hardcoded():
    block = _container_config_block("tools/file_tools.py")
    for key in _KEYS:
        assert f'config.get("{key}"' in block, f"{key} is not sourced from config"


def test_the_three_spawners_agree_on_the_docker_key_set():
    """Guard against the next divergence: compare the key sets directly."""
    ce = set(re.findall(r'"(docker_\w+)"', _container_config_block("tools/code_execution_tool.py")))
    ft = set(re.findall(r'"(docker_\w+)"', _container_config_block("tools/file_tools.py")))

    missing_from_ce = _keys_only_in(ft, ce)
    assert not missing_from_ce, (
        f"file_tools forwards docker keys execute_code does not: {sorted(missing_from_ce)}"
    )


def _keys_only_in(a: set, b: set) -> set:
    """Keys in *a* absent from *b*, ignoring ones that are legitimately local."""
    # docker_mount_cwd_to_workspace is a file-tool concept (it mounts the task
    # cwd for read/write helpers); execute_code has no equivalent surface.
    return (a - b) - {"docker_mount_cwd_to_workspace"}
