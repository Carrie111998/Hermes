"""WhatsApp bridge subprocesses must hide Windows console flashes (#75628).

``node --version``, ``taskkill``, and ``npm install`` are console-subsystem
children. From a windowless ``pythonw`` gateway parent they allocate a
visible console unless ``creationflags=windows_hide_flags()`` is set.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import plugins.platforms.whatsapp.adapter as wa


def test_check_whatsapp_requirements_passes_hide_flags_on_windows():
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="v22.0.0\n", stderr="")

    with (
        patch.object(wa, "_IS_WINDOWS", True),
        patch.object(wa, "windows_hide_flags", return_value=0x08000000),
        patch.object(wa, "find_node_executable", return_value="node.exe"),
        patch.object(wa.subprocess, "run", side_effect=fake_run),
    ):
        assert wa.check_whatsapp_requirements() is True

    assert captured.get("creationflags") == 0x08000000


def test_check_whatsapp_requirements_no_flags_on_posix():
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="v22.0.0\n", stderr="")

    with (
        patch.object(wa, "_IS_WINDOWS", False),
        patch.object(wa, "find_node_executable", return_value="/usr/bin/node"),
        patch.object(wa.subprocess, "run", side_effect=fake_run),
    ):
        assert wa.check_whatsapp_requirements() is True

    assert captured.get("creationflags") == 0


def test_terminate_bridge_process_passes_hide_flags_on_windows():
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    proc = SimpleNamespace(pid=4242)

    with (
        patch.object(wa, "_IS_WINDOWS", True),
        patch.object(wa, "windows_hide_flags", return_value=0x08000000),
        patch.object(wa.subprocess, "run", side_effect=fake_run),
    ):
        wa._terminate_bridge_process(proc, force=True)

    assert captured.get("creationflags") == 0x08000000
    # First positional argv is the taskkill command list.
    # subprocess.run(cmd, ...) — cmd is args[0]
    # We only assert creationflags; command shape is covered elsewhere.
