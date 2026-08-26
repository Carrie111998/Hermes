"""Behavior tests for the ``hermes mobile`` Android build command."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import main as cli_main
from hermes_cli.subcommands.mobile import build_mobile_parser


def _ns(**overrides) -> argparse.Namespace:
    values = {"release": False, "sdk_root": None}
    values.update(overrides)
    return argparse.Namespace(**values)


def _make_mobile_tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "hermes-agent"
    mobile = root / "apps" / "mobile"
    android = mobile / "android"
    android.mkdir(parents=True)
    (mobile / "package.json").write_text("{}", encoding="utf-8")
    return root, android


def _make_sdk(tmp_path: Path) -> Path:
    sdk = tmp_path / "android-sdk"
    (sdk / "platform-tools").mkdir(parents=True)
    return sdk


def test_mobile_parser_selects_release_build_and_sdk_root():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_mobile_parser(subparsers, cmd_mobile=lambda args: args)

    args = parser.parse_args(["mobile", "--release", "--sdk-root", "/android-sdk"])

    assert args.command == "mobile"
    assert args.release is True
    assert args.sdk_root == "/android-sdk"


def test_mobile_build_runs_the_checked_mobile_pipeline(tmp_path, monkeypatch):
    root, android = _make_mobile_tree(tmp_path)
    sdk = _make_sdk(tmp_path)
    apk = android / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    apk.parent.mkdir(parents=True)
    apk.write_bytes(b"apk")
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

    ok = subprocess.CompletedProcess([], 0)
    with patch("hermes_cli.main._resolve_node_runtime_npm", return_value="/usr/bin/npm"), \
         patch("hermes_cli.main._run_npm_install_deterministic", return_value=ok) as install, \
         patch("hermes_cli.main.subprocess.run", return_value=ok) as run:
        cli_main.cmd_mobile(_ns(sdk_root=str(sdk)))

    assert install.call_args.args == ("/usr/bin/npm", root)
    assert install.call_args.kwargs["capture_output"] is False
    assert [call.args[0] for call in run.call_args_list] == [
        ["/usr/bin/npm", "run", "test", "--workspace", "apps/mobile"],
        ["/usr/bin/npm", "run", "build", "--workspace", "apps/mobile"],
        ["/usr/bin/npm", "exec", "--", "cap", "sync", "android"],
        ["./gradlew", "assembleDebug", "--no-daemon"],
    ]
    assert [call.kwargs["cwd"] for call in run.call_args_list] == [root, root, root / "apps" / "mobile", android]
    assert run.call_args_list[-1].kwargs["env"]["ANDROID_HOME"] == str(sdk)


def test_mobile_rejects_an_sdk_without_platform_tools(tmp_path, monkeypatch, capsys):
    root, _ = _make_mobile_tree(tmp_path)
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)

    with pytest.raises(SystemExit) as error:
        cli_main.cmd_mobile(_ns(sdk_root=str(tmp_path / "not-an-sdk")))

    assert error.value.code == 1
    assert "requires an Android SDK" in capsys.readouterr().out
