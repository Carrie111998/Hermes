"""Model-visible Vision Router tool wrapper (Stage 1: registered but hidden).

The wrapper is model-invisible until a session explicitly enables the local
Vision Router through the approved minimal-enablement gate.

Contract:
- the model may express SEMANTIC intent only: source_handle / task / mode /
  region / question;
- everything else (endpoint, model, transport, num_ctx, num_predict, timeout,
  retries, keep_alive, fallback, escalation, criticality) is policy-controlled
  inside the orchestrator profiles;
- visibility is server-controlled: ``vision_router.enabled``; when false the
  tool name is filtered out of the model tool set entirely
  (``model_tools.get_tool_definitions``) — hard invisibility, not runtime
  POLICY_BLOCKED only;
- image sources are authorized by handle: ``locator://<key>`` (approved
  private-locator handles) or an explicit allowlisted handle; anything else
  returns SOURCE_DENIED with ZERO model calls;
- result envelope is fail-closed and never contains thinking / raw provider
  output / base64 / private paths / credentials;
- OCR long results are bounded: first <=``ocr_excerpt_chars`` chars plus
  total count, sha256 and a private handle; full text needs an explicit
  follow-up request.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Awaitable, Dict, Optional

from tools.registry import registry

# ---------------------------------------------------------------------------
# Constants (policy-controlled boundaries)
# ---------------------------------------------------------------------------

DEFAULT_OCR_EXCERPT_CHARS = 4000
DEFAULT_OCR_PAGE_CHARS = 65536
DEFAULT_MAX_QUESTION_CHARS = 500
SOURCE_DENIED = "SOURCE_DENIED"

_TASK_TO_CRITICALITY = {
    "UI_READ": "HIGH",
    "EXACT_OCR": "HIGH",
    "SCENE_DESCRIBE": "NORMAL",
    "EVIDENCE_VERIFY": "NORMAL",
}

_ALLOWED_TASKS = ("UI_READ", "SCENE_DESCRIBE", "EVIDENCE_VERIFY", "EXACT_OCR")


def _config_value(config: Optional[Dict[str, Any]], key: str, default: Any) -> Any:
    if not config:
        return default
    router = config.get("vision_router") or {}
    return router.get(key, default)


def _authorized_source_handles() -> Dict[str, str]:
    """Approved locator handles -> resolved local path.

    Only handles listed here are model-authorized image sources. The mapping
    itself (paths) never leaves this module; the model only ever sees the
    opaque handle. Public builds carry no locator: the primary
    authorization path is the session-scoped attachment allowlist
    (``attachment://`` handles registered by the host).
    """
    return {}


def _resolve_source(source_handle: str) -> Optional[str]:
    """Authorize a model-supplied source handle.

    Accepts ``locator://<key>`` (approved locator handle) or a bare key that
    exists in the approved locator set. Returns the resolved local path, or
    None when the handle is not authorized (→ SOURCE_DENIED, zero calls).
    """
    handle = source_handle.strip()
    if handle.startswith("locator://"):
        handle = handle[len("locator://"):]
    handles = _authorized_source_handles()
    return handles.get(handle)


def _criticality_for(task: str) -> str:
    """Policy-derived criticality — the model can never force an expensive
    slot by declaring HIGH (design §10)."""
    return _TASK_TO_CRITICALITY.get(task, "NORMAL")


def _build_envelope(result: Dict[str, Any], config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Safe model-visible envelope. Never contains thinking / raw provider
    output / base64 / private paths / credentials."""
    excerpt_limit = int(_config_value(config, "ocr_excerpt_chars",
                                      DEFAULT_OCR_EXCERPT_CHARS))
    structured = result.get("structured") or {}
    observed = structured.get("observed_text")
    observed_text: Any = observed
    ocr_meta: Optional[Dict[str, Any]] = None
    if isinstance(observed, list):
        joined = "\n".join(str(t) for t in observed if str(t).strip())
        if len(joined) > excerpt_limit:
            excerpt = joined[:excerpt_limit]
            ocr_meta = {
                "total_chars": len(joined),
                "returned_chars": len(excerpt),
                "truncated": True,
                "sha256": hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16],
                "private_handle": result.get("request_id"),
                "full_text_policy": "explicit_followup_required",
            }
            observed_text = [excerpt]
    trace = (result.get("trace") or [{}])[0]
    envelope = {
        "request_id": result.get("request_id"),
        "task": result.get("task"),
        "selected_slot": result.get("final_model_slot")
        or result.get("initial_model_slot"),
        "execution_status": result.get("execution_status"),
        "quality_decision": result.get("quality_decision"),
        "observed_text": observed_text,
        "evidence": structured.get("evidence"),
        "inference": structured.get("inference"),
        "uncertainty": structured.get("uncertainty"),
        "human_review_required": result.get("human_review_required", False),
        "recommended_next_action": result.get("recommended_next_slot"),
        "logical_model_calls": result.get("logical_model_calls", 0),
        "latency_ms": trace.get("latency_ms") or trace.get("total_ms"),
        "context_headroom": None,
        "truncation_status": trace.get("done_reason"),
        "ocr_meta": ocr_meta,
        "source_handle": None,  # caller fills
    }
    return envelope


