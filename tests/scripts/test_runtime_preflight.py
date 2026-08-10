"""Regression coverage for the standalone gateway runtime preflight."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def runtime_preflight():
    path = Path(__file__).parents[2] / "scripts" / "runtime_preflight.py"
    spec = importlib.util.spec_from_file_location("runtime_preflight", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _venv(tmp_path: Path) -> Path:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (
        venv / "lib" / "python3.11" / "site-packages" / "hermes_agent-0.20.0.dist-info"
    ).mkdir(parents=True)
    return venv


def _hermes_direct_url(venv: Path) -> Path:
    return (
        venv
        / "lib"
        / "python3.11"
        / "site-packages"
        / "hermes_agent-0.20.0.dist-info"
        / "direct_url.json"
    )


def test_rejects_staging_reference_in_editable_finder(runtime_preflight, tmp_path):
    venv = _venv(tmp_path)
    finder = (
        venv
        / "lib"
        / "python3.11"
        / "site-packages"
        / "__editable___hermes_agent_finder.py"
    )
    finder.write_text(
        "MAPPING = {'hermes_cli': '/tmp/stage/hermes_cli'}\n", encoding="utf-8"
    )

    matches = runtime_preflight.find_forbidden_references(venv, (Path("/tmp/stage"),))

    assert matches == [finder]


def test_rejects_moved_console_script_shebang(runtime_preflight, tmp_path):
    venv = _venv(tmp_path)
    stale = venv / "bin" / "uvicorn"
    stale.write_text("#!/tmp/stage/venv/bin/python\n", encoding="utf-8")

    with pytest.raises(runtime_preflight.RuntimePreflightError, match="uvicorn"):
        runtime_preflight.validate_console_scripts(venv)


def test_accepts_permanent_or_relocatable_console_scripts(runtime_preflight, tmp_path):
    venv = _venv(tmp_path)
    permanent = venv / "bin" / "hermes"
    permanent.write_text(f"#!{venv / 'bin' / 'python'}\n", encoding="utf-8")
    relocatable = venv / "bin" / "tool"
    relocatable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    runtime_preflight.validate_console_scripts(venv)


def test_rejects_direct_url_for_a_deleted_staging_checkout(runtime_preflight, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    venv = _venv(tmp_path)
    direct_url = _hermes_direct_url(venv)
    direct_url.write_text(json.dumps({"url": "file:///tmp/stage"}), encoding="utf-8")

    with pytest.raises(
        runtime_preflight.RuntimePreflightError, match="selected runtime"
    ):
        runtime_preflight.validate_hermes_direct_url(runtime, venv)


def test_direct_url_accepts_the_selected_runtime(runtime_preflight, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    venv = _venv(tmp_path)
    direct_url = _hermes_direct_url(venv)
    direct_url.write_text(json.dumps({"url": runtime.as_uri()}), encoding="utf-8")

    runtime_preflight.validate_hermes_direct_url(runtime, venv)
