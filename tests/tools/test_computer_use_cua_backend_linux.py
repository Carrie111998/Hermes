"""Regression tests for Linux/X11 capture target selection (#58026, #54173)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

# Tied z_index=0 fixture from #58026 (ding ahead of real terminals).
ISSUE_58026_WINDOWS = [
    {
        "app_name": "ding",
        "pid": 4294,
        "window_id": 33554439,
        "title": "Desktop Icons 1",
        "is_on_screen": True,
        "z_index": 0,
    },
    {
        "app_name": "",
        "pid": 1816017,
        "window_id": 60817412,
        "title": "zcode",
        "is_on_screen": True,
        "z_index": 0,
    },
    {
        "app_name": "",
        "pid": 1877178,
        "window_id": 84043449,
        "title": "xr@10:~/hermes",
        "is_on_screen": True,
        "z_index": 0,
    },
    {
        "app_name": "",
        "pid": 1877178,
        "window_id": 84065715,
        "title": "HERMES-CU",
        "is_on_screen": True,
        "z_index": 0,
    },
]

# Linux metadata-quirk fixture from #54173 (null is_on_screen, GNOME Shell
# @!x,y;BDHF backdrop helper ahead of real app windows).
LINUX_LIST_WINDOWS = [
    {
        "app_name": "",
        "pid": 2951331,
        "window_id": 98566147,
        "title": "@!1921,0;BDHF",
        "is_on_screen": None,
        "z_index": 0,
    },
    {
        "app_name": "",
        "pid": 11715,
        "window_id": 81790890,
        "title": "Guides — OMC Docs - Google Chrome",
        "is_on_screen": None,
        "z_index": 0,
    },
    {
        "app_name": "",
        "pid": 11433,
        "window_id": 41943052,
        "title": "README.md - hermes-agent - Visual Studio Code",
        "is_on_screen": False,
        "z_index": 0,
    },
]


def _normalized_windows(raw=ISSUE_58026_WINDOWS):
    from tools.computer_use.cua_backend import _ingest_windows

    return _ingest_windows(raw)


def test_parse_xprop_net_active_window_standard_output():
    from tools.computer_use.cua_backend import _parse_xprop_net_active_window

    raw = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x503000b\n"
    assert _parse_xprop_net_active_window(raw) == 0x503000b


@pytest.mark.linux_only
def test_default_capture_prefers_x11_active_window_when_z_index_tied():
    """The ``_NET_ACTIVE_WINDOW`` tie-break is a Linux/X11-only branch of
    ``_select_capture_target``; run it where ``sys.platform`` really is
    linux instead of patching the branch selector."""
    from tools.computer_use.cua_backend import _select_capture_target

    windows = _normalized_windows()

    with patch(
        "tools.computer_use.cua_backend._linux_x11_active_window_id",
        return_value=84043449,
    ):
        target = _select_capture_target(windows, app_requested=False)

    assert target["title"] == "xr@10:~/hermes"
    assert target["window_id"] == 84043449


@pytest.mark.linux_only
def test_default_capture_skips_desktop_helper_when_active_window_unknown():
    """Even without _NET_ACTIVE_WINDOW, ding/Desktop helpers must not win (#54173).

    Linux-only: the helper-skipping pool filter is inside the
    ``sys.platform == "linux"`` branch."""
    from tools.computer_use.cua_backend import _select_capture_target

    windows = _normalized_windows()

    with patch(
        "tools.computer_use.cua_backend._linux_x11_active_window_id",
        return_value=None,
    ):
        target = _select_capture_target(windows, app_requested=False)

    # "Desktop Icons 1" is a shell helper window that captures as empty; with
    # the active window unknown, the first REAL app window wins list order.
    assert target["window_id"] == 60817412
    assert target["title"] == "zcode"


def test_linux_null_is_on_screen_is_treated_as_unknown_not_offscreen():
    """cua-driver 0.6.x may return JSON null for Linux is_on_screen (#54173)."""
    windows = _normalized_windows(LINUX_LIST_WINDOWS)

    assert windows[0]["off_screen"] is False
    assert windows[1]["off_screen"] is False
    assert windows[2]["off_screen"] is True


def test_explicit_app_capture_preserves_filtered_target_order():
    """When the caller filters first, target selection should not skip the match."""
    from tools.computer_use.cua_backend import _select_capture_target

    chrome = _normalized_windows(LINUX_LIST_WINDOWS)[1]

    assert _select_capture_target([chrome], app_requested=True) == chrome


@pytest.mark.linux_only
def test_wayland_screen_discovers_desktop_tool_before_cold_session_route(monkeypatch):
    """The first safe call must discover tools before choosing a capture route."""
    from tools.computer_use.cua_backend import CuaDriverBackend

    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAADUlEQVR4nG"
        "NgGAUgAAABCAABgukLHQAAAABJRU5ErkJggg=="
    )
    calls = []

    class _ColdWaylandSession:
        capabilities_discovered = False

        def __init__(self):
            self.tools = set()

        def _has_tool(self, name):
            return name in self.tools

        def call_tool(self, name, args, timeout=30.0):
            calls.append((name, dict(args)))
            if name == "list_windows":
                self.tools = {"get_desktop_state", "list_windows"}
                self.capabilities_discovered = True
                return {
                    "data": "",
                    "images": [],
                    "structuredContent": {
                        "windows": [{
                            "app_name": "xwayland-terminal",
                            "pid": 4242,
                            "window_id": 99,
                            "title": "Terminal",
                            "is_on_screen": True,
                            "z_index": 1,
                        }],
                    },
                    "isError": False,
                }
            assert name == "get_desktop_state"
            return {
                "data": "",
                "images": [png_b64],
                "image_mime_types": ["image/png"],
                "structuredContent": {},
                "isError": False,
            }

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    backend = CuaDriverBackend()
    backend._session = _ColdWaylandSession()

    capture = backend.capture(mode="vision", app="screen")

    assert capture.png_b64 == png_b64
    assert capture.app == "screen"
    assert calls == [
        (
            "list_windows",
            {"on_screen_only": True, "session": backend._session_id},
        ),
        ("get_desktop_state", {"session": backend._session_id}),
    ]


@pytest.mark.linux_only
def test_wayland_empty_windows_uses_desktop_capture_and_input_target(monkeypatch):
    """Native Wayland can capture and act without an X11 window identity."""
    from tools.computer_use.cua_backend import CuaDriverBackend

    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAADUlEQVR4nG"
        "NgGAUgAAABCAABgukLHQAAAABJRU5ErkJggg=="
    )
    calls = []

    class _WaylandSession:
        capabilities_discovered = True

        def _has_tool(self, name):
            return name in {"get_desktop_state", "click", "type_text"}

        def supports_input_property(self, tool, prop):
            return prop == "target" and tool in {"click", "type_text"}

        def supports_capability(self, capability, tool=None):
            return False

        def _call_tool_via_cli(self, name, args, timeout):
            assert name == "list_windows"
            return {
                "data": "",
                "images": [],
                "structuredContent": {"windows": []},
                "isError": False,
            }

        def call_tool(self, name, args, timeout=30.0):
            calls.append((name, dict(args)))
            if name == "list_windows":
                return {
                    "data": "",
                    "images": [],
                    "structuredContent": {"windows": []},
                    "isError": False,
                }
            if name == "get_desktop_state":
                return {
                    "data": "",
                    "images": [png_b64],
                    "image_mime_types": ["image/png"],
                    "structuredContent": {},
                    "isError": False,
                }
            return {
                "data": "ok",
                "images": [],
                "structuredContent": {},
                "isError": False,
            }

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    backend = CuaDriverBackend()
    backend._session = _WaylandSession()

    capture = backend.capture(mode="som", app="screen")
    clicked = backend.click(x=12, y=34)
    typed = backend.type_text("hello")

    assert capture.png_b64 == png_b64
    assert (capture.width, capture.height) == (8, 8)
    assert capture.app == "screen"
    assert clicked.ok is True
    assert typed.ok is True
    assert calls == [
        ("get_desktop_state", {"session": backend._session_id}),
        (
            "click",
            {
                "button": "left",
                "x": 12,
                "y": 34,
                "target": {"kind": "desktop", "display_id": "primary"},
                "session": backend._session_id,
            },
        ),
        (
            "type_text",
            {
                "text": "hello",
                "target": {"kind": "desktop", "display_id": "primary"},
                "session": backend._session_id,
            },
        ),
    ]


