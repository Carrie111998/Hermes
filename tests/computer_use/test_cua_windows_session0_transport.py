"""Windows Session-0 transport policy for cua-driver."""

from tools.computer_use.cua_backend import _standard_mcp_transport_args


_DEFAULT_PIPE = r"\\.\pipe\cua-driver"


def test_session_zero_routes_standard_mcp_to_interactive_daemon():
    args = _standard_mcp_transport_args(
        ["mcp"],
        platform="win32",
        windows_session_id=0,
    )

    assert args == ["mcp", "--socket", _DEFAULT_PIPE]


def test_interactive_windows_session_keeps_direct_standard_runtime():
    args = _standard_mcp_transport_args(
        ["mcp"],
        platform="win32",
        windows_session_id=3,
    )

    assert args == ["mcp"]


def test_unknown_windows_session_fails_closed_to_driver_guard():
    args = _standard_mcp_transport_args(
        ["mcp"],
        platform="win32",
        windows_session_id=None,
    )

    assert args == ["mcp"]


def test_non_windows_standard_runtime_is_unchanged():
    args = _standard_mcp_transport_args(
        ["mcp"],
        platform="linux",
        windows_session_id=0,
    )

    assert args == ["mcp"]
