import contextlib
import threading
import types

from tui_gateway import media_prompt_router as router
from tui_gateway import server


def _session(**extra):
    return {
        "agent": types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 80,
        "tool_progress_mode": "all",
        **extra,
    }


class _ImmediateThread:
    def __init__(self, target=None, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def test_prompt_submit_routes_direct_image_request_to_atlas(monkeypatch):
    events = []
    session = _session()
    server._sessions["sid"] = session
    monkeypatch.setattr(router.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_session_db", lambda _session: contextlib.nullcontext(None))
    monkeypatch.setattr(
        router,
        "_generate_atlas_image",
        lambda prompt, aspect_ratio: {
            "success": True,
            "image": "https://atlas-media.example/cat.png",
            "model": "nano-banana-2",
            "provider": "atlas",
        },
    )

    try:
        response = router.try_handle_prompt_submit(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {"session_id": "sid", "text": "帮我生成一个猫的图片"},
            },
            transport=object(),
        )
    finally:
        server._sessions.pop("sid", None)

    assert response["result"]["router"] == "atlas_image"
    assert any(evt[0] == "tool.start" and evt[2]["name"] == "image_generate" for evt in events)
    complete = [evt for evt in events if evt[0] == "message.complete"][-1]
    assert "https://atlas-media.example/cat.png" in complete[2]["text"]
    assert session["history"][0]["role"] == "user"
    assert "atlas-media.example" in session["history"][1]["content"]
    assert session["running"] is False


def test_prompt_submit_router_ignores_regular_chat():
    assert (
        router.try_handle_prompt_submit(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {"session_id": "sid", "text": "你是什么模型？"},
            },
            transport=object(),
        )
        is None
    )


def test_prompt_submit_router_ignores_video_request():
    assert (
        router.try_handle_prompt_submit(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {"session_id": "sid", "text": "用这张图片做一个视频"},
            },
            transport=object(),
        )
        is None
    )
