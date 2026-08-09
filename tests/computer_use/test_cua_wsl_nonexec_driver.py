from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from tools.computer_use import cua_backend, doctor, permissions


DRIVER = "/mnt/c/Users/test/cua-driver.exe"


def _nonexec_wsl_driver():
    def isfile(path: str) -> bool:
        return path in {DRIVER, "/init"}

    def access(path: str, mode: int) -> bool:
        return path == "/init"

    return (
        patch("hermes_constants.is_wsl", return_value=True),
        patch.object(cua_backend.os.path, "isfile", side_effect=isfile),
        patch.object(cua_backend.os, "access", side_effect=access),
        patch.object(cua_backend.shutil, "which", return_value=None),
    )


def test_resolver_accepts_nonexec_wsl_drvfs_exe_and_prefixes_init():
    patches = _nonexec_wsl_driver()
    with patches[0], patches[1], patches[2], patches[3]:
        assert cua_backend.resolve_cua_driver_cmd(DRIVER) == DRIVER
        assert cua_backend.cua_driver_argv(DRIVER, "--version") == [
            "/init",
            DRIVER,
            "--version",
        ]


def test_init_prefix_is_not_used_for_direct_or_native_execution():
    with (
        patch("hermes_constants.is_wsl", return_value=True),
        patch.object(cua_backend.shutil, "which", return_value=DRIVER),
        patch.object(cua_backend.os, "access", return_value=True),
    ):
        assert cua_backend.resolve_cua_driver_cmd(DRIVER) == DRIVER
        assert cua_backend.cua_driver_argv(DRIVER, "mcp") == [DRIVER, "mcp"]

    with patch("hermes_constants.is_wsl", return_value=False):
        assert cua_backend.cua_driver_argv(DRIVER, "mcp") == [DRIVER, "mcp"]


def test_status_uses_shared_init_invocation_for_nonexec_wsl_driver():
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        if "--version" in argv:
            return SimpleNamespace(stdout="cua-driver 0.19.2\n", returncode=0)
        return SimpleNamespace(
            stdout=json.dumps({"ok": True, "probes": []}),
            returncode=0,
        )

    patches = _nonexec_wsl_driver()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patch.object(permissions.sys, "platform", "linux"),
        patch.object(permissions.subprocess, "run", side_effect=fake_run),
    ):
        status = permissions.computer_use_status(DRIVER)

    assert status["installed"] is True
    assert status["ready"] is True
    assert seen
    assert all(argv[:2] == ["/init", DRIVER] for argv in seen)


def test_doctor_and_runtime_use_shared_init_invocation_for_nonexec_wsl_driver():
    popen = SimpleNamespace()
    patches = _nonexec_wsl_driver()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patch.object(doctor.subprocess, "Popen", return_value=popen) as mock_popen,
    ):
        assert doctor._open_mcp(DRIVER) is popen
    assert mock_popen.call_args.args[0] == ["/init", DRIVER, "mcp"]

    completed = SimpleNamespace(returncode=1, stdout="")
    patches = _nonexec_wsl_driver()
    cua_backend._cua_driver_supports_no_overlay.cache_clear()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        # Force the Linux/WSL auto no_overlay path so the ``--help`` probe
        # also runs (CI Linux runners hit this; Windows hosts often do not).
        patch.object(cua_backend, "_cua_no_overlay", return_value=True),
        patch.object(cua_backend.subprocess, "run", return_value=completed) as mock_run,
    ):
        assert cua_backend._resolve_mcp_invocation(DRIVER) == (DRIVER, ["mcp"])
    # First hop is always ``manifest``; auto no_overlay then probes ``--help``.
    # Both must share the /init argv contract.
    assert [call.args[0] for call in mock_run.call_args_list] == [
        ["/init", DRIVER, "manifest"],
        ["/init", DRIVER, "--help"],
    ]
