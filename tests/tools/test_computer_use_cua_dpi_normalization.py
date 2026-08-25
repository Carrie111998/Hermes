"""Regression tests for Windows HiDPI coordinate normalization (#94538).

cua-driver's Windows backend captures screenshots in physical pixels while
its input dispatch (SendInput) operates in logical units, so on a scaled
display (e.g. 150%) a click at screenshot coordinate ``[x, y]`` lands at
``[x * scale, y * scale]`` and misses the intended element. The Hermes
wrapper must divide coordinate inputs by the display scale factor
(DPI / 96) before dispatching them to the driver.

These tests pin that contract without needing a live cua-driver binary:
the DPI probe is patched and the MCP args the backend would send are
asserted directly.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest


class _FakeSession:
    """Minimal stand-in for ``_CuaDriverSession`` recording tool-call args."""

    def __init__(
        self,
        out: Optional[Dict[str, Any]] = None,
        *,
        scroll_coords: bool = True,
    ) -> None:
        self.out = out or {
            "isError": False,
            "data": {},
            "structuredContent": {"effect": "confirmed"},
        }
        self.scroll_coords = scroll_coords
        self.calls = []  # type: list[tuple[str, Dict[str, Any]]]

    def call_tool(self, name: str, args: Dict[str, Any], timeout: float = 30.0):
        self.calls.append((name, dict(args)))
        return self.out

    def supports_capability(self, capability: str, tool: Optional[str] = None) -> bool:
        return self.scroll_coords and capability == "input.scroll.coordinates"

    def supports_input_property(self, tool: str, prop: str) -> bool:
        return False

    def _has_tool(self, name: str) -> bool:
        return True


def _make_backend(session: _FakeSession):
    from tools.computer_use.cua_backend import CuaDriverBackend

    backend = CuaDriverBackend.__new__(CuaDriverBackend)
    backend._session = session
    backend._session_id = "hermes-session"
    backend._snapshot_tokens = {}
    backend._active_pid = 4242
    backend._active_window_id = 77
    return backend


def _last_call(session: _FakeSession):
    assert session.calls, "expected a cua-driver tool call"
    return session.calls[-1]


@pytest.fixture(autouse=True)
def _sanitize_env(monkeypatch):
    """The kill switch is env/config driven; keep each test opt-in clean."""
    monkeypatch.delenv("HERMES_CUA_NO_DPI_NORMALIZATION", raising=False)
    yield


def _patch_scale(monkeypatch, scale):
    from tools.computer_use import cua_backend

    monkeypatch.setattr(cua_backend, "_win32_dpi_scale", lambda pid: scale)
    return cua_backend


def test_click_coordinates_are_divided_by_scale_on_windows_hidpi(monkeypatch):
    """150% DPI: screenshot-space [300, 450] must dispatch as [200, 300]."""
    _patch_scale(monkeypatch, 1.5)
    session = _FakeSession()
    backend = _make_backend(session)

    result = backend.click(x=300, y=450)

    assert result.ok
    _, args = _last_call(session)
    assert args["x"] == 200
    assert args["y"] == 300


def test_double_click_coordinates_are_normalized(monkeypatch):
    _patch_scale(monkeypatch, 1.5)
    session = _FakeSession()
    backend = _make_backend(session)

    backend.click(x=300, y=450, click_count=2)

    name, args = _last_call(session)
    assert name == "double_click"
    assert args["x"] == 200
    assert args["y"] == 300


def test_drag_coordinates_are_normalized(monkeypatch):
    _patch_scale(monkeypatch, 1.5)
    session = _FakeSession()
    backend = _make_backend(session)

    backend.drag(from_xy=(300, 450), to_xy=(600, 900))

    _, args = _last_call(session)
    assert (args["from_x"], args["from_y"]) == (200, 300)
    assert (args["to_x"], args["to_y"]) == (400, 600)


def test_scroll_coordinates_are_normalized(monkeypatch):
    _patch_scale(monkeypatch, 1.5)
    session = _FakeSession(scroll_coords=True)
    backend = _make_backend(session)

    backend.scroll(direction="down", amount=3, x=300, y=450)

    _, args = _last_call(session)
    assert (args["x"], args["y"]) == (200, 300)


def test_normalization_rounds_to_nearest_integer(monkeypatch):
    """445 / 1.5 = 296.67 → 297, matching the issue's 285/1.5-style math."""
    _patch_scale(monkeypatch, 1.5)
    session = _FakeSession()
    backend = _make_backend(session)

    backend.click(x=445, y=285)

    _, args = _last_call(session)
    assert (args["x"], args["y"]) == (297, 190)


