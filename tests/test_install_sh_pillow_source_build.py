"""Behavioral tests for Pillow 12.3 source builds on older Linux hosts."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
PILLOW_HELPERS = REPO_ROOT / "scripts" / "lib" / "pillow_source_build.sh"


def _run_bash(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(PILLOW_HELPERS))}\n{body}"],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_pillow_12_3_linux_wheel_floor() -> None:
    """The compatibility predicate matches Pillow 12.3's published wheel tags."""
    result = _run_bash(
        """
set -u
pillow_linux_wheel_compatible x86_64 glibc 2.17; echo "rhel7=$?"
pillow_linux_wheel_compatible x86_64 glibc 2.27; echo "glibc227=$?"
pillow_linux_wheel_compatible aarch64 musl 1.2; echo "musl12=$?"
pillow_linux_wheel_compatible ppc64le glibc 2.39; echo "ppc64le=$?"
"""
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "rhel7=1",
        "glibc227=0",
        "musl12=0",
        "ppc64le=1",
    ]


def test_host_getconf_glibc_takes_precedence_over_python_build_baseline() -> None:
    """Wheel selection uses the host glibc even when Python has an older baseline."""
    result = _run_bash(
        """
set -u
OS=linux
PYTHON_PATH=python_probe
getconf() { printf 'glibc 2.39\n'; }
uname() { printf 'x86_64\n'; }
python_probe() { echo "PYTHON_FALLBACK_RAN"; return 99; }
pillow_source_build_required
printf 'source_required=%s\n' "$?"
"""
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["source_required=1"]


def test_musl_falls_back_to_interpreter_probe() -> None:
    """A non-glibc host uses the bounded interpreter fallback when needed."""
    result = _run_bash(
        """
set -u
OS=linux
PYTHON_PATH=python_probe
getconf() { return 1; }
ldd() { printf 'not a dynamic executable\n'; return 1; }
uname() { printf 'aarch64\n'; }
python_probe() { cat >/dev/null; printf 'musl|1.2\n'; }
pillow_source_build_required
printf 'source_required=%s\n' "$?"
"""
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["source_required=1"]


def test_musl_host_probe_precedes_pre_3_14_python_fallback() -> None:
    """Compatible musl hosts do not need Python 3.14 libc detection."""
    result = _run_bash(
        """
set -u
OS=linux
PYTHON_PATH=python_probe
getconf() { return 1; }
ldd() { printf 'musl libc (x86_64)\nVersion 1.2.5\n'; return 1; }
uname() { printf 'x86_64\n'; }
python_probe() { echo "PYTHON_FALLBACK_RAN"; return 99; }
pillow_source_build_required
printf 'source_required=%s\n' "$?"
"""
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["source_required=1"]


def test_python_header_probe_checks_platinclude_and_includepy(
    tmp_path: Path,
) -> None:
    """Alternate interpreter layouts can expose Python.h outside ``include``."""
    python_h = tmp_path / "alternate" / "Python.h"
    python_h.parent.mkdir()
    python_h.touch()

    for candidate_kind in ("include", "platinclude", "INCLUDEPY"):
        fake_stdlib = tmp_path / candidate_kind
        fake_stdlib.mkdir()
        path_value = (
            f"CANDIDATE if name == {candidate_kind!r} else None"
            if candidate_kind != "INCLUDEPY"
            else "None"
        )
        config_value = (
            "CANDIDATE if name == 'INCLUDEPY' else None"
            if candidate_kind == "INCLUDEPY"
            else "None"
        )
        (fake_stdlib / "sysconfig.py").write_text(
            "\n".join([
                f"CANDIDATE = {str(python_h.parent)!r}",
                "def get_path(name):",
                f"    return {path_value}",
                "def get_config_var(name):",
                f"    return {config_value}",
            ]),
            encoding="utf-8",
        )
        result = _run_bash(
            f"""
set -u
PYTHON_PATH={shlex.quote(sys.executable)}
PYTHONPATH={shlex.quote(str(fake_stdlib))}
export PYTHONPATH
pillow_python_headers_ready
"""
        )
        assert result.returncode == 0, (
            f"{candidate_kind} was not checked: {result.stderr}"
        )


def test_missing_debian_prerequisites_fail_with_actionable_command() -> None:
    """A non-interactive older Debian host never escalates privileges itself."""
    result = _run_bash(
        """
set -u
DISTRO=debian
NON_INTERACTIVE=true
IS_INTERACTIVE=false
pillow_source_build_required() { return 0; }
pillow_source_build_ready() { return 1; }
log_info() { printf 'INFO:%s\n' "$1"; }
log_warn() { printf 'WARN:%s\n' "$1"; }
log_error() { printf 'ERROR:%s\n' "$1"; }
prompt_yes_no() { return 1; }
id() { [ "$1" = "-u" ] && echo 1000; }
sudo() { echo "UNSAFE_SUDO:$*"; return 99; }
prepare_python_build_environment
"""
    )
    assert result.returncode == 1
    assert "UNSAFE_SUDO" not in result.stdout
    assert (
        "sudo apt-get update && sudo apt-get install -y "
        "build-essential python3-dev libffi-dev libjpeg-dev zlib1g-dev"
    ) in result.stdout


def test_missing_rhel_prerequisites_fail_with_actionable_command() -> None:
    """RHEL-family diagnostics name the codec headers required by Pillow."""
    result = _run_bash(
        """
set -u
DISTRO=rocky
pillow_source_build_required() { return 0; }
pillow_source_build_ready() { return 1; }
log_info() { printf 'INFO:%s\n' "$1"; }
log_warn() { printf 'WARN:%s\n' "$1"; }
log_error() { printf 'ERROR:%s\n' "$1"; }
prompt_yes_no() { return 1; }
id() { [ "$1" = "-u" ] && echo 1000; }
dnf() { :; }
sudo() { echo "UNSAFE_SUDO:$*"; return 99; }
prepare_python_build_environment
"""
    )
    assert result.returncode == 1
    assert "UNSAFE_SUDO" not in result.stdout
    assert (
        "sudo dnf install -y "
        "gcc python3-devel libffi-devel libjpeg-turbo-devel zlib-devel"
    ) in result.stdout


def test_compatible_wheel_keeps_normal_debian_build_tool_path() -> None:
    """Compatible-wheel hosts preserve the installer's existing build-tool setup."""
    result = _run_bash(
        """
set -u
DISTRO=debian
TRACE="$(mktemp)"
pillow_source_build_required() { printf 'WHEEL_CHECK\n'; return 1; }
dpkg() { printf 'DPKG:%s\n' "$*" >>"$TRACE"; return 1; }
id() { [ "$1" = "-u" ] && echo 0; }
apt-get() { printf 'APT:%s\n' "$*" >>"$TRACE"; }
log_info() { printf 'INFO:%s\n' "$1"; }
log_success() { printf 'SUCCESS:%s\n' "$1"; }
log_warn() { printf 'WARN:%s\n' "$1"; }
prepare_python_build_environment
cat "$TRACE"
rm -f "$TRACE"
"""
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "WHEEL_CHECK",
        "INFO:Some build tools may be needed for Python packages...",
        "SUCCESS:Build tools installed",
        "DPKG:-s gcc",
        "APT:update -qq",
        "APT:install -y -qq build-essential python3-dev libffi-dev",
    ]


def test_source_build_uses_one_provisioning_path_before_resolver() -> None:
    """Older Debian hosts use the Pillow transaction and stop on its failure."""
    result = _run_bash(
        """
set -u
DISTRO=debian
pillow_source_build_required() { printf 'SOURCE_REQUIRED\n'; return 0; }
prepare_pillow_source_build() { printf 'PILLOW_PROVISION\n'; return 1; }
dpkg() { printf 'UNEXPECTED_DPKG\n'; return 1; }
prepare_python_build_environment
printf 'result=%s\n' "$?"
"""
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "SOURCE_REQUIRED",
        "PILLOW_PROVISION",
        "result=1",
    ]
