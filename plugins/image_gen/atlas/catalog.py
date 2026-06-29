"""AtlasCloud image model catalog and config helpers."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "nano-banana-2"
DEFAULT_EDIT_MODEL = "nano-banana-edit"

ATLAS_IMAGE_MODELS: Dict[str, Dict[str, Any]] = {
    "nano-banana-2": {
        "display": "Nano Banana 2",
        "speed": "standard",
        "strengths": "Latest Atlas Nano Banana text-to-image route.",
        "price": "Atlas paid",
        "atlas_model": "google/nano-banana-2/text-to-image",
        "edit": False,
    },
    "nano-banana-pro": {
        "display": "Nano Banana Pro",
        "speed": "standard",
        "strengths": "Higher-quality Atlas Nano Banana Pro image generation.",
        "price": "Atlas paid",
        "atlas_model": "google/nano-banana-pro/text-to-image",
        "edit": False,
    },
    "nano-banana": {
        "display": "Nano Banana",
        "speed": "standard",
        "strengths": "Atlas Nano Banana standard text-to-image route.",
        "price": "Atlas paid",
        "atlas_model": "google/nano-banana/text-to-image",
        "edit": False,
    },
    "nano-banana-edit": {
        "display": "Nano Banana Edit",
        "speed": "standard",
        "strengths": "Atlas Nano Banana image edit route for prompt + reference images.",
        "price": "Atlas paid",
        "atlas_model": "google/nano-banana/edit",
        "edit": True,
    },
}


def load_image_gen_section() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def list_model_rows() -> List[Dict[str, str]]:
    return [
        {
            "id": model_id,
            "display": meta["display"],
            "speed": meta["speed"],
            "strengths": meta["strengths"],
            "price": meta["price"],
        }
        for model_id, meta in ATLAS_IMAGE_MODELS.items()
    ]


def _full_model_map() -> Dict[str, str]:
    return {
        meta["atlas_model"]: model_id
        for model_id, meta in ATLAS_IMAGE_MODELS.items()
    }


def _candidate_models(explicit: Optional[str]) -> List[str]:
    candidates: List[Optional[str]] = [explicit, os.environ.get("ATLAS_IMAGE_MODEL")]
    cfg = load_image_gen_section()
    atlas_cfg = cfg.get("atlas") if isinstance(cfg.get("atlas"), dict) else {}
    if isinstance(atlas_cfg, dict):
        candidates.append(atlas_cfg.get("model"))
    top = cfg.get("model")
    if isinstance(top, str):
        candidates.append(top)
    return [item.strip() for item in candidates if isinstance(item, str) and item.strip()]


def resolve_model(explicit: Optional[str] = None, *, edit: bool = False) -> Tuple[str, str]:
    """Return ``(model_id, atlas_model_id)`` for configured Atlas image generation."""
    full_models = _full_model_map()
    for candidate in _candidate_models(explicit):
        if candidate in ATLAS_IMAGE_MODELS:
            if edit and not ATLAS_IMAGE_MODELS[candidate].get("edit"):
                continue
            return candidate, ATLAS_IMAGE_MODELS[candidate]["atlas_model"]
        if candidate in full_models:
            model_id = full_models[candidate]
            if edit and not ATLAS_IMAGE_MODELS[model_id].get("edit"):
                continue
            return model_id, candidate

    if edit:
        return DEFAULT_EDIT_MODEL, ATLAS_IMAGE_MODELS[DEFAULT_EDIT_MODEL]["atlas_model"]
    return DEFAULT_MODEL, ATLAS_IMAGE_MODELS[DEFAULT_MODEL]["atlas_model"]