def test_coordinates_unchanged_when_scale_is_unavailable(monkeypatch):
    """A driver/host that cannot report DPI must behave exactly as before."""
    _patch_scale(monkeypatch, None)
    session = _FakeSession()
    backend = _make_backend(session)

    backend.click(x=300, y=450)
    backend.drag(from_xy=(300, 450), to_xy=(600, 900))
    backend.scroll(direction="down", amount=3, x=300, y=450)

    _, click_args = session.calls[0]
    assert (click_args["x"], click_args["y"]) == (300, 450)
    _, drag_args = session.calls[1]
    assert (drag_args["from_x"], drag_args["from_y"]) == (300, 450)
    assert (drag_args["to_x"], drag_args["to_y"]) == (600, 900)
    _, scroll_args = session.calls[2]
    assert (scroll_args["x"], scroll_args["y"]) == (300, 450)


def test_coordinates_unchanged_at_100_percent_scale(monkeypatch):
    _patch_scale(monkeypatch, 1.0)
    session = _FakeSession()
    backend = _make_backend(session)

    backend.click(x=300, y=450)

    _, args = _last_call(session)
    assert (args["x"], args["y"]) == (300, 450)


def test_env_kill_switch_disables_normalization(monkeypatch):
    _patch_scale(monkeypatch, 1.5)
    monkeypatch.setenv("HERMES_CUA_NO_DPI_NORMALIZATION", "1")
    session = _FakeSession()
    backend = _make_backend(session)

    backend.click(x=300, y=450)

    _, args = _last_call(session)
    assert (args["x"], args["y"]) == (300, 450)


def test_config_kill_switch_disables_normalization(monkeypatch):
    from tools.computer_use import cua_backend

    _patch_scale(monkeypatch, 1.5)
    monkeypatch.setattr(
        cua_backend, "_computer_use_cfg", lambda: {"dpi_normalization": False}
    )
    session = _FakeSession()
    backend = _make_backend(session)

    backend.click(x=300, y=450)

    _, args = _last_call(session)
    assert (args["x"], args["y"]) == (300, 450)


def test_element_index_clicks_are_not_scaled(monkeypatch):
    """Element clicks resolve server-side in the driver's own space."""
    _patch_scale(monkeypatch, 1.5)
    session = _FakeSession()
    backend = _make_backend(session)

    backend.click(element=3)

    _, args = _last_call(session)
    assert args["element_index"] == 3
    assert "x" not in args and "y" not in args


def test_win32_dpi_scale_returns_none_off_windows(monkeypatch):
    """The raw probe must be a no-op on non-Windows hosts."""
    from tools.computer_use import cua_backend

    monkeypatch.setattr(cua_backend.sys, "platform", "linux")
    assert cua_backend._win32_dpi_scale(4242) is None


def test_normalize_coordinate_helper(monkeypatch):
    from tools.computer_use.cua_backend import _normalize_coordinate

    assert _normalize_coordinate(300, 1.5) == 200
    assert _normalize_coordinate(445, 1.5) == 297
    assert _normalize_coordinate(230, 1.25) == 184
    assert _normalize_coordinate(0, 2.0) == 0
    assert _normalize_coordinate(300, 1.0) == 300
    assert _normalize_coordinate(300, None) == 300
