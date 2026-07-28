import tui_gateway.server as server
from tools.skills_tool import (
    _get_secret_capture_callback,
    bind_secret_capture_callback,
    reset_secret_capture_callback,
)


def test_wire_callbacks_returns_token_and_restores_outer_secret_callback():
    outer_callback = object()
    outer_token = bind_secret_capture_callback(outer_callback)
    turn_token = None
    try:
        turn_token = server._wire_callbacks("session-a")
        turn_callback = _get_secret_capture_callback()
        assert callable(turn_callback)
        assert turn_callback is not outer_callback

        server._reset_secret_capture_token(turn_token)
        turn_token = None
        assert _get_secret_capture_callback() is outer_callback
    finally:
        if turn_token is not None:
            server._reset_secret_capture_token(turn_token)
        reset_secret_capture_callback(outer_token)


def test_reset_secret_capture_token_accepts_no_binding():
    server._reset_secret_capture_token(None)
