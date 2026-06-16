"""Media-intent router for the WebSocket chat panel.

This module keeps narrow media-generation shortcuts out of the large
``tui_gateway.server`` file. It is used only by the WebSocket panel path and
falls back to the normal agent turn for anything ambiguous.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any

from tui_gateway import server


_IMAGE_NOUN_RE = re.compile(
    r"(图片|图像|照片|海报|插画|头像|壁纸|封面|配图|image|picture|photo|poster|illustration|avatar|wallpaper)",
    re.IGNORECASE,
)
_IMAGE_VERB_RE = re.compile(
    r"(生成|做|画|创建|制作|出一张|来一张|generate|create|draw|make)",
    re.IGNORECASE,
)
_VIDEO_NOUN_RE = re.compile(r"(视频|影片|短片|动画|video|movie|clip|reel)", re.IGNORECASE)
_ATTACHMENT_MARKER_RE = re.compile(
    r"\[(?:Attached image path for tools|User attached image):", re.IGNORECASE
)


def try_handle_prompt_submit(req: Any, transport: Any) -> dict | None:
    """Handle direct Atlas image turns, or return ``None`` for normal dispatch."""
    if not isinstance(req, dict) or req.get("method") != "prompt.submit":
        return None
    params = req.get("params")
    if not isinstance(params, dict):
        return None
    if params.get("truncate_before_user_ordinal") is not None:
        return None

    rid = req.get("id")
    text = str(params.get("text") or "").strip()
    if not _is_direct_image_generation_request(text):
        return None

    session, err = server._sess_nowait(params, rid)
    if err:
        return err
    if session.get("attached_images") or _ATTACHMENT_MARKER_RE.search(text):
        return None

    with session["history_lock"]:
        if session.get("running"):
            return server._err(rid, 4009, "session busy")
        session["transport"] = transport
        session["running"] = True
        session["last_active"] = time.time()
        server._start_inflight_turn(session, text)

    server._ensure_session_db_row(session)
    threading.Thread(
        target=_run_atlas_image_turn,
        args=(str(params.get("session_id") or ""), session, text),
        daemon=True,
    ).start()
    return server._ok(rid, {"status": "streaming", "router": "atlas_image"})


def _is_direct_image_generation_request(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    if _VIDEO_NOUN_RE.search(value):
        return False
    return bool(_IMAGE_NOUN_RE.search(value) and _IMAGE_VERB_RE.search(value))


def _infer_aspect_ratio(text: str) -> str:
    value = (text or "").lower()
    if any(token in value for token in ("9:16", "竖版", "竖图", "portrait")):
        return "portrait"
    if any(token in value for token in ("16:9", "横版", "横图", "landscape")):
        return "landscape"
    if any(token in value for token in ("1:1", "方图", "头像", "icon", "avatar", "square")):
        return "square"
    return "square"


def _run_atlas_image_turn(sid: str, session: dict, prompt: str) -> None:
    tool_id = f"image_generate_{uuid.uuid4().hex[:8]}"
    status = "complete"
    assistant_text = ""
    try:
        server._emit("message.start", sid)
        server._emit(
            "status.update",
            sid,
            {"kind": "creating", "text": "Creating image with Atlas"},
        )
        server._emit(
            "tool.start",
            sid,
            {"tool_id": tool_id, "name": "image_generate", "context": prompt},
        )

        result = _generate_atlas_image(prompt, _infer_aspect_ratio(prompt))
        if not isinstance(result, dict):
            raise RuntimeError("Atlas image provider returned a non-dict result")

        if result.get("success") and isinstance(result.get("image"), str):
            image = str(result["image"])
            assistant_text = _format_image_success(result, image)
            server._emit(
                "tool.complete",
                sid,
                {
                    "tool_id": tool_id,
                    "name": "image_generate",
                    "summary": "Atlas image generated",
                    "result": result,
                },
            )
        else:
            status = "error"
            error = str(result.get("error") or "Atlas image generation failed")
            assistant_text = f"图片生成失败：{error}"
            server._emit(
                "tool.complete",
                sid,
                {"tool_id": tool_id, "name": "image_generate", "error": error, "result": result},
            )

        _append_turn_to_history(session, prompt, assistant_text, sid)
        server._emit("message.complete", sid, {"text": assistant_text, "status": status})
    except Exception as exc:
        status = "error"
        assistant_text = f"图片生成失败：{exc}"
        server._emit(
            "tool.complete",
            sid,
            {"tool_id": tool_id, "name": "image_generate", "error": str(exc)},
        )
        _append_turn_to_history(session, prompt, assistant_text, sid)
        server._emit("message.complete", sid, {"text": assistant_text, "status": status})
    finally:
        with session["history_lock"]:
            session["running"] = False
            session["last_active"] = time.time()
            server._clear_inflight_turn(session)


def _generate_atlas_image(prompt: str, aspect_ratio: str) -> dict:
    from plugins.image_gen.atlas import AtlasImageGenProvider

    return AtlasImageGenProvider().generate(prompt=prompt, aspect_ratio=aspect_ratio)


def _format_image_success(result: dict, image: str) -> str:
    model = str(result.get("model") or result.get("atlas_model") or "atlas").strip()
    if image.startswith(("http://", "https://")):
        return f"已通过 Atlas 生成图片。\n\n![Atlas generated image]({image})\n\n模型：{model}"
    return f"已通过 Atlas 生成图片。\n\n生成结果：{image}\n\n模型：{model}"


def _append_turn_to_history(session: dict, user_text: str, assistant_text: str, sid: str) -> None:
    with session["history_lock"]:
        history = session.setdefault("history", [])
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": assistant_text})
        session["history_version"] = int(session.get("history_version", 0)) + 1
        server._clear_inflight_turn(session)

    session_key = str(session.get("session_key") or "")
    if not session_key:
        return
    try:
        with server._session_db(session) as db:
            if db is None:
                return
            db.append_message(session_id=session_key, role="user", content=user_text)
            db.append_message(session_id=session_key, role="assistant", content=assistant_text)
    except Exception as exc:
        server._emit(
            "status.update",
            sid,
            {
                "kind": "history",
                "text": f"Response is visible but history save failed: {exc}",
            },
        )
