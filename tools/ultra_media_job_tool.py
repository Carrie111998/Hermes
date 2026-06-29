"""Ultra Studio media job tools."""

from __future__ import annotations

import json
from typing import Any

from agent import ultra_media_store as store
from tools.registry import registry, tool_error


ULTRA_MEDIA_JOB_CREATE_SCHEMA: dict[str, Any] = {
    "name": "ultra_media_job_create",
    "description": (
        "Create a durable Ultra Studio MediaJob, execute image/video generation "
        "through the current provider, and optionally finalize the output as an asset."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "media_type": {"type": "string", "enum": ["image", "video"]},
            "prompt": {"type": "string"},
            "mode": {"type": "string", "enum": ["generate", "edit", "extend"], "default": "generate"},
            "image_url": {"type": "string"},
            "reference_image_urls": {"type": "array", "items": {"type": "string"}},
            "input_assets": {"type": "array", "items": {}},
            "duration": {"type": "integer"},
            "aspect_ratio": {"type": "string"},
            "resolution": {"type": "string"},
            "negative_prompt": {"type": "string"},
            "audio": {"type": "boolean"},
            "seed": {"type": "integer"},
            "model": {"type": "string"},
            "auto_finalize": {"type": "boolean", "default": True},
        },
        "required": ["media_type", "prompt"],
    },
}

