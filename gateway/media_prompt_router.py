"""Direct media prompt helpers for authenticated API clients."""

from __future__ import annotations

import re
from typing import Any

_IMAGE_NOUN_RE = re.compile(
    r"(图片|图像|照片|海报|插画|头像|壁纸|封面|配图|image|picture|photo|poster|illustration|avatar|wallpaper)",
    re.IGNORECASE,
)
_IMAGE_VERB_RE = re.compile(
    r"(生成|做|画|创建|制作|出一张|来一张|generate|create|draw|make)",
    re.IGNORECASE,
)
_VIDEO_NOUN_RE = re.compile(r"(视频|影片|短片|动画|video|movie|clip|reel)", re.IGNORECASE)


def is_direct_image_prompt(message: Any) -> bool:
    if not isinstance(message, str):
        return False
    text = message.strip()
    if not text or _VIDEO_NOUN_RE.search(text):
        return False
    return bool(_IMAGE_NOUN_RE.search(text) and _IMAGE_VERB_RE.search(text))


def infer_aspect_ratio(text: str) -> str:
    value = (text or "").lower()
    if any(token in value for token in ("9:16", "竖版", "竖图", "portrait")):
        return "portrait"
    if any(token in value for token in ("16:9", "横版", "横图", "landscape")):
        return "landscape"
    return "square"


def generate_atlas_image(prompt: str) -> dict[str, Any]:
    from plugins.image_gen.atlas import AtlasImageGenProvider

    return AtlasImageGenProvider().generate(
        prompt=prompt,
        aspect_ratio=infer_aspect_ratio(prompt),
    )


def image_response_text(result: dict[str, Any]) -> str:
    image = str(result.get("image") or "").strip()
    if result.get("success") and image:
        model = str(result.get("model") or result.get("atlas_model") or "atlas")
        if image.startswith(("http://", "https://")):
            return f"已通过 Atlas 生成图片。\n\n![Atlas generated image]({image})\n\n模型：{model}"
        return f"已通过 Atlas 生成图片。\n\n生成结果：{image}\n\n模型：{model}"
    error = str(result.get("error") or "Atlas image generation failed")
    return f"图片生成失败：{error}"
