"""#76728: the gateway runtime-status write guard keeps the test suite from
overwriting the operator's real ``~/.hermes/gateway_state.json``.

``write_runtime_status`` stamps the calling process's pid/argv into the file
unconditionally, so a test that ran with HERMES_HOME still pointing at the real
root would clobber the live gateway's identity (and it never self-heals). This
is fail-closed **defense-in-depth**, mirroring the kanban write guard: the
per-test ``_isolate_env`` fixture already re-points HERMES_HOME at a tmp dir, so
the guard normally never fires — it exists to catch a REGRESSION of that
isolation before it can touch the real root. The autouse guard in
``tests/conftest.py`` refuses any write whose resolved status path lands under
the REAL Hermes root.
"""

from __future__ import annotations

import pytest

# Import so the module is in sys.modules when the autouse guard's probe runs.
from gateway import status


def test_write_succeeds_under_test_home():
    """Under the hermetic HERMES_HOME tempdir, the write is allowed and lands in
    the sandbox — the deny-list only trips on the real root."""
    path = status._get_runtime_status_path()
    status.write_runtime_status(gateway_state="running")
    assert path.exists()
    record = status.read_runtime_status()
    assert record is not None
    assert record["gateway_state"] == "running"


def test_write_raises_when_status_path_is_real_root(tmp_path, monkeypatch):
    """A write whose resolved path is under the (captured) real root is refused.

    The real root is redirected to a tempdir for this test so a guard
    regression can only touch the tempdir, never the operator's actual file.
    """
    import tests.conftest as _conftest

    fake_real = (tmp_path / "real_hermes").resolve()
    fake_real.mkdir()
    monkeypatch.setattr(_conftest, "_REAL_HERMES_ROOT", fake_real)
    monkeypatch.setattr(
        status, "_get_runtime_status_path", lambda: fake_real / "gateway_state.json"
    )

    with pytest.raises(RuntimeError, match="gateway_state_write_guard"):
        status.write_runtime_status(gateway_state="running")
    # The refusal happened before any write.
    assert not (fake_real / "gateway_state.json").exists()


def test_write_allowed_for_sibling_tempdir_even_when_real_root_exists(
    tmp_path, monkeypatch
):
    """Hermetic tests moving HERMES_HOME to a sibling tempdir are unaffected by
    the deny-list, even though a distinct real root is registered."""
    import tests.conftest as _conftest

    fake_real = (tmp_path / "real_hermes").resolve()
    fake_real.mkdir()
    sibling = (tmp_path / "sandbox_home").resolve()
    sibling.mkdir()
    monkeypatch.setattr(_conftest, "_REAL_HERMES_ROOT", fake_real)
    monkeypatch.setattr(
        status, "_get_runtime_status_path", lambda: sibling / "gateway_state.json"
    )

    status.write_runtime_status(gateway_state="running")
    assert (sibling / "gateway_state.json").exists()
    assert not (fake_real / "gateway_state.json").exists()