@pytest.mark.linux_only
def test_wayland_screen_prefers_desktop_capture_with_xwayland_windows(monkeypatch):
    """A native desktop request must not be captured through an XWayland window."""
    from tools.computer_use.cua_backend import CuaDriverBackend

    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAADUlEQVR4nG"
        "NgGAUgAAABCAABgukLHQAAAABJRU5ErkJggg=="
    )
    calls = []

    class _MixedWaylandSession:
        capabilities_discovered = True

        def _has_tool(self, name):
            return name == "get_desktop_state"

        def call_tool(self, name, args, timeout=30.0):
            calls.append((name, dict(args)))
            if name == "list_windows":
                return {
                    "data": "",
                    "images": [],
                    "structuredContent": {
                        "windows": [{
                            "app_name": "xwayland-terminal",
                            "pid": 4242,
                            "window_id": 99,
                            "title": "Terminal",
                            "is_on_screen": True,
                            "z_index": 1,
                        }],
                    },
                    "isError": False,
                }
            assert name == "get_desktop_state"
            return {
                "data": "",
                "images": [png_b64],
                "image_mime_types": ["image/png"],
                "structuredContent": {},
                "isError": False,
            }

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    backend = CuaDriverBackend()
    backend._session = _MixedWaylandSession()

    capture = backend.capture(mode="som", app="desktop")

    assert capture.png_b64 == png_b64
    assert capture.app == "desktop"
    assert calls == [
        ("get_desktop_state", {"session": backend._session_id}),
    ]