def _source_denied_envelope() -> Dict[str, Any]:
    """SOURCE_DENIED: zero model calls, nothing leaked."""
    return {
        "request_id": None,
        "execution_status": "POLICY_BLOCKED",
        "quality_decision": "NOT_EVALUATED",
        "logical_model_calls": 0,
        "error": "SOURCE_DENIED: source handle is not authorized",
        "source_handle": None,
    }


def _build_request(
    args: Dict[str, Any],
    resolved_path: str,
) -> Any:
    from tools.vision_policy import (
        VisionRequest, VisionTask, VisionMode, VisionCriticality,
    )
    task = str(args.get("task", "UI_READ")).upper()
    if task not in _ALLOWED_TASKS:
        task = "UI_READ"
    question = str(args.get("question") or "")
    question = question[:DEFAULT_MAX_QUESTION_CHARS]
    return VisionRequest(
        request_id="vision-router-tool",
        image_source=resolved_path,
        task=VisionTask(task),
        mode=VisionMode.AUTO,
        criticality=VisionCriticality(_criticality_for(task)),
        question=question,
        required_outputs=["observed_text"],
    )


async def _handle_vision_router(args: Dict[str, Any], **kw: Any) -> str:
    """Registry handler. Returns a JSON envelope string (tool protocol)."""
    from tools.vision_orchestrator import analyze_image

    source_handle = str(args.get("source_handle") or "").strip()
    if not source_handle:
        return json.dumps(_source_denied_envelope(), ensure_ascii=False)

    resolved_path = _resolve_source(source_handle)
    if not resolved_path:
        return json.dumps(_source_denied_envelope(), ensure_ascii=False)

    config = None
    try:
        from hermes_cli.config import load_config
        config = load_config()
    except Exception:  # noqa: BLE001
        config = None

    request = _build_request(args, resolved_path)
    try:
        # enabled=None → the server flag (vision_router.enabled) is resolved
        # inside analyze_image. The model cannot influence it.
        # base_url → trusted server-side Ollama endpoint (native transport
        # requires it); never model-visible, never model-supplied.
        from tools.vision_policy import resolve_ollama_base_url

        result = await analyze_image(
            request,
            enabled=None,
            base_url=resolve_ollama_base_url(config),
        )
    except Exception as exc:  # noqa: BLE001 — never crash the tool protocol
        envelope = {
            "request_id": request.request_id,
            "execution_status": "INVALID_RESPONSE",
            "quality_decision": "NOT_EVALUATED",
            "logical_model_calls": 0,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
        return json.dumps(envelope, ensure_ascii=False)

    envelope = _build_envelope(result, config)
    envelope["source_handle"] = source_handle
    envelope["request_id"] = result.get("request_id") or source_handle
    return json.dumps(envelope, ensure_ascii=False)


VISION_ROUTER_SCHEMA = {
    "name": "vision_router_analyze",
    "description": (
        "Analyze an approved screenshot/image through the controlled Vision "
        "Router (orchestrator profiles: Precision native generate / Fast / "
        "OCR). Express only semantic intent: an approved source handle, the "
        "task, and an optional question. All model/transport/context choices "
        "are policy-controlled. Returns a bounded fail-closed result "
        "envelope; OCR text longer than the excerpt limit is truncated with "
        "explicit metadata."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_handle": {
                "type": "string",
                "description": "Approved image handle (e.g. locator://<key> "
                               "or a pre-authorized handle). Arbitrary paths "
                               "and URLs are not authorized.",
            },
            "task": {
                "type": "string",
                "enum": ["UI_READ", "SCENE_DESCRIBE", "EVIDENCE_VERIFY",
                         "EXACT_OCR"],
                "description": "Semantic task intent.",
            },
            "mode": {
                "type": "string",
                "enum": ["AUTO"],
                "description": "Routing mode (AUTO only in the initial "
                               "activation).",
            },
            "region": {
                "type": "string",
                "description": "Optional bounded region hint (task-dependent).",
            },
            "question": {
                "type": "string",
                "description": "Optional question about the image.",
            },
        },
        "required": ["source_handle"],
    },
}


registry.register(
    name=VISION_ROUTER_SCHEMA["name"],
    toolset="vision",
    schema=VISION_ROUTER_SCHEMA,
    handler=_handle_vision_router,
    is_async=True,
    emoji="🛰️",
)
