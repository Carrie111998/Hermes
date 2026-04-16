import os
from typing import Any


def build_attachment_capabilities() -> dict[str, Any]:
    return {
        "vision_url": os.getenv("HERMES_MINIAPP_VISION_URL", "").strip(),
        "ocr_url": os.getenv("HERMES_MINIAPP_OCR_URL", "").strip(),
    }


def inject_attachment_hints(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    caps = build_attachment_capabilities()
    injected: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue

        if item.get("type") != "input_file":
            injected.append(item)
            continue

        file_info = item.get("file", {}) or {}
        name = str(file_info.get("name", "")).strip()
        lowered = name.lower()
        mime_type = str(file_info.get("mime_type", "")).strip().lower()
        data_url = str(file_info.get("data_url", "")).strip()

        is_pdf = lowered.endswith(".pdf") or mime_type == "application/pdf"
        is_image = mime_type.startswith("image/") or lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))

        if caps["ocr_url"] and is_pdf:
            injected.append(
                {
                    "type": "text",
                    "text": f"Local OCR is available at {caps['ocr_url']}. Prefer OCR extraction for PDF pages before answering.",
                }
            )

        if caps["vision_url"] and is_image:
            injected.append(
                {
                    "type": "text",
                    "text": f"Local vision is available at {caps['vision_url']}. Prefer vision analysis for image attachments before answering.",
                }
            )

        if is_image and data_url:
            injected.append({"type": "image_url", "image_url": {"url": data_url}})
            continue

        label = name or "attachment"
        if mime_type:
            label = f"{label} ({mime_type})"
        injected.append({"type": "text", "text": f"Attached file: {label}."})

    return injected
