"""Regression tests for symlink-safe Docker stage2 ownership repair."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE2_HOOK = REPO_ROOT / "docker" / "stage2-hook.sh"


@pytest.fixture(scope="module")
def stage2_text() -> str:
    if not STAGE2_HOOK.exists():
        pytest.skip("docker/stage2-hook.sh not present in this checkout")
    return STAGE2_HOOK.read_text()


def _chown_orion_tree_function(text: str) -> str:
    start = text.index("path_has_symlink_component() {")
    end = text.index("\n\nneeds_chown=false", start)
    return text[start:end]


def _run_helper(
    text: str,
    target: Path,
    log_path: Path,
    *,
    orion_home: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("sh not available")
    orion_home = target if orion_home is None else orion_home
    script = (
        "set -eu\n"
        f'ORION_HOME="{orion_home}"\n'
        f"{_chown_orion_tree_function(text)}\n"
        f'chown() {{ printf "%s\\n" "$*" >> "{log_path}"; }}\n'
        f'chown_orion_tree "{target}"\n'
    )
    return subprocess.run([shell, "-c", script], capture_output=True, text=True)


def test_chown_helper_repairs_real_directories(stage2_text: str, tmp_path: Path) -> None:
    target = tmp_path / "home"
    target.mkdir()
    log_path = tmp_path / "chown.log"

    proc = _run_helper(stage2_text, target, log_path)

    assert proc.returncode == 0, proc.stderr
    assert log_path.read_text().splitlines() == [
        f"-R orion:orion {target}",
    ]


def test_chown_helper_refuses_symlinked_directories(stage2_text: str, tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    symlinked_home = tmp_path / "orion-home"
    try:
        symlinked_home.symlink_to(real_home, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are not available on this platform")
    log_path = tmp_path / "chown.log"

    proc = _run_helper(stage2_text, symlinked_home, log_path)

    assert proc.returncode == 0, proc.stderr
    assert not log_path.exists()
    assert "refusing recursive chown through symlinked path" in proc.stdout


def test_chown_helper_refuses_target_under_symlinked_home(
    stage2_text: str,
    tmp_path: Path,
) -> None:
    real_home = tmp_path / "real-home"
    (real_home / "cron").mkdir(parents=True)
    linked_home = tmp_path / "linked-home"
    try:
        linked_home.symlink_to(real_home, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are not available on this platform")
    log_path = tmp_path / "chown.log"

    proc = _run_helper(
        stage2_text,
        linked_home / "cron",
        log_path,
        orion_home=linked_home,
    )

    assert proc.returncode == 0, proc.stderr
    assert not log_path.exists(), "must not chown through a symlinked ORION_HOME"
    assert "refusing recursive chown through symlinked path" in proc.stdout


def test_stage2_uses_symlink_safe_helper_for_orion_home_trees(stage2_text: str) -> None:
    assert 'chown_orion_tree "$ORION_HOME/$sub"' in stage2_text
    assert 'chown_orion_tree "$ORION_HOME/profiles"' in stage2_text
    assert 'chown_orion_tree "$ORION_HOME/cron"' in stage2_text
    assert 'chown -R orion:orion "$ORION_HOME/$sub"' not in stage2_text
    assert 'chown -R orion:orion "$ORION_HOME/profiles"' not in stage2_text
    assert 'chown -R orion:orion "$ORION_HOME/cron"' not in stage2_text


def test_stage2_skips_top_level_chown_for_symlinked_orion_home(
    stage2_text: str,
) -> None:
    assert 'refuse_symlinked_path "chown" "$ORION_HOME"' in stage2_text


def test_stage2_skips_recursive_repairs_when_tree_is_already_owned(
    stage2_text: str,
) -> None:
    assert "tree_has_non_orion_owner() {" in stage2_text
    assert 'if [ -e "$ORION_HOME/$sub" ] && tree_has_non_orion_owner "$ORION_HOME/$sub"; then' in stage2_text
    assert 'if [ -d "$ORION_HOME/profiles" ] && tree_has_non_orion_owner "$ORION_HOME/profiles"; then' in stage2_text
    # Sibling every-boot chown blocks carry the same warm-boot gate.
    assert 'if [ -d "$ORION_HOME/cron" ] && tree_has_non_orion_owner "$ORION_HOME/cron"; then' in stage2_text
    assert 'if [ -d "$ORION_HOME/platforms/pairing" ] && tree_has_non_orion_owner "$ORION_HOME/platforms/pairing"; then' in stage2_text
    assert 'if [ -d "$ORION_HOME/pairing" ] && tree_has_non_orion_owner "$ORION_HOME/pairing"; then' in stage2_text
