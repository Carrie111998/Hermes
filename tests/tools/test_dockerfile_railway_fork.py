"""Fork-owned contract tests for the Railway divergences in the Dockerfile.

Upstream used to guard the mutable-state contract in
``test_dockerfile_immutable_install.py::test_dockerfile_keeps_mutable_state_under_opt_data``,
which asserted ``VOLUME [ "/opt/data" ]``. That test was deleted upstream, and
our fork had been re-patching it on every merge because Railway *rejects*
images that declare a VOLUME.

These assertions live in a fork-only file so upstream never touches them: the
Railway contract stays guarded without re-conflicting each merge cycle.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text()


def test_dockerfile_keeps_mutable_state_under_opt_data() -> None:
    text = _dockerfile_text()

    assert "ENV HERMES_HOME=/opt/data" in text
    assert "ENV HERMES_WRITE_SAFE_ROOT=/opt/data" in text


def test_dockerfile_omits_volume_but_creates_the_mount_point() -> None:
    """Railway rejects images declaring a VOLUME; we mount a Railway volume.

    The mount point must still exist in the image, otherwise a cold boot has
    nowhere to land /opt/data before the volume attaches.
    """
    text = _dockerfile_text()

    assert 'VOLUME [ "/opt/data" ]' not in text
    assert "RUN mkdir -p /opt/data" in text


def test_dockerfile_disables_runtime_install_mutations() -> None:
    text = _dockerfile_text()

    assert "ENV PYTHONDONTWRITEBYTECODE=1" in text
    assert "ENV HERMES_DISABLE_LAZY_INSTALLS=1" in text
    assert "HERMES_TUI_DIR=/opt/hermes/ui-tui" in text