@pytest.mark.linux_only
def test_wayland_desktop_routes_double_click_drag_and_scroll(monkeypatch):
    """Every retained desktop input branch uses the advertised desktop target."""
    from tools.computer_use.cua_backend import CuaDriverBackend

    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAADUlEQVR4nG"
        "NgGAUgAAABCAABgukLHQAAAABJRU5ErkJggg=="
    )
    calls = []

    class _WaylandInputSession:
        capabilities_discovered = True

        def _has_tool(self, name):
            return name in {"get_desktop_state", "click", "drag", "scroll"}

        def supports_input_property(self, tool, prop):
            return (tool, prop) in {
                ("click", "target"),
                ("click", "count"),
                ("drag", "target"),
                ("scroll", "target"),
                ("scroll", "x"),
                ("scroll", "y"),
            }

        def supports_capability(self, capability, tool=None):
            return False

        def call_tool(self, name, args, timeout=30.0):
            calls.append((name, dict(args)))
            if name == "get_desktop_state":
                return {
                    "data": "",
                    "images": [png_b64],
                    "image_mime_types": ["image/png"],
                    "structuredContent": {},
                    "isError": False,
                }
            return {
                "data": "ok",
                "images": [],
                "structuredContent": {},
                "isError": False,
            }

    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    backend = CuaDriverBackend()
    backend._session = _WaylandInputSession()

    backend.capture(mode="som", app="screen")
    double_clicked = backend.click(x=12, y=34, click_count=2)
    dragged = backend.drag(from_xy=(1, 2), to_xy=(30, 40))
    scrolled = backend.scroll(direction="down", amount=7, x=50, y=60)

    assert double_clicked.ok is True
    assert dragged.ok is True
    assert scrolled.ok is True
    target = {"kind": "desktop", "display_id": "primary"}
    assert calls == [
        ("get_desktop_state", {"session": backend._session_id}),
        (
            "click",
            {
                "button": "left",
                "x": 12,
                "y": 34,
                "target": target,
                "count": 2,
                "session": backend._session_id,
            },
        ),
        (
            "drag",
            {
                "from_x": 1,
                "from_y": 2,
                "to_x": 30,
                "to_y": 40,
                "target": target,
                "session": backend._session_id,
            },
        ),
        (
            "scroll",
            {
                "direction": "down",
                "amount": 7,
                "target": target,
                "x": 50,
                "y": 60,
                "session": backend._session_id,
            },
        ),
    ]
