"""Model discovery tool — dynamic catalog, native ``models_explore`` shape.

One agent-facing tool (``action`` ∈ list/search/get/recommend) backed by
:class:`tools.media_catalog.MediaCatalogClient`. Mirrors the native
``higgsfield_generate_models_explore`` design: the agent discovers models and
their parameters at runtime from the backend catalog instead of relying on a
static, hand-maintained table (which is what silently fell back to
``wan-2.6-flash``).

Typical call order for the agent:
  1. models_explore(action="recommend", task=..., type="video")  # pick a model
  2. models_explore(action="get", model=...)                     # its params
  3. video_generate(model=..., ...)                              # generate
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from tools.media_catalog import MediaCatalogError, get_catalog_client
from tools.registry import registry

logger = logging.getLogger(__name__)


MODELS_EXPLORE_SCHEMA: Dict[str, Any] = {
    "name": "models_explore",
    "description": (
        "Discover generation models from the backend catalog at runtime. "
        "Actions: 'list' (all models, optionally filtered by type), 'search' "
        "(keyword match on id/description/tags), 'get' (full parameter schema "
        "for one model — call BEFORE video_generate/image_generate when unsure "
        "what a model accepts), 'recommend' (rank models for a described task). "
        "Use this instead of guessing model ids; model ids passed to "
        "video_generate/image_generate are validated against this same catalog."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "search", "get", "recommend"],
                "description": "What to do.",
            },
            "type": {
                "type": "string",
                "enum": ["chat", "image", "video"],
                "description": "Filter by media family (list/search/recommend).",
            },
            "query": {
                "type": "string",
                "description": "Free-text keywords for action='search'.",
            },
            "model": {
                "type": "string",
                "description": "Model id for action='get', e.g. 'bytedance/seedance-2.0/reference-to-video'.",
            },
            "task": {
                "type": "string",
                "description": "Described intent for action='recommend', e.g. 'reference-driven identity video'.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (search/recommend). Default 10.",
            },
        },
        "required": ["action"],
    },
}


def _model_dict(model: Any) -> Dict[str, Any]:
    return {
        "id": model.id,
        "type": model.type,
        "vendor": model.vendor,
        "name": model.name,
        "description": model.description,
        "tags": model.tags,
        "input_modalities": model.input_modalities,
        "output_modalities": model.output_modalities,
    }


def _ok(payload: Dict[str, Any]) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _fail(code: str, message: str) -> str:
    return json.dumps(
        {"success": False, "error": message, "error_type": code},
        ensure_ascii=False,
    )


def _run_list(client: Any, args: Dict[str, Any]) -> str:
    type_filter = (args.get("type") or "").strip() or None
    models = client.list_models(type=type_filter)
    return _ok({"models": [_model_dict(m) for m in models], "count": len(models)})


def _run_search(client: Any, args: Dict[str, Any]) -> str:
    query = (args.get("query") or "").strip().lower()
    if not query:
        return _fail("invalid_param", "action='search' requires a non-empty 'query'.")
    type_filter = (args.get("type") or "").strip() or None
    limit = int(args.get("limit") or 10)
    words = [w for w in query.split() if w]
    matches: List[Any] = []
    for model in client.list_models(type=type_filter):
        hay = " ".join([model.id, model.name, model.description, " ".join(model.tags)]).lower()
        if any(word in hay for word in words):
            matches.append(model)
        if len(matches) >= limit:
            break
    return _ok({"models": [_model_dict(m) for m in matches], "count": len(matches)})


def _run_get(client: Any, args: Dict[str, Any]) -> str:
    model_id = (args.get("model") or "").strip()
    if not model_id:
        return _fail("invalid_param", "action='get' requires 'model'.")
    schema = client.get_model_schema(model_id)
    params = {
        name: {
            "type": p.type,
            "required": p.required,
            "default": p.default,
            "min": p.min,
            "max": p.max,
            "values": p.values,
            "description": p.description,
        }
        for name, p in schema.params.items()
    }
    return _ok({
        "model": schema.id,
        "type": schema.type,
        "vendor": schema.vendor,
        "params": params,
    })


def _run_recommend(client: Any, args: Dict[str, Any]) -> str:
    task = (args.get("task") or "").strip()
    if not task:
        return _fail("invalid_param", "action='recommend' requires 'task'.")
    type_filter = (args.get("type") or "").strip() or None
    limit = int(args.get("limit") or 5)
    results = client.recommend(task, type=type_filter, limit=limit)
    return _ok({
        "recommendations": [
            {"model": _model_dict(model), "reason": reason} for model, reason in results
        ],
        "count": len(results),
    })


_ACTION_HANDLERS = {
    "list": _run_list,
    "search": _run_search,
    "get": _run_get,
    "recommend": _run_recommend,
}


def _handle_models_explore(args: Dict[str, Any], **_kw: Any) -> str:
    action = (args.get("action") or "").strip().lower()
    handler = _ACTION_HANDLERS.get(action)
    if handler is None:
        return _fail(
            "invalid_param",
            f"action must be one of list/search/get/recommend, got {action!r}.",
        )
    try:
        client = get_catalog_client()
        return handler(client, args)
    except MediaCatalogError as exc:
        return _fail(exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 — surface as structured error, never crash the tool
        logger.warning("models_explore failed: %s", exc, exc_info=True)
        return _fail("catalog_unavailable", f"model catalog query failed: {exc}")


def _check_models_explore_available() -> bool:
    try:
        from plugins.video_gen.atlas.client import resolve_credentials

        api_key, api_root = resolve_credentials()
        return bool(api_key and api_root)
    except Exception:
        return False


registry.register(
    name="models_explore",
    toolset="video_gen",
    schema=MODELS_EXPLORE_SCHEMA,
    handler=_handle_models_explore,
    check_fn=_check_models_explore_available,
    requires_env=[],
    is_async=False,
    emoji="🧭",
)
