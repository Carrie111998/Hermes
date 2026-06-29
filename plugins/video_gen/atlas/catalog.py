"""AtlasCloud video model catalog and routing helpers."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "wan-2.6-flash"
DEFAULT_DURATION = 5
DEFAULT_RESOLUTION = "720p"
VALID_ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")


ATLAS_FAMILIES: Dict[str, Dict[str, Any]] = {
    "wan-2.6-flash": {
        "display": "Wan 2.6 Flash",
        "speed": "fast",
        "price": "Atlas paid",
        "strengths": "Fast Atlas Wan route. Good default for basic motion and uploaded images.",
        "text_model": "alibaba/wan-2.6/text-to-video",
        "image_model": "alibaba/wan-2.6/image-to-video-flash",
        "durations": (5, 10, 15),
        "resolutions": ("720P",),
        "audio": False,
    },
    "wan-2.6": {
        "display": "Wan 2.6",
        "speed": "standard",
        "price": "Atlas paid",
        "strengths": "Wan 2.6 standard route; supports 720p and 1080p image-to-video.",
        "text_model": "alibaba/wan-2.6/text-to-video",
        "image_model": "alibaba/wan-2.6/image-to-video",
        "durations": (5, 10, 15),
        "resolutions": ("720P", "1080P"),
        "audio": True,
    },
    "seedance-1.5-pro-fast": {
        "display": "Seedance 1.5 Pro Fast",
        "speed": "fast",
        "price": "Atlas paid",
        "strengths": "ByteDance fast text-to-video and image-to-video.",
        "text_model": "bytedance/seedance-v1.5-pro/text-to-video-fast",
        "image_model": "bytedance/seedance-v1.5-pro/image-to-video-fast",
        "durations": (5, 10),
        "resolutions": ("720P", "1080P"),
        "audio": False,
    },
    "kling-v3-pro": {
        "display": "Kling v3 Pro",
        "speed": "standard",
        "price": "Atlas paid",
        "strengths": "Kling v3 Pro image-to-video with negative prompt, native sound, and start-frame guidance.",
        "text_model": "kwaivgi/kling-v3.0-pro/text-to-video",
        "image_model": "kwaivgi/kling-v3.0-pro/image-to-video",
        "durations": tuple(range(3, 16)),
        "resolutions": ("1080P", "1440P-SR"),
        "audio": True,
        "audio_param": "sound",
        "negative": True,
    },
    "veo3.1": {
        "display": "Veo 3.1",
        "speed": "premium",
        "price": "Atlas paid",
        "strengths": "Google Veo 3.1 via Atlas for higher-quality cinematic shots.",
        "text_model": "google/veo3.1/text-to-video",
        "image_model": "google/veo3.1/image-to-video",
        "durations": (5, 10),
        "resolutions": ("720P", "1080P"),
        "audio": True,
    },
    "sora-2": {
        "display": "Sora 2",
        "speed": "premium",
        "price": "Atlas paid",
        "strengths": "OpenAI Sora 2 via Atlas.",
        "text_model": "openai/sora-2/text-to-video",
        "image_model": "openai/sora-2/image-to-video",
        "durations": (5, 10),
        "resolutions": ("720P", "1080P"),
        "audio": True,
    },
}


def load_video_gen_section() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("video_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load video_gen config: %s", exc)
        return {}


def family_modalities(family: Dict[str, Any]) -> List[str]:
    modes: List[str] = []
    if family.get("text_model"):
        modes.append("text")
    if family.get("image_model"):
        modes.append("image")
    return modes


def _full_model_map() -> Dict[str, Tuple[str, str]]:
    out: Dict[str, Tuple[str, str]] = {}
    for family_id, family in ATLAS_FAMILIES.items():
        if family.get("text_model"):
            out[str(family["text_model"])] = (family_id, "text")
        if family.get("image_model"):
            out[str(family["image_model"])] = (family_id, "image")
    return out


def _candidate_models(explicit: Optional[str]) -> List[str]:
    candidates: List[Optional[str]] = [explicit, os.environ.get("ATLAS_VIDEO_MODEL")]
    cfg = load_video_gen_section()
    atlas_cfg = cfg.get("atlas") if isinstance(cfg.get("atlas"), dict) else {}
    if isinstance(atlas_cfg, dict):
        candidates.append(atlas_cfg.get("model"))
    top = cfg.get("model")
    if isinstance(top, str):
        candidates.append(top)
    return [c.strip() for c in candidates if isinstance(c, str) and c.strip()]


def resolve_family_and_model(
    explicit: Optional[str],
    *,
    modality: str,
) -> Tuple[str, Dict[str, Any], str, Optional[str]]:
    """Return family id, metadata, Atlas model id, and optional error."""
    full_models = _full_model_map()
    for candidate in _candidate_models(explicit):
        if candidate in ATLAS_FAMILIES:
            family = ATLAS_FAMILIES[candidate]
            atlas_model = family.get(f"{modality}_model")
            if atlas_model:
                return candidate, family, str(atlas_model), None
            return (
                candidate,
                family,
                "",
                f"Atlas family {candidate} does not support {modality}-to-video.",
            )

        if candidate in full_models:
            family_id, model_modality = full_models[candidate]
            family = ATLAS_FAMILIES[family_id]
            if model_modality != modality:
                return (
                    family_id,
                    family,
                    "",
                    f"Atlas model {candidate} is {model_modality}-to-video only.",
                )
            return family_id, family, candidate, None

    family = ATLAS_FAMILIES[DEFAULT_MODEL]
    atlas_model = str(family[f"{modality}_model"])
    return DEFAULT_MODEL, family, atlas_model, None


def clamp_duration(family: Dict[str, Any], duration: Optional[int]) -> int:
    value = duration if duration is not None else DEFAULT_DURATION
    durations = family.get("durations")
    if not durations:
        return max(1, value)
    if len(durations) == 2 and durations[1] - durations[0] > 1:
        return max(durations[0], min(durations[1], value))
    if value in durations:
        return value
    return min(durations, key=lambda item: abs(item - value))


def normalize_resolution(family: Dict[str, Any], resolution: str) -> str:
    requested = (resolution or DEFAULT_RESOLUTION).strip().upper().replace("_", "-")
    atlas_resolution = (
        requested
        if requested.endswith(("P", "P-SR"))
        else f"{requested}P"
    )
    resolutions = family.get("resolutions") or ()
    if atlas_resolution in resolutions:
        return atlas_resolution
    return str(resolutions[0]) if resolutions else DEFAULT_RESOLUTION.upper()
