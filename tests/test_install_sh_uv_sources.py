"""Behavioral coverage for the installer's uv fallback configuration."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from zipfile import ZIP_DEFLATED, ZipFile

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


def _write_wheel(path: Path) -> None:
    dist_info = "fallback_proof-1.0.0.dist-info"
    with ZipFile(path, "w", ZIP_DEFLATED) as wheel:
        wheel.writestr("fallback_proof/__init__.py", '__version__ = "1.0.0"\n')
        wheel.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\nName: fallback-proof\nVersion: 1.0.0\n",
        )
        wheel.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: hermes-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        wheel.writestr(f"{dist_info}/RECORD", "")


def _write_uv_wrapper(path: Path) -> None:
    """Fail Tier 0, then delegate fallback resolution to real uv as a dry run."""
    path.write_text(
        """#!/bin/sh
printf 'UV_NO_CONFIG=%s UV_NO_SOURCES=%s %s\\n' \\
    "${UV_NO_CONFIG:-}" "${UV_NO_SOURCES:-}" "$*" >> "$UV_WRAPPER_LOG"
if [ "$1" = "sync" ]; then
    exit 42
fi
if [ "$1" = "pip" ] && [ "$2" = "install" ]; then
    "$REAL_UV" "$@" --dry-run
    status=$?
    printf 'pip-status=%s\\n' "$status" >> "$UV_WRAPPER_LOG"
    exit "$status"
fi
exec "$REAL_UV" "$@"
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_python_deps_stage(
    *, project: Path, hermes_home: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--stage",
            "python-deps",
            "--dir",
            str(project),
            "--hermes-home",
            str(hermes_home),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


@pytest.mark.linux_only
def test_installer_fallback_uses_project_sources_with_uv_no_config(tmp_path):
    """The real python-deps fallback must retain package-scoped uv sources."""
    uv = shutil.which("uv")
    assert uv is not None, "uv must be available for installer integration tests"

    index_root = tmp_path / "index"
    package_index = index_root / "simple" / "fallback-proof"
    package_index.mkdir(parents=True)
    wheel_name = "fallback_proof-1.0.0-py3-none-any.whl"
    _write_wheel(package_index / wheel_name)
    (package_index / "index.html").write_text(
        f'<a href="{wheel_name}">{wheel_name}</a>\n', encoding="utf-8"
    )
    (index_root / "empty").mkdir()

    handler = partial(_QuietHandler, directory=str(index_root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        project = tmp_path / "project"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            f"""
[project]
name = "fallback-fixture"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["fallback-proof==1.0.0"]

[project.optional-dependencies]
all = []

[tool.uv.sources]
fallback-proof = {{ index = "fixture" }}

[[tool.uv.index]]
name = "fixture"
url = "{base_url}/simple"
explicit = true
""".strip()
            + "\n",
            encoding="utf-8",
        )
        # Presence enters Tier 0; the wrapper forces that locked sync to fail so
        # install.sh must execute its real uv-pip recovery tier.
        (project / "uv.lock").write_text("", encoding="utf-8")

        venv = project / "venv"
        subprocess.run(
            [uv, "venv", str(venv), "--python", sys.executable],
            check=True,
            capture_output=True,
            text=True,
        )

        hermes_home = tmp_path / "hermes-home"
        managed_bin = hermes_home / "bin"
        managed_bin.mkdir(parents=True)
        wrapper = managed_bin / "uv"
        _write_uv_wrapper(wrapper)
        wrapper_log = tmp_path / "uv-wrapper.log"

        tool_bin = tmp_path / "tool-bin"
        tool_bin.mkdir()
        dpkg = tool_bin / "dpkg"
        dpkg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        dpkg.chmod(0o755)

        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("UV_")
            and key not in {"CONDA_DEFAULT_ENV", "CONDA_PREFIX", "VIRTUAL_ENV"}
        }
        env.update({
            "HOME": str(tmp_path / "home"),
            "PATH": os.pathsep.join([
                str(tool_bin),
                str(Path(sys.executable).parent),
                env["PATH"],
            ]),
            "REAL_UV": uv,
            "UV_CACHE_DIR": str(tmp_path / "cache"),
            "UV_DEFAULT_INDEX": f"{base_url}/empty",
            "UV_WRAPPER_LOG": str(wrapper_log),
        })

        negative_env = {**env, "UV_NO_SOURCES": "1"}
        negative = _run_python_deps_stage(
            project=project, hermes_home=hermes_home, env=negative_env
        )
        negative_calls = wrapper_log.read_text(encoding="utf-8")
        negative_statuses = [
            line.removeprefix("pip-status=")
            for line in negative_calls.splitlines()
            if line.startswith("pip-status=")
        ]

        assert negative.returncode != 0
        assert "UV_NO_CONFIG=1 UV_NO_SOURCES=1 pip install -e .[all]" in negative_calls
        assert negative_statuses
        assert all(status != "0" for status in negative_statuses)

        wrapper_log.write_text("", encoding="utf-8")
        result = _run_python_deps_stage(
            project=project, hermes_home=hermes_home, env=env
        )
        calls = wrapper_log.read_text(encoding="utf-8")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "UV_NO_CONFIG=1 UV_NO_SOURCES= sync --extra all --locked" in calls
        assert "UV_NO_CONFIG=1 UV_NO_SOURCES= pip install -e .[all]" in calls
        assert "pip-status=0" in calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
