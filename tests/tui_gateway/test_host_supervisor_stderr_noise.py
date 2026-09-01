"""#54833: HostSupervisor stderr drain must skip the benign malloc line."""

import io
from types import SimpleNamespace

import pytest

from tui_gateway.host_supervisor import HostSupervisor

_MALLOC_NOISE = (
    "python(16414) MallocStackLogging: can't turn off malloc stack logging "
    "because it was not enabled."
)


def _bare_supervisor() -> HostSupervisor:
    sup = HostSupervisor.__new__(HostSupervisor)
    sup._stderr_tail = []
    return sup


@pytest.fixture
def _darwin(monkeypatch):
    from hermes_cli import subprocess_noise

    monkeypatch.setattr(subprocess_noise, "sys", SimpleNamespace(platform="darwin"))


def test_drain_skips_noise_keeps_real_lines(_darwin):
    sup = _bare_supervisor()
    proc = SimpleNamespace(
        stderr=io.StringIO(f"real line\n{_MALLOC_NOISE}\nsecond line\n")
    )

    sup._drain_stderr(proc)  # type: ignore[arg-type]

    assert sup._stderr_tail == ["real line", "second line"]


def test_drain_keeps_noise_off_darwin(monkeypatch):
    from hermes_cli import subprocess_noise

    monkeypatch.setattr(subprocess_noise, "sys", SimpleNamespace(platform="linux"))
    sup = _bare_supervisor()
    proc = SimpleNamespace(stderr=io.StringIO(f"{_MALLOC_NOISE}\n"))

    sup._drain_stderr(proc)  # type: ignore[arg-type]

    assert sup._stderr_tail == [_MALLOC_NOISE]