ULTRA_MEDIA_JOB_STATUS_SCHEMA: dict[str, Any] = {
    "name": "ultra_media_job_status",
    "description": "Return durable state, output assets, error, and event log for an Ultra Studio MediaJob.",
    "parameters": {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
}

ULTRA_MEDIA_JOB_FINALIZE_SCHEMA: dict[str, Any] = {
    "name": "ultra_media_job_finalize",
    "description": "Register a succeeded MediaJob output as an asset with lineage. Safe to call repeatedly.",
    "parameters": {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
}


def _clean_str(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _clean_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    out = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return out or None


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _provider_args(args: dict[str, Any], media_type: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"prompt": args["prompt"]}
    for key in (
        "image_url",
        "reference_image_urls",
        "aspect_ratio",
        "duration",
        "resolution",
        "negative_prompt",
        "audio",
        "seed",
        "model",
    ):
        if key not in args:
            continue
        value = args.get(key)
        if key in {"image_url", "aspect_ratio", "resolution", "negative_prompt", "model"}:
            value = _clean_str(value)
        elif key == "reference_image_urls":
            value = _clean_list(value)
        if value is not None:
            payload[key] = value

    if media_type == "image":
        for key in ("duration", "resolution", "negative_prompt", "audio"):
            payload.pop(key, None)
    return payload


def _dispatch_provider(media_type: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
    tool_name = "image_generate" if media_type == "image" else "video_generate"
    raw = registry.dispatch(tool_name, payload, **kw)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {
            "success": False,
            "error": f"{tool_name} returned invalid JSON",
            "error_type": "invalid_provider_result",
            "raw": raw,
        }
    if not isinstance(parsed, dict):
        return {
            "success": False,
            "error": f"{tool_name} returned a non-object result",
            "error_type": "invalid_provider_result",
            "raw": parsed,
        }
    return parsed


def _output_ref(media_type: str, provider_result: dict[str, Any]) -> str | None:
    key = "image" if media_type == "image" else "video"
    value = provider_result.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _provider_task_id(provider_result: dict[str, Any]) -> str | None:
    for key in ("prediction_id", "provider_task_id", "task_id", "job_id"):
        value = provider_result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _reload_job(job_id: str) -> dict[str, Any]:
    job = store.get_job(job_id)
    if job is None:
        raise RuntimeError(f"MediaJob {job_id} disappeared")
    return job


def _job_result(success: bool, job: dict[str, Any], asset: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "success": success,
            "job_id": job["job_id"],
            "status": job["status"],
            "asset_id": asset["asset_id"] if asset else None,
            "job": job,
            "asset": asset,
            "events": store.job_events(job["job_id"]),
        },
        ensure_ascii=False,
    )


def _handle_ultra_media_job_create(args: dict[str, Any], **kw: Any) -> str:
    media_type = _clean_str(args.get("media_type"))
    prompt = _clean_str(args.get("prompt"))
    if media_type not in {"image", "video"}:
        return tool_error("media_type must be 'image' or 'video'")
    if not prompt:
        return tool_error("prompt is required")

    provider_payload = _provider_args({**args, "prompt": prompt}, media_type)
    auto_finalize = _coerce_bool(args.get("auto_finalize"), default=True)
    job = store.create_job(
        media_type=media_type,
        prompt=prompt,
        mode=_clean_str(args.get("mode")) or "generate",
        session_id=kw.get("session_id"),
        run_id=kw.get("task_id"),
        tool_call_id=kw.get("tool_call_id"),
        negative_prompt=_clean_str(args.get("negative_prompt")),
        input_assets=args.get("input_assets") if isinstance(args.get("input_assets"), list) else [],
        request={
            "provider_tool": "image_generate" if media_type == "image" else "video_generate",
            "provider_args": provider_payload,
            "auto_finalize": auto_finalize,
        },
    )
    store.update_job_running(job["job_id"])

    provider_result = _dispatch_provider(
        media_type,
        provider_payload,
        task_id=kw.get("task_id"),
        session_id=kw.get("session_id"),
        user_task=kw.get("user_task"),
    )
    provider = provider_result.get("provider") if isinstance(provider_result.get("provider"), str) else None
    model = provider_result.get("model") if isinstance(provider_result.get("model"), str) else None

    if not provider_result.get("success"):
        failed = store.fail_job(
            job["job_id"],
            error={
                "error": provider_result.get("error") or "media provider failed",
                "error_type": provider_result.get("error_type") or "provider_error",
            },
            provider_result=provider_result,
            provider=provider,
            model=model,
        )
        return _job_result(False, failed)

    output_ref = _output_ref(media_type, provider_result)
    if not output_ref:
        failed = store.fail_job(
            job["job_id"],
            error={
                "error": f"{media_type} provider succeeded without an output reference",
                "error_type": "empty_output",
            },
            provider_result=provider_result,
            provider=provider,
            model=model,
        )
        return _job_result(False, failed)

    completed = store.complete_job(
        job["job_id"],
        provider_result=provider_result,
        output_ref=output_ref,
        provider=provider,
        model=model,
        provider_task_id=_provider_task_id(provider_result),
    )
    asset = store.finalize_job(completed["job_id"]) if auto_finalize else None
    return _job_result(True, _reload_job(completed["job_id"]), asset)


def _handle_ultra_media_job_status(args: dict[str, Any], **_kw: Any) -> str:
    job_id = _clean_str(args.get("job_id"))
    if not job_id:
        return tool_error("job_id is required")
    job = store.get_job(job_id)
    if job is None:
        return json.dumps(
            {"success": False, "error": f"MediaJob not found: {job_id}", "error_type": "job_not_found"},
            ensure_ascii=False,
        )
    return _job_result(True, job)


def _handle_ultra_media_job_finalize(args: dict[str, Any], **_kw: Any) -> str:
    job_id = _clean_str(args.get("job_id"))
    if not job_id:
        return tool_error("job_id is required")
    try:
        asset = store.finalize_job(job_id)
    except KeyError:
        return json.dumps(
            {"success": False, "error": f"MediaJob not found: {job_id}", "error_type": "job_not_found"},
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps(
            {"success": False, "error": str(exc), "error_type": "job_not_finalizable"},
            ensure_ascii=False,
        )
    return _job_result(True, _reload_job(job_id), asset)


registry.register(
    name="ultra_media_job_create",
    toolset="ultra_media",
    schema=ULTRA_MEDIA_JOB_CREATE_SCHEMA,
    handler=_handle_ultra_media_job_create,
    check_fn=lambda: True,
    requires_env=[],
    is_async=False,
    emoji="🎬",
)

registry.register(
    name="ultra_media_job_status",
    toolset="ultra_media",
    schema=ULTRA_MEDIA_JOB_STATUS_SCHEMA,
    handler=_handle_ultra_media_job_status,
    check_fn=lambda: True,
    requires_env=[],
    is_async=False,
    emoji="📋",
)

registry.register(
    name="ultra_media_job_finalize",
    toolset="ultra_media",
    schema=ULTRA_MEDIA_JOB_FINALIZE_SCHEMA,
    handler=_handle_ultra_media_job_finalize,
    check_fn=lambda: True,
    requires_env=[],
    is_async=False,
    emoji="📦",
)
