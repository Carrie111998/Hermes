"""Run Orchestrator Runtime Driver surface for the API server."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from gateway.runtime_skill_projection import (
    RuntimeSkillProjection,
    resolve_skill_projections,
    view_skill,
)
from gateway.runtime_tool_exposure import (
    RuntimeToolExposure,
    build_runtime_tool_exposure,
)
from tools.tool_search import (
    TOOL_SEARCH_NAME,
)

from gateway.api_server_shared import (
    AIOHTTP_AVAILABLE,
    MAX_RUNTIME_ATTACHMENT_BYTES,
    web,
)
from gateway.runtime_contract import runtime_error_envelope
from agent.tool_dispatch_helpers import DeferredToolResult
from gateway.runtime_session_history import (
    RuntimeSessionStateError as _RuntimeSessionStateError,
    SESSION_DB_HISTORY_MODE as _SESSION_DB_HISTORY_MODE,
    _inject_runtime_attachment_context,
    load_runtime_session_history as _load_runtime_session_history,
    merge_runtime_session_history as _merge_runtime_session_history,
    resume_runtime_history as _resume_runtime_history,
    resume_session_db_history as _resume_session_db_history,
    runtime_history_tool_names as _runtime_history_tool_names,
)

logger = logging.getLogger(__name__)

_SESSIONS: dict[str, "RuntimeBridgeSession"] = {}
_SESSIONS_LOCK = threading.RLock()
_REGISTERED_MANAGERS: set[int] = set()
_LOCAL_ACTIVITY_TOOLS = {
    "skill_view",
    "tool_search",
    "image_analyze",
    "video_analyze",
    "web_search",
    "web_extract",
}
_MAX_ARGUMENT_CORRECTIONS = 1
_FAILED_ALLOWED_STRING_FIELDS = {"media_type", "model", "provider"}
_FAILED_ALLOWED_STRING_LIST_FIELDS = {"aspect_ratios", "resolutions"}
_FAILED_ALLOWED_INTEGER_LIST_FIELDS = {"durations"}
_FAILED_ALLOWED_INTEGER_FIELDS = {"max_prompt_chars", "max_reference_images"}
_FAILED_ALLOWED_FIELDS = (
    _FAILED_ALLOWED_STRING_FIELDS
    | _FAILED_ALLOWED_STRING_LIST_FIELDS
    | _FAILED_ALLOWED_INTEGER_LIST_FIELDS
    | _FAILED_ALLOWED_INTEGER_FIELDS
)
_FAILED_ERROR_FIELDS = {"code", "message", "retryable"}
_TERMINAL_PLATFORM_ERROR_CODES = {
    "auth_rejected",
    "configuration_error",
    "cost_budget_exceeded",
    "idempotency_conflict",
    "insufficient_credits",
    "internal_error",
    "invalid_tool_result",
    "model_incompatible",
    "model_not_allowed",
    "provider_unavailable",
    "scope_denied",
    "tool_call_limit_exceeded",
    "tool_not_allowed",
    "tool_not_implemented",
    "unsupported_capability",
}

# Each bridge run parks one thread for its whole duration (invoke_platform_tool
# blocks on pending.ready.wait), so /v1/runtime/runs must never share the small
# default executor. Runs use a dedicated bounded pool gated before streaming.
_RUNTIME_MAX_CONCURRENT_ENV = "HERMES_RUNTIME_MAX_CONCURRENT"
_RUNTIME_MAX_CONCURRENT_DEFAULT = 8
_RUNTIME_STREAM_HEARTBEAT_SECONDS = 15.0
# Safety cap for pending.ready.wait when the run carries no explicit deadline;
# prevents a lost tool result from pinning an executor thread forever.
_UNBOUNDED_TOOL_WAIT_CAP_SECONDS = 3600.0
_SESSION_SWEEP_INTERVAL_SECONDS = 60.0
_FINISHED_SESSION_TTL_SECONDS = 120.0

_FAILURE_REASON_CODES = {
    "content_policy_blocked": "content_policy_blocked",
    "timeout": "provider_timeout",
    "overloaded": "provider_unavailable",
    "rate_limit": "provider_unavailable",
    "server_error": "provider_unavailable",
}


def _runtime_failure_code(result: Any) -> str:
    if not isinstance(result, dict):
        return "runtime_unavailable"
    if result.get("turn_exit_reason") in {
        "empty_response_exhausted",
        "all_retries_exhausted_no_response",
    }:
        return "provider_empty_stream"
    reason = str(result.get("failure_reason") or "").strip().lower()
    if reason in _FAILURE_REASON_CODES:
        return _FAILURE_REASON_CODES[reason]
    error = str(result.get("error") or "").strip().lower()
    if error.startswith("content_policy_blocked:"):
        return "content_policy_blocked"
    return "runtime_unavailable"

_RUNTIME_GATE_LOCK = threading.Lock()
_RUNTIME_EXECUTOR: ThreadPoolExecutor | None = None
_ACTIVE_RUN_COUNT = 0
_SWEEPERS: dict[int, tuple[asyncio.AbstractEventLoop, "asyncio.Task[None]"]] = {}
_NO_SKILL_MANIFEST = object()


async def _next_runtime_stream_event(
    queue: "asyncio.Queue[dict[str, Any] | None]",
) -> dict[str, Any] | None:
    """Return the next bridge event or a transport-only keepalive frame."""
    try:
        return await asyncio.wait_for(
            queue.get(),
            timeout=_RUNTIME_STREAM_HEARTBEAT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {"type": "heartbeat", "payload": {}}


def _runtime_max_concurrent() -> int:
    try:
        value = int(os.environ.get(_RUNTIME_MAX_CONCURRENT_ENV, ""))
    except ValueError:
        value = _RUNTIME_MAX_CONCURRENT_DEFAULT
    return max(1, value)


def _runtime_executor() -> ThreadPoolExecutor:
    global _RUNTIME_EXECUTOR
    with _RUNTIME_GATE_LOCK:
        if _RUNTIME_EXECUTOR is None:
            _RUNTIME_EXECUTOR = ThreadPoolExecutor(
                max_workers=_runtime_max_concurrent(),
                thread_name_prefix="runtime-bridge",
            )
        return _RUNTIME_EXECUTOR


def _acquire_run_slot() -> bool:
    global _ACTIVE_RUN_COUNT
    with _RUNTIME_GATE_LOCK:
        if _ACTIVE_RUN_COUNT >= _runtime_max_concurrent():
            return False
        _ACTIVE_RUN_COUNT += 1
        return True


def _release_run_slot() -> None:
    global _ACTIVE_RUN_COUNT
    with _RUNTIME_GATE_LOCK:
        _ACTIVE_RUN_COUNT = max(0, _ACTIVE_RUN_COUNT - 1)


def _sweep_finished_sessions(now: float | None = None) -> list[str]:
    """Evict sessions that finished but were never popped (leak backstop only).

    The normal cleanup path in _handle_runtime_run pops sessions before
    finished is set; anything still registered past the TTL leaked.
    """
    current = time.monotonic() if now is None else now
    removed: list[str] = []
    with _SESSIONS_LOCK:
        for key, session in list(_SESSIONS.items()):
            finished_at = session.finished_at
            if (
                session.finished.is_set()
                and finished_at is not None
                and current - finished_at >= _FINISHED_SESSION_TTL_SECONDS
            ):
                _SESSIONS.pop(key, None)
                removed.append(key)
    for key in removed:
        logger.warning("Runtime bridge sweeper evicted orphaned session %s", key)
    return removed


async def _session_sweeper_loop() -> None:
    while True:
        await asyncio.sleep(_SESSION_SWEEP_INTERVAL_SECONDS)
        _sweep_finished_sessions()


def _ensure_session_sweeper() -> None:
    loop = asyncio.get_running_loop()
    for key, (known_loop, task) in list(_SWEEPERS.items()):
        if known_loop.is_closed() or (known_loop is loop and task.done()):
            _SWEEPERS.pop(key, None)
    entry = _SWEEPERS.get(id(loop))
    if entry is not None and entry[0] is loop and not entry[1].done():
        return
    _SWEEPERS[id(loop)] = (loop, loop.create_task(_session_sweeper_loop()))


def _pin_run_model(agent: Any, requested_model: Any) -> str:
    """Lock one Runtime run to its requested model with no fallback chain."""
    pinned_model = str(requested_model or getattr(agent, "model", "")).strip()
    if not pinned_model:
        raise ValueError("model is required")

    current_model = str(getattr(agent, "model", "") or "").strip()
    switch_model = getattr(agent, "switch_model", None)
    if pinned_model != current_model and callable(switch_model):
        switch_model(
            pinned_model,
            str(getattr(agent, "provider", "") or ""),
            getattr(agent, "api_key", ""),
            str(getattr(agent, "base_url", "") or ""),
            str(getattr(agent, "api_mode", "") or ""),
        )
    else:
        agent.model = pinned_model

    primary_runtime = getattr(agent, "_primary_runtime", None)
    if isinstance(primary_runtime, dict):
        primary_runtime["model"] = pinned_model
        if "compressor_model" in primary_runtime:
            primary_runtime["compressor_model"] = pinned_model

    agent._run_model_pin = pinned_model
    agent._fallback_chain = []
    agent._fallback_model = None
    agent._fallback_index = 0
    agent._fallback_activated = False
    return pinned_model


def _activity_arguments(tool_name: str, args: Any) -> dict[str, str]:
    if not isinstance(args, dict):
        return {}
    allowed = {
        "skill_view": ("name", "file_path"),
        "tool_search": ("query",),
    }
    return {
        key: str(args[key])
        for key in allowed.get(tool_name, ())
        if isinstance(args.get(key), str) and str(args[key]).strip()
    }


def _skill_body_digest(args: Any, result: Any) -> str:
    """Digest of a successful SKILL.md body load; "" for sub-file reads or failures."""
    if not isinstance(args, dict) or str(args.get("file_path") or "").strip():
        return ""
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return ""
    if not isinstance(parsed, dict):
        return ""
    if parsed.get("success") is False or ("error" in parsed and parsed.get("success") is not True):
        return ""
    content = parsed.get("content")
    if not isinstance(content, str):
        return ""
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _activity_failure_message(result: Any) -> str:
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return ""
    if not isinstance(parsed, dict) or parsed.get("success") is not False:
        return ""
    message = parsed.get("error")
    if not isinstance(message, str) or not message.strip():
        return "runtime activity failed"
    return message.strip()[:240]


def _invalid_failed_tool_result() -> dict[str, Any]:
    return {
        "error": {
            "code": "invalid_tool_result",
            "message": "tool failed with an invalid result envelope",
            "retryable": False,
        },
    }


def _failed_allowed_value_is_safe(key: str, value: Any) -> bool:
    if key in _FAILED_ALLOWED_STRING_FIELDS:
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 512
    if key in _FAILED_ALLOWED_INTEGER_FIELDS:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if not isinstance(value, list) or len(value) > 64:
        return False
    if key in _FAILED_ALLOWED_STRING_LIST_FIELDS:
        return all(
            isinstance(item, str) and bool(item.strip()) and len(item) <= 128
            for item in value
        )
    return all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value
    )


def _failed_tool_result_projection(transport: Any) -> dict[str, Any]:
    """Project a failed platform result without exposing arbitrary fields."""
    if (
        not isinstance(transport, dict)
        or transport.get("ok") is not False
        or set(transport) - {"call_id", "ok", "result", "error"}
    ):
        return _invalid_failed_tool_result()
    error = transport.get("error")
    if not isinstance(error, dict) or set(error) != _FAILED_ERROR_FIELDS:
        return _invalid_failed_tool_result()
    code = error.get("code")
    message = error.get("message")
    retryable = error.get("retryable")
    if (
        not isinstance(code, str)
        or not code.strip()
        or len(code) > 128
        or not isinstance(message, str)
        or not message.strip()
        or len(message) > 2_000
        or not isinstance(retryable, bool)
    ):
        return _invalid_failed_tool_result()
    projection: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }
    if "result" not in transport:
        return projection
    result = transport.get("result")
    if not isinstance(result, dict) or set(result) != {"allowed"}:
        return _invalid_failed_tool_result()
    allowed = result.get("allowed")
    if (
        not isinstance(allowed, dict)
        or set(allowed) - _FAILED_ALLOWED_FIELDS
        or any(not _failed_allowed_value_is_safe(key, value) for key, value in allowed.items())
    ):
        return _invalid_failed_tool_result()
    projection["result"] = {"allowed": dict(allowed)}
    return projection


def _skill_scope_error(name: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": f"Skill '{name}' is not available for this run.",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _runtime_allowed_skill_names(
    skill_manifest: Any = _NO_SKILL_MANIFEST,
) -> set[str]:
    """Resolve the immutable Run Skill scope from the user snapshot only."""
    return set(_runtime_allowed_skill_digests(skill_manifest))


def _runtime_allowed_skill_digests(
    skill_manifest: Any = _NO_SKILL_MANIFEST,
) -> dict[str, str]:
    """Resolve Runtime aliases to the immutable package digests they prove."""
    if skill_manifest is _NO_SKILL_MANIFEST:
        return {}
    if not isinstance(skill_manifest, dict):
        raise ValueError("skill_manifest must be an object")
    skills = skill_manifest.get("skills")
    if not isinstance(skills, list):
        raise ValueError("skill_manifest.skills must be an array")
    digests: dict[str, str] = {}
    for skill in skills:
        if not isinstance(skill, dict):
            raise ValueError("skill_manifest.skills must contain only objects")
        name = skill.get("runtime_alias")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > 255
        ):
            raise ValueError("skill_manifest runtime_alias is invalid")
        name = name.strip()
        if name in digests:
            raise ValueError("skill_manifest contains duplicate runtime_alias")
        digest = skill.get("content_digest")
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            raise ValueError(f"skill_manifest content_digest is invalid for {name}")
        digests[name] = digest
    return digests


def _runtime_skill_projections(skill_manifest: Any) -> dict[str, RuntimeSkillProjection]:
    return resolve_skill_projections(skill_manifest, _NO_SKILL_MANIFEST)


def _discover_skill_metadata() -> list[dict[str, Any]]:
    from gateway.ultrastudio_skill_routing import discover_skill_metadata

    return discover_skill_metadata()


def _allowed_skills_prompt(allowed_names: set[str]) -> str:
    from gateway.ultrastudio_skill_routing import format_allowed_skills

    return format_allowed_skills(allowed_names, _discover_skill_metadata())


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


_RUNTIME_IMAGE_MIME_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
_RUNTIME_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
}
_MAX_RUNTIME_IMAGE_BYTES = 20 << 20
_MAX_RUNTIME_VIDEO_BYTES = 50 << 20
_RUNTIME_VIDEO_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}
_RUNTIME_IMAGE_SUFFIXES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _runtime_attachment_parts(
    attachments: Any,
    *,
    image_dir: str | os.PathLike[str] | None = None,
    video_dir: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    if attachments in (None, []):
        return []
    if not isinstance(attachments, list) or len(attachments) > 8:
        raise ValueError("attachments must be an array of at most 8 items")
    parts: list[dict[str, Any]] = []
    total_bytes = 0
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("attachments must contain only objects")
        asset_id = str(item.get("asset_id") or "").strip()
        reference_id = str(item.get("reference_id") or asset_id).strip()
        role = str(item.get("role") or "").strip()
        filename = str(item.get("filename") or "").strip()
        media_type = str(item.get("media_type") or "").strip()
        mime_type = str(item.get("mime_type") or "").strip().lower()
        encoded = item.get("data")
        if (
            not reference_id
            or (asset_id and item.get("reference_id") and reference_id != asset_id)
            or not role
            or not filename
            or not isinstance(encoded, str)
        ):
            raise ValueError("attachment identity, role, filename, and data are required")

        identity_label = "asset_id" if asset_id else "reference_id"
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("attachment data must be valid base64") from exc
        total_bytes += len(data)
        if total_bytes > MAX_RUNTIME_ATTACHMENT_BYTES:
            raise ValueError("runtime attachments exceed the 64 MiB total limit")
        if media_type == "image":
            if mime_type not in _RUNTIME_IMAGE_MIME_TYPES or not data or len(data) > _MAX_RUNTIME_IMAGE_BYTES:
                raise ValueError("runtime image attachment is invalid or too large")
            if image_dir is None:
                raise ValueError("runtime image materialization directory is required")
            directory = Path(image_dir).resolve()
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            image_path = directory / (
                hashlib.sha256(reference_id.encode("utf-8")).hexdigest()[:24]
                + _RUNTIME_IMAGE_SUFFIXES[mime_type]
            )
            image_path.write_bytes(data)
            image_path.chmod(0o600)
            parts.append({
                "type": "text",
                "text": (
                    f"[Attached image: {filename}; role={role}; "
                    f"{identity_label}={reference_id}. "
                    "When pixel analysis is required, call image_analyze with "
                    f"image_url={image_path}. Keep this private runtime path out "
                    "of the final answer.]"
                ),
                "_runtime_image_path": str(image_path),
            })
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
            })
            continue
        if media_type == "video":
            if mime_type not in _RUNTIME_VIDEO_MIME_TYPES or not data or len(data) > _MAX_RUNTIME_VIDEO_BYTES:
                raise ValueError("runtime video attachment is invalid or too large")
            if video_dir is None:
                raise ValueError("runtime video materialization directory is required")
            directory = Path(video_dir).resolve()
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            video_path = directory / (
                hashlib.sha256(reference_id.encode("utf-8")).hexdigest()[:24]
                + _RUNTIME_VIDEO_SUFFIXES[mime_type]
            )
            video_path.write_bytes(data)
            video_path.chmod(0o600)
            parts.append({
                "type": "text",
                "text": (
                    f"[Attached video: {filename}; role={role}; "
                    f"{identity_label}={reference_id}. "
                    "Analyze the complete source video with video_analyze using "
                    f"video_url={video_path} and include_transcript=true. "
                    "Representative frames, when present, "
                    "are supplementary rather than the source of truth.]"
                ),
                "_runtime_video_path": str(video_path),
            })
            continue
        raise ValueError("runtime attachment media_type must be image or video")
    return parts


def _runtime_image_paths(parts: list[dict[str, Any]]) -> list[Path]:
    return [
        Path(str(part["_runtime_image_path"])).resolve()
        for part in parts
        if isinstance(part, dict) and part.get("_runtime_image_path")
    ]


def _runtime_video_paths(parts: list[dict[str, Any]]) -> list[Path]:
    return [
        Path(str(part["_runtime_video_path"])).resolve()
        for part in parts
        if isinstance(part, dict) and part.get("_runtime_video_path")
    ]


def _public_runtime_attachment_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in part.items() if not key.startswith("_runtime_")}
        for part in parts
    ]


def _native_image_tool_definition() -> dict[str, Any]:
    # Runtime visual research is Hermes-owned: the Orchestrator supplies
    # run-scoped media authority, while Hermes owns model/provider execution.
    from tools import image_analyze as _image_analyze  # noqa: F401
    from tools.registry import registry

    entry = registry.get_entry("image_analyze")
    if entry is None:
        raise RuntimeError("Hermes image_analyze tool is not registered")
    return {
        "type": "function",
        "function": {**entry.schema, "name": entry.name},
    }


def _native_video_tool_definition() -> dict[str, Any]:
    # Importing the module performs its normal registry registration. Runtime
    # video input is an explicit per-run grant, so it must not depend on the
    # process-wide default toolset containing the opt-in video tool.
    from tools import vision_tools as _vision_tools  # noqa: F401
    from tools.registry import registry

    entry = registry.get_entry("video_analyze")
    if entry is None:
        raise RuntimeError("Hermes video_analyze tool is not registered")
    return {
        "type": "function",
        "function": {**entry.schema, "name": entry.name},
    }


def _tool_schemas(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, definition in enumerate(definitions):
        if not isinstance(definition, dict):
            raise ValueError(f"tools[{index}] must be an object")
        name = str(definition.get("name") or "").strip()
        parameters = definition.get("input_schema")
        if not name or name in seen or not isinstance(parameters, dict):
            raise ValueError(f"tools[{index}] has an invalid or duplicate definition")
        seen.add(name)
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(definition.get("description") or ""),
                "parameters": parameters,
            },
        })
    return schemas


def _replacement_system_prompt(system_context: Any) -> str:
    if not isinstance(system_context, dict):
        raise ValueError("trusted system_context is required")
    version = str(system_context.get("version") or "").strip()
    mode = str(system_context.get("mode") or "").strip()
    digest = str(system_context.get("digest") or "").strip()
    stable = str(system_context.get("stable") or "").strip()
    turn = str(system_context.get("turn") or "").strip()
    if not version or mode != "replace" or not digest or not stable:
        raise ValueError("trusted replacement system_context is required")
    value = f"{version}\n{mode}\n{stable}\n{turn}"
    expected = "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    if digest != expected:
        raise ValueError("system_context digest mismatch")
    return stable + ("\n\n" + turn if turn else "")


def _run_state_prompt(run_state: Any) -> str:
    """Render the platform-derived run state as an authenticated instructions block.

    run_state is platform data derived from the orchestrator event log, not
    user content; it is appended verbatim (compact JSON) without model-side
    interpretation. Resume and first start are treated identically.
    """
    if run_state is None:
        return ""
    if not isinstance(run_state, dict):
        raise ValueError("run_state must be an object")
    if not run_state:
        return ""
    return (
        "\n\n[RUN STATE — platform-authenticated, read-only]\n"
        + json.dumps(run_state, ensure_ascii=False, separators=(",", ":"))
    )


def _runtime_checkpoint_activated_tool_names(checkpoint: Any) -> set[str]:
    if not isinstance(checkpoint, dict):
        raise ValueError("runtime_checkpoint must be an object")
    raw_names = checkpoint.get("activated_tool_names", [])
    if not isinstance(raw_names, list) or any(
        not isinstance(name, str) or not name.strip()
        for name in raw_names
    ):
        raise ValueError("runtime_checkpoint.activated_tool_names must contain Tool names")
    return {name.strip() for name in raw_names}


def _runtime_tool_middleware(**kwargs: Any) -> Any:
    session_id = str(kwargs.get("session_id") or "")
    tool_name = str(kwargs.get("tool_name") or "")
    raw_args = kwargs.get("args")
    args: dict[str, Any] = dict(raw_args) if isinstance(raw_args, dict) else {}
    next_call = kwargs.get("next_call")
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        return next_call(args) if callable(next_call) else args
    if tool_name == TOOL_SEARCH_NAME:
        return session.search_and_activate_tools(args)
    if session.tool_exposure.is_callable(tool_name):
        return session.invoke_platform_tool(
            tool_name,
            args,
            str(kwargs.get("tool_call_id") or ""),
        )
    if tool_name in session.tool_names:
        return json.dumps({
            "error": {
                "code": "tool_not_loaded",
                "message": (
                    f"Tool '{tool_name}' is available to this Run but must be "
                    "loaded with tool_search before it can be called."
                ),
                "retryable": False,
            },
        }, ensure_ascii=False, separators=(",", ":"))
    if tool_name == "skill_view":
        requested = str(args.get("name") or args.get("skill") or "").strip()
        if not session.is_skill_allowed(requested):
            return _skill_scope_error(requested)
        projection = session.allowed_skill_projections.get(requested)
        if projection is not None:
            return view_skill(requested, projection, args)
    if tool_name == "image_analyze":
        for source in _image_analysis_sources(args):
            parsed = urlparse(source)
            if parsed.scheme.lower() in {"http", "https"}:
                continue
            if parsed.scheme.lower() == "file":
                if parsed.netloc not in {"", "localhost"}:
                    return _image_analysis_scope_error()
                candidate = unquote(parsed.path)
            elif parsed.scheme:
                return _image_analysis_scope_error()
            else:
                candidate = os.path.expanduser(source)
            try:
                resolved_path = str(Path(candidate).resolve(strict=True))
            except (OSError, RuntimeError):
                return _image_analysis_scope_error()
            if resolved_path not in session.allowed_image_paths:
                return _image_analysis_scope_error()
    if tool_name == "video_analyze":
        requested_path = str(args.get("video_url") or "").strip()
        try:
            resolved_path = str(Path(requested_path).resolve(strict=True))
        except (OSError, RuntimeError):
            resolved_path = ""
        if resolved_path not in session.allowed_video_paths:
            return json.dumps({
                "success": False,
                "error": "video_analyze may only read video attachments owned by this run.",
            })
    return next_call(args) if callable(next_call) else args


def _image_analysis_sources(args: dict[str, Any]) -> list[str]:
    sources: list[str] = []
    for field_name in ("image_url", "image_paths"):
        value = args.get(field_name)
        if isinstance(value, str):
            sources.append(value.strip())
        elif isinstance(value, list):
            sources.extend(
                item.strip()
                for item in value
                if isinstance(item, str)
            )
    return sources


def _image_analysis_scope_error() -> str:
    return json.dumps({
        "success": False,
        "error": (
            "image_analyze may only read HTTP(S) images or local image "
            "attachments owned by this run."
        ),
    })


def _ensure_runtime_middleware() -> None:
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager_id = id(manager)
    with _SESSIONS_LOCK:
        if manager_id in _REGISTERED_MANAGERS:
            return
        callbacks = manager._middleware.setdefault("tool_execution", [])
        if _runtime_tool_middleware not in callbacks:
            callbacks.insert(0, _runtime_tool_middleware)
        _REGISTERED_MANAGERS.add(manager_id)


@dataclass
class _PendingTool:
    name: str = ""
    signature_key: str = ""
    ready: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


class RuntimeBridgeSession:
    def __init__(
        self,
        run_id: str,
        loop: asyncio.AbstractEventLoop,
        queue: "asyncio.Queue[dict[str, Any] | None]",
        definitions: list[dict[str, Any]],
        deadline_ms: int,
        agent_session_id: str,
        tool_exposure: RuntimeToolExposure | None = None,
        allowed_skill_names: set[str] | None = None,
        allowed_skill_projections: dict[str, RuntimeSkillProjection] | None = None,
        allowed_image_paths: set[str] | None = None,
        allowed_video_paths: set[str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.agent_session_id = agent_session_id
        self.loop = loop
        self.queue = queue
        self.definitions = {str(item["name"]): dict(item) for item in definitions}
        self.tool_names = set(self.definitions)
        self.tool_exposure = tool_exposure or build_runtime_tool_exposure(
            definitions,
            _tool_schemas(definitions),
        )
        self.native_tool_schemas: list[dict[str, Any]] = []
        self.allowed_skill_names = (
            set(allowed_skill_names)
            if allowed_skill_names is not None
            else set()
        )
        self.allowed_skill_projections = {
            name: projection
            for name, projection in (allowed_skill_projections or {}).items()
            if name in self.allowed_skill_names
        }
        self.allowed_image_paths = {
            str(Path(path).resolve())
            for path in (allowed_image_paths or set())
        }
        self.allowed_video_paths = {
            str(Path(path).resolve())
            for path in (allowed_video_paths or set())
        }
        self.deadline_seconds = max(0.001, deadline_ms / 1000) if deadline_ms > 0 else None
        self.local_activities: dict[str, str] = {}
        self.pending: dict[str, _PendingTool] = {}
        self.non_retryable_failures: dict[str, str] = {}
        self.argument_correction_failures: dict[str, int] = {}
        self.agent_ref: list[Any] = [None]
        self.lock = threading.RLock()
        self.interrupted = threading.Event()
        self.interrupt_reason = ""
        self.finished = threading.Event()
        self.finished_async = asyncio.Event()
        self.finished_at: float | None = None

    def bind_agent(self, agent: Any, native_tool_schemas: list[dict[str, Any]]) -> None:
        self.agent_ref[0] = agent
        self.native_tool_schemas = [dict(schema) for schema in native_tool_schemas]
        self._refresh_agent_tools()

    def _refresh_agent_tools(self) -> None:
        agent = self.agent_ref[0]
        if agent is None:
            return
        with self.lock:
            agent.tools = [
                *self.native_tool_schemas,
                *self.tool_exposure.model_schemas,
            ]
            agent.valid_tool_names = {
                str((tool.get("function") or {}).get("name") or "")
                for tool in agent.tools
            }

    def search_and_activate_tools(self, args: dict[str, Any]) -> str:
        result = self.tool_exposure.search_and_activate(args)
        parsed = json.loads(result)
        if "error" not in parsed:
            self._refresh_agent_tools()
        return result

    def is_skill_allowed(self, name: str) -> bool:
        return bool(name) and name in self.allowed_skill_names

    @staticmethod
    def _tool_signature_key(name: str, args: dict[str, Any]) -> str:
        canonical = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return name + ":" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _halt_tool_loop(self, name: str, args: dict[str, Any], code: str, message: str, count: int) -> None:
        agent = self.agent_ref[0]
        setter = getattr(agent, "_set_tool_guardrail_halt", None)
        if not callable(setter):
            return
        from agent.tool_guardrails import ToolCallSignature, ToolGuardrailDecision

        setter(ToolGuardrailDecision(
            action="halt",
            code=code,
            message=message,
            tool_name=name,
            count=count,
            signature=ToolCallSignature.from_call(name, args),
        ))

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {"run_id": self.run_id, "type": event_type, "payload": payload}
        try:
            if asyncio.get_running_loop() is self.loop:
                self.queue.put_nowait(event)
                return
        except RuntimeError:
            pass
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    def loaded_skill_digest(self, name: str, args: Any, result: Any) -> str:
        """Return the digest of the root SKILL.md bytes actually loaded."""
        body_digest = _skill_body_digest(args, result)
        if not body_digest or not self.is_skill_allowed(name):
            return ""
        return body_digest

    def start_local_activity(self, call_id: str, name: str, args: Any) -> None:
        if not call_id or name not in _LOCAL_ACTIVITY_TOOLS:
            return
        with self.lock:
            if call_id in self.local_activities:
                return
            self.local_activities[call_id] = name
        self.emit("activity_started", {
            "call_id": call_id,
            "name": name,
            "arguments": _activity_arguments(name, args),
        })

    def complete_local_activity(self, call_id: str, name: str, args: Any, result: Any) -> None:
        if not call_id:
            return
        with self.lock:
            started_name = self.local_activities.pop(call_id, "")
        if not started_name or started_name != name:
            return
        message = _activity_failure_message(result)
        payload: dict[str, Any] = {
            "call_id": call_id,
            "name": name,
            "status": "failed" if message else "completed",
        }
        if message:
            payload["error"] = {
                "code": "runtime_activity_failed",
                "message": message,
                "retryable": False,
            }
        elif name == "skill_view":
            skill_name = (
                str(args.get("name") or args.get("skill") or "").strip()
                if isinstance(args, dict)
                else ""
            )
            digest = self.loaded_skill_digest(skill_name, args, result)
            if digest:
                payload["arguments"] = {"digest": digest}
        self.emit("activity_completed", payload)

    def _checkpoint_message(
        self,
        call_id: str,
        model_tool_name: str,
        canonical_name: str,
        canonical_args: dict[str, Any],
    ) -> dict[str, Any]:
        agent = self.agent_ref[0]
        candidate = getattr(agent, "_runtime_checkpoint_message", None)
        if not isinstance(candidate, dict):
            db = getattr(agent, "_session_db", None)
            loader = getattr(db, "get_messages_as_conversation", None)
            history = loader(self.agent_session_id) if callable(loader) else []
            candidate = next(
                (
                    message
                    for message in reversed(history)
                    if isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and isinstance(message.get("tool_calls"), list)
                    and any(
                        isinstance(call, dict) and str(call.get("id") or "") == call_id
                        for call in message["tool_calls"]
                    )
                ),
                None,
            )
        calls = candidate.get("tool_calls") if isinstance(candidate, dict) else None
        active_calls = [
            call
            for call in calls or []
            if isinstance(call, dict) and str(call.get("id") or "") == call_id
        ]
        if len(active_calls) != 1:
            raise ValueError("runtime checkpoint must contain the active platform tool call")
        # One model turn may contain a local skill_view call beside a delegated
        # platform call. Only the active
        # platform call belongs in the restart checkpoint: local activity is
        # already complete and is not a resumable side effect.
        candidate = {**candidate, "tool_calls": active_calls}
        # Checkpoints cross the Runtime boundary and are persisted by the
        # Orchestrator. Plaintext model reasoning is not part of the resumable
        # tool-call contract and may contain private Skill instructions that
        # were legitimately used to form safe tool arguments. Keep only the
        # empty reasoning_content sentinel expected by thinking-mode replay;
        # agent_runtime_helpers will retain or strip it for the active provider.
        candidate.pop("reasoning", None)
        if "reasoning_content" in candidate:
            candidate["reasoning_content"] = " "
        checkpoint = _resume_runtime_history(
            [],
            {"message": candidate},
            [{"tool_call_id": call_id, "status": "succeeded", "output": {}}],
        )[0]
        function = checkpoint["tool_calls"][0].get("function") or {}
        if str(function.get("name") or "") != model_tool_name:
            raise ValueError("runtime checkpoint tool name does not match active call")
        function["name"] = canonical_name
        function["arguments"] = json.dumps(
            canonical_args,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return checkpoint

    def invoke_platform_tool(
        self,
        name: str,
        args: dict[str, Any],
        call_id: str,
    ) -> str | DeferredToolResult:
        if not call_id:
            return json.dumps({"error": {"code": "invalid_tool_request", "message": "tool call id is required"}})
        signature_key = self._tool_signature_key(name, args)
        with self.lock:
            prior_code = self.non_retryable_failures.get(signature_key, "")
        if prior_code:
            message = (
                f"Blocked unchanged retry of {name}: the previous call failed with "
                f"non-retryable error {prior_code}."
            )
            self._halt_tool_loop(name, args, "repeated_non_retryable_tool_call", message, 2)
            return json.dumps({
                "error": {
                    "code": "repeated_non_retryable_tool_call",
                    "message": message,
                    "retryable": False,
                },
            }, ensure_ascii=False, separators=(",", ":"))
        try:
            checkpoint = self._checkpoint_message(
                call_id,
                name,
                name,
                args,
            )
        except ValueError as exc:
            message = str(exc)
            self.emit("error", {"code": "runtime_checkpoint_invalid", "message": message})
            self.interrupt(message)
            return json.dumps({
                "error": {
                    "code": "runtime_checkpoint_invalid",
                    "message": message,
                    "retryable": False,
                },
            }, ensure_ascii=False, separators=(",", ":"))
        pending = _PendingTool(name=name, signature_key=signature_key)
        with self.lock:
            if call_id in self.pending:
                return json.dumps({"error": {"code": "idempotency_conflict", "message": "duplicate active tool call id"}})
            self.pending[call_id] = pending
        payload: dict[str, Any] = {"call_id": call_id, "name": name, "arguments": args}
        self.emit("checkpoint", {
            "message": checkpoint,
            "activated_tool_names": self.tool_exposure.snapshot_activated_names(),
        })
        self.emit("tool_request", payload)
        wait_timeout = (
            self.deadline_seconds
            if self.deadline_seconds is not None
            else _UNBOUNDED_TOOL_WAIT_CAP_SECONDS
        )
        ready = pending.ready.wait(wait_timeout)
        if not ready or self.interrupted.is_set():
            with self.lock:
                self.pending.pop(call_id, None)
            # An interrupt wakes the same wait; attribute it before deadline.
            if self.interrupted.is_set():
                if self.interrupt_reason.startswith("parked:"):
                    return DeferredToolResult(call_id)
                code, message = "run_interrupted", "run was interrupted"
            else:
                code, message = "runtime_deadline_exceeded", "tool-result deadline exceeded"
            return json.dumps({"error": {"code": code, "message": message, "retryable": False}})
        result = pending.result or {}
        if result.get("ok"):
            with self.lock:
                self.argument_correction_failures.pop(name, None)
            return json.dumps(result.get("result"), ensure_ascii=False, separators=(",", ":"))
        failure = _failed_tool_result_projection(result)
        error = failure["error"]  # same dict; recovery added below stays in failure
        code = str(error.get("code") or "invalid_tool_result")
        if code == "invalid_tool_arguments":
            with self.lock:
                correction_count = self.argument_correction_failures.get(name, 0) + 1
                self.argument_correction_failures[name] = correction_count
            if correction_count <= _MAX_ARGUMENT_CORRECTIONS:
                error["recovery"] = {
                    "action": "correct_arguments",
                    "remaining_attempts": _MAX_ARGUMENT_CORRECTIONS - correction_count + 1,
                    "same_arguments_allowed": False,
                }
            else:
                message = (
                    f"{name} produced invalid arguments after "
                    f"{_MAX_ARGUMENT_CORRECTIONS} correction attempt."
                )
                self._halt_tool_loop(
                    name,
                    args,
                    "argument_correction_exhausted",
                    message,
                    correction_count,
                )
                failure["error"] = error = {
                    "code": "argument_correction_exhausted",
                    "message": message,
                    "retryable": False,
                    "cause": dict(error),
                }
                code = "argument_correction_exhausted"
        else:
            with self.lock:
                self.argument_correction_failures.pop(name, None)
        if error.get("retryable") is False and code != "domain_gate_required":
            with self.lock:
                self.non_retryable_failures[signature_key] = code
            if code in _TERMINAL_PLATFORM_ERROR_CODES:
                message = str(error.get("message") or f"{name} failed with {code}")
                self._halt_tool_loop(name, args, code, message, 1)
        return json.dumps(failure, ensure_ascii=False, separators=(",", ":"))

    def submit_result(self, result: dict[str, Any]) -> bool:
        call_id = str(result.get("call_id") or "")
        with self.lock:
            pending = self.pending.pop(call_id, None)
            if pending is None:
                return False
            pending.result = result
            pending.ready.set()
        return True

    def interrupt(self, reason: str) -> None:
        self.interrupt_reason = reason
        self.interrupted.set()
        agent = self.agent_ref[0]
        if agent is not None:
            agent.interrupt(reason)
        with self.lock:
            for pending in self.pending.values():
                pending.ready.set()

    def mark_finished(self) -> None:
        self.finished_at = time.monotonic()
        self.finished.set()
        try:
            if asyncio.get_running_loop() is self.loop:
                self.finished_async.set()
                return
        except RuntimeError:
            pass
        self.loop.call_soon_threadsafe(self.finished_async.set)


class APIServerRuntimeMixin:
    async def _run_agent_bridge(self, **kwargs: Any) -> tuple:
        """Run one bridge conversation on the dedicated bounded runtime pool.

        Mirrors APIServerRunsMixin._run_agent but swaps the default executor
        (min(32, cpu+4) threads shared with every other endpoint) for the
        bridge-owned pool sized by HERMES_RUNTIME_MAX_CONCURRENT.
        """
        loop = asyncio.get_running_loop()

        def _run() -> tuple:
            from gateway.api_agent_runner import run_agent_sync

            return run_agent_sync(self, **kwargs)

        return await loop.run_in_executor(_runtime_executor(), _run)

    async def _handle_runtime_run(self, request: "web.Request") -> "web.StreamResponse":
        auth_error = self._check_auth(request)
        if auth_error:
            return auth_error
        # Concurrency gate before response.prepare: over-limit requests are
        # rejected with a retryable 429 instead of queueing on the executor.
        if not _acquire_run_slot():
            return web.json_response(
                {
                    "error": {
                        "code": "runtime_concurrency_exceeded",
                        "message": f"too many concurrent runtime runs (max {_runtime_max_concurrent()})",
                        "retryable": True,
                    },
                },
                status=429,
                headers={"Retry-After": "1"},
            )
        try:
            return await self._handle_runtime_run_gated(request)
        finally:
            _release_run_slot()

    async def _handle_runtime_run_gated(self, request: "web.Request") -> "web.StreamResponse":
        media_temp_dir: tempfile.TemporaryDirectory[str] | None = None
        try:
            body = await request.json()
            run_id = str(body.get("run_id") or "").strip()
            messages = body.get("messages")
            system_context = body.get("system_context")
            definitions = body.get("tools") or []
            tool_results = body.get("tool_results") or []
            runtime_checkpoint = body.get("runtime_checkpoint")
            context = body.get("context") if isinstance(body.get("context"), dict) else {}
            history_mode = str(context.get("history_mode") or "").strip()
            if history_mode not in {"", _SESSION_DB_HISTORY_MODE}:
                raise ValueError("context.history_mode must be session_db when provided")
            session_db_mode = history_mode == _SESSION_DB_HISTORY_MODE
            if not run_id or not isinstance(messages, list) or not messages:
                raise ValueError("run_id and messages are required")
            # Expose the run id to the audit middleware: its completion line
            # is logged after this handler returns, so the audit trail can
            # correlate the access log with the run it served.
            request["hermes_run_id"] = run_id
            if session_db_mode:
                resuming = bool(tool_results)
                if runtime_checkpoint is not None and not tool_results:
                    raise ValueError("tool_results are required when a runtime_checkpoint is provided")
            else:
                resuming = bool(tool_results or runtime_checkpoint)
                if resuming and (not tool_results or runtime_checkpoint is None):
                    raise ValueError("runtime_checkpoint and tool_results are both required for resume")
            skill_manifest = (
                body["skill_manifest"]
                if "skill_manifest" in body
                else _NO_SKILL_MANIFEST
            )
            allowed_skill_digests = _runtime_allowed_skill_digests(
                skill_manifest,
            )
            allowed_skill_names = set(allowed_skill_digests)
            allowed_skill_projections = _runtime_skill_projections(skill_manifest)
            instructions = (
                _replacement_system_prompt(system_context)
                + _allowed_skills_prompt(allowed_skill_names)
                + _run_state_prompt(body.get("run_state"))
            )
            schemas = _tool_schemas(definitions)
            tool_exposure = build_runtime_tool_exposure(definitions, schemas)
            normalized_messages = [
                {"role": str(item.get("role") or ""), "content": _message_text(item.get("content"))}
                for item in messages
                if isinstance(item, dict)
            ]
            if len(normalized_messages) != len(messages):
                raise ValueError("messages must contain only objects")
            attachments = body.get("attachments")
            has_image_attachment = any(
                isinstance(item, dict) and item.get("media_type") == "image"
                for item in (attachments if isinstance(attachments, list) else [])
            )
            has_video_attachment = any(
                isinstance(item, dict) and item.get("media_type") == "video"
                for item in (attachments if isinstance(attachments, list) else [])
            )
            if has_image_attachment or has_video_attachment:
                media_temp_dir = tempfile.TemporaryDirectory(prefix="hermes-runtime-media-")
            attachment_parts = _runtime_attachment_parts(
                attachments,
                image_dir=media_temp_dir.name if media_temp_dir else None,
                video_dir=media_temp_dir.name if media_temp_dir else None,
            )
            runtime_image_paths = _runtime_image_paths(attachment_parts)
            runtime_video_paths = _runtime_video_paths(attachment_parts)
            if attachment_parts and not (session_db_mode and resuming):
                last_user_index = next(
                    (
                        index
                        for index in range(len(normalized_messages) - 1, -1, -1)
                        if normalized_messages[index].get("role") == "user"
                    ),
                    -1,
                )
                if last_user_index < 0:
                    raise ValueError("attachments require a user message")
                text = _message_text(normalized_messages[last_user_index].get("content"))
                normalized_messages[last_user_index]["content"] = [
                    {"type": "text", "text": text or "[Attached media]"},
                    *_public_runtime_attachment_parts(attachment_parts),
                ]
            requested_agent_session_id = str(context.get("session_id") or run_id).strip()
            agent_session_id = requested_agent_session_id
            if session_db_mode:
                db, agent_session_id, session_history = _load_runtime_session_history(
                    self,
                    requested_agent_session_id,
                    require_existing=resuming,
                )
                if resuming:
                    history = _merge_runtime_session_history(
                        normalized_messages,
                        session_history,
                    )
                    history = _resume_session_db_history(
                        db,
                        agent_session_id,
                        history,
                        tool_results,
                    )
                    history = _inject_runtime_attachment_context(
                        history,
                        attachment_parts,
                    )
                    activated_tool_names = _runtime_history_tool_names(history)
                    if runtime_checkpoint is not None:
                        activated_tool_names.update(
                            _runtime_checkpoint_activated_tool_names(
                                runtime_checkpoint,
                            ),
                        )
                        checkpoint_message = runtime_checkpoint.get("message")
                        checkpoint_calls = (
                            checkpoint_message.get("tool_calls")
                            if isinstance(checkpoint_message, dict)
                            else []
                        )
                        activated_tool_names.update({
                            str((call.get("function") or {}).get("name") or "")
                            for call in checkpoint_calls or []
                            if isinstance(call, dict)
                        })
                    tool_exposure.activate_names(activated_tool_names)
                    user_message = ""
                else:
                    last = normalized_messages[-1]
                    if last.get("role") != "user":
                        raise ValueError("last message must be user")
                    history = _merge_runtime_session_history(
                        normalized_messages[:-1],
                        session_history,
                    )
                    user_message = last.get("content")
            elif resuming:
                history = _resume_runtime_history(normalized_messages, runtime_checkpoint, tool_results)
                activated_tool_names = _runtime_checkpoint_activated_tool_names(
                    runtime_checkpoint,
                )
                checkpoint_message = runtime_checkpoint.get("message")
                checkpoint_calls = (
                    checkpoint_message.get("tool_calls")
                    if isinstance(checkpoint_message, dict)
                    else []
                )
                activated_tool_names.update({
                    str((call.get("function") or {}).get("name") or "")
                    for call in checkpoint_calls or []
                    if isinstance(call, dict)
                })
                tool_exposure.activate_names(activated_tool_names)
                user_message = ""
            else:
                history = normalized_messages[:-1]
                last = normalized_messages[-1]
                if last.get("role") != "user":
                    raise ValueError("last message must be user")
                user_message = last.get("content")
        except _RuntimeSessionStateError as exc:
            if media_temp_dir is not None:
                media_temp_dir.cleanup()
            return web.json_response(
                {"error": {"code": exc.code, "message": str(exc)}},
                status=exc.status,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            if media_temp_dir is not None:
                media_temp_dir.cleanup()
            return web.json_response({"error": {"code": "invalid_param", "message": str(exc)}}, status=422)
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "application/x-ndjson",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await response.prepare(request)
        queue: "asyncio.Queue[dict[str, Any] | None]" = asyncio.Queue()
        session = RuntimeBridgeSession(
            run_id,
            asyncio.get_running_loop(),
            queue,
            definitions,
            int(body.get("deadline_ms") or 0),
            agent_session_id,
            tool_exposure=tool_exposure,
            allowed_skill_names=allowed_skill_names,
            allowed_skill_projections=allowed_skill_projections,
            allowed_image_paths={str(path) for path in runtime_image_paths},
            allowed_video_paths={str(path) for path in runtime_video_paths},
        )
        _ensure_runtime_middleware()
        _ensure_session_sweeper()
        with _SESSIONS_LOCK:
            if run_id in _SESSIONS or agent_session_id in _SESSIONS:
                await response.write(json.dumps({"run_id": run_id, "type": "error", "payload": {"code": "run_state_conflict", "message": "run already active"}}).encode() + b"\n")
                if media_temp_dir is not None:
                    media_temp_dir.cleanup()
                return response
            _SESSIONS[run_id] = session
            _SESSIONS[agent_session_id] = session

        def configure_agent(agent: Any) -> None:
            native_runtime_tools = {
                "skill_view",
                "web_search",
                "web_extract",
                "image_analyze",
            }
            if runtime_video_paths:
                native_runtime_tools.add("video_analyze")
            native = [
                tool
                for tool in (agent.tools or [])
                if tool.get("function", {}).get("name") in native_runtime_tools
            ]
            if not any(
                tool.get("function", {}).get("name") == "image_analyze"
                for tool in native
            ):
                native.append(_native_image_tool_definition())
            if runtime_video_paths and not any(
                tool.get("function", {}).get("name") == "video_analyze"
                for tool in native
            ):
                native.append(_native_video_tool_definition())
            session.bind_agent(agent, native)
            _pin_run_model(agent, body.get("model"))
            agent.ephemeral_system_prompt = None
            agent._cached_system_prompt = instructions
            agent._build_system_prompt = lambda _system_message=None: instructions
            agent._resume_from_tool_results = resuming
            agent._require_incremental_session_persistence = session_db_mode
            # The Orchestrator already validated and materialized these image
            # assets. Its model catalog can lag newly deployed multimodal
            # aliases, so do not replace trusted pixels with an auxiliary
            # vision description merely because models.dev lacks the alias.
            agent._runtime_force_native_vision = has_image_attachment
            # Runtime bridge runs park on media generation for well over the
            # default 5m prompt-cache TTL, so a resume repays the full 13-14k
            # token system prefix at uncached price. Pin the 1h tier (the
            # other value agent_init accepts) for these runs only; the global
            # default and ~/.hermes/config.yaml stay untouched.
            agent._cache_ttl = "1h"

        def on_tool_start(tool_call_id: str, function_name: str, function_args: Any) -> None:
            session.start_local_activity(tool_call_id, function_name, function_args)

        def on_tool_complete(
            tool_call_id: str,
            function_name: str,
            function_args: Any,
            function_result: Any,
        ) -> None:
            session.complete_local_activity(tool_call_id, function_name, function_args, function_result)

        async def pump() -> None:
            while True:
                event = await _next_runtime_stream_event(queue)
                if event is None:
                    return
                try:
                    await response.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                except Exception as exc:
                    # The orchestrator went away; without an interrupt the
                    # agent keeps running and events pile into the queue.
                    logger.warning(
                        "Runtime bridge stream write failed for run %s; interrupting: %s",
                        run_id,
                        exc,
                    )
                    session.interrupt("orchestrator stream disconnected")
                    return

        pump_task = asyncio.create_task(pump())
        session.emit("run_started", {
            "runtime": "hermes",
            "system_context_version": system_context["version"],
            "system_context_mode": system_context["mode"],
            "system_context_digest": system_context["digest"],
        })
        try:
            result, usage = await self._run_agent_bridge(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=None,
                session_id=agent_session_id,
                stream_delta_callback=lambda delta: session.emit("text_delta", {"delta": delta}) if delta else None,
                tool_start_callback=on_tool_start,
                tool_complete_callback=on_tool_complete,
                agent_ref=session.agent_ref,
                agent_configurator=configure_agent,
            )
            text = str((result or {}).get("final_response") or "")
            session.emit("usage", usage or {})
            failed = isinstance(result, dict) and (
                bool(result.get("failed")) or result.get("completed") is False
            )
            if failed:
                session.emit(
                    "error",
                    runtime_error_envelope(
                        _runtime_failure_code(result),
                        support_id=run_id,
                    ),
                )
            else:
                session.emit("completed", {"finish_reason": "stop", "text": text})
        except asyncio.CancelledError:
            # aiohttp cancels the handler when the orchestrator disconnects;
            # the agent may keep running on its executor thread unless told
            # to stop.
            logger.warning("Runtime bridge request cancelled for run %s; interrupting", run_id)
            session.interrupt("orchestrator stream disconnected")
            raise
        except Exception as exc:
            logger.exception("Run Orchestrator runtime run failed: %s", run_id)
            session.emit(
                "error",
                runtime_error_envelope("runtime_unavailable", support_id=run_id),
            )
        finally:
            with _SESSIONS_LOCK:
                _SESSIONS.pop(run_id, None)
                _SESSIONS.pop(agent_session_id, None)
            session.mark_finished()
            queue.put_nowait(None)
            await pump_task
            if media_temp_dir is not None:
                media_temp_dir.cleanup()
        return response

    async def _handle_runtime_tool_result(self, request: "web.Request") -> "web.Response":
        auth_error = self._check_auth(request)
        if auth_error:
            return auth_error
        run_id = request.match_info["run_id"]
        request["hermes_run_id"] = run_id
        with _SESSIONS_LOCK:
            session = _SESSIONS.get(run_id)
        if session is None:
            return web.json_response({"error": {"code": "run_not_found", "message": "run is not active"}}, status=404)
        try:
            result = await request.json()
        except Exception:
            return web.json_response({"error": {"code": "invalid_param", "message": "invalid JSON"}}, status=400)
        if not isinstance(result, dict) or not session.submit_result(result):
            return web.json_response({"error": {"code": "invalid_tool_result", "message": "unknown call_id"}}, status=409)
        return web.Response(status=204)

    async def _handle_runtime_interrupt(self, request: "web.Request") -> "web.Response":
        auth_error = self._check_auth(request)
        if auth_error:
            return auth_error
        run_id = request.match_info["run_id"]
        request["hermes_run_id"] = run_id
        with _SESSIONS_LOCK:
            session = _SESSIONS.get(run_id)
        if session is None:
            return web.json_response({"error": {"code": "run_not_found", "message": "run is not active"}}, status=404)
        body = await request.json()
        session.interrupt(str(body.get("reason") or "interrupted by orchestrator"))
        # Wait on the asyncio-side completion event: interrupt must never
        # borrow an executor thread the runs themselves may have exhausted.
        try:
            await asyncio.wait_for(session.finished_async.wait(), 10)
        except asyncio.TimeoutError:
            return web.json_response(
                {"error": {"code": "interrupt_timeout", "message": "runtime session did not stop"}},
                status=503,
            )
        return web.Response(status=204)
