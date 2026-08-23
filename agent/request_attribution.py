"""Opt-in metadata-only attribution for LiteLLM spend-log joins."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlparse


SCHEMA = "aos.telemetry_envelope.v1"
_LITELLM_TOKEN_RE = re.compile(r"(?:^|[:_./-])litellm(?:$|[:_./-])", re.I)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _normalized_endpoint(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if text.endswith("/v1"):
        text = text[:-3]
    return text.casefold()


def _is_configured_litellm_route(agent: Any) -> bool:
    provider = str(getattr(agent, "provider", "") or "")
    base_url = str(getattr(agent, "base_url", "") or "")
    try:
        hostname = urlparse(base_url).hostname or ""
    except ValueError:
        hostname = ""
    if _LITELLM_TOKEN_RE.search(provider) or _LITELLM_TOKEN_RE.search(hostname):
        return True
    target = _normalized_endpoint(base_url)
    endpoints = getattr(agent, "_request_attribution_litellm_endpoints", ()) or ()
    return bool(target and target in {_normalized_endpoint(item) for item in endpoints})


def _profile_name() -> Optional[str]:
    explicit = str(os.environ.get("HERMES_PROFILE") or "").strip()
    if explicit:
        return explicit
    try:
        from hermes_cli.profiles import get_active_profile_name

        return str(get_active_profile_name() or "").strip() or None
    except Exception:
        return None


def _provider_slot(model: str) -> Optional[str]:
    lowered = model.casefold()
    if "lightcloud" in lowered:
        return "lightcloud"
    if "oscar" in lowered:
        return "oscar"
    if "local" in lowered or "ollama" in lowered:
        return "local"
    return None


def _git_context() -> dict[str, Optional[str]]:
    workspace = str(os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if not workspace:
        return {
            "repository": None,
            "branch": None,
            "commit_sha": None,
            "pr_url": str(os.environ.get("HERMES_KANBAN_PR_URL") or "").strip()
            or None,
        }
    try:
        from hermes_cli.github_receipts import git_expectations

        context = git_expectations(workspace)
    except Exception:
        context = {"repository": None, "branch": None, "commit_sha": None}
    branch = str(os.environ.get("HERMES_KANBAN_BRANCH") or "").strip()
    if branch:
        context["branch"] = branch
    context["pr_url"] = (
        str(os.environ.get("HERMES_KANBAN_PR_URL") or "").strip() or None
    )
    return context


def build_request_envelope(
    agent: Any,
    *,
    call_role: str,
    retry_count: int,
    streaming: bool,
    action_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return the complete explicit-null spend-log envelope."""
    git_context = _git_context()
    logical_id = str(
        action_id or getattr(agent, "_current_api_request_id", "") or ""
    ).strip()
    physical_id = f"req_{uuid.uuid4().hex}"
    model = str(getattr(agent, "model", "") or "")
    return {
        "schema": SCHEMA,
        "surface": "hermes",
        "interface": str(getattr(agent, "platform", "") or "") or None,
        "profile": _profile_name(),
        "session_id": str(getattr(agent, "session_id", "") or "") or None,
        "task_id": str(os.environ.get("HERMES_KANBAN_TASK") or "").strip() or None,
        "provider_slot": _provider_slot(model),
        "model": model or None,
        "repository": git_context.get("repository"),
        "branch": git_context.get("branch"),
        "commit_sha": git_context.get("commit_sha"),
        "pr_url": git_context.get("pr_url"),
        "requested_at": _utc_now(),
        "completed_at": None,
        "request_id": physical_id,
        "action_id": logical_id or physical_id,
        "status": "requested",
        "call_role": call_role or "primary",
        "retry_count": max(int(retry_count or 0), 0),
        "streaming": bool(streaming),
    }


def attach_request_attribution(
    agent: Any,
    request: Mapping[str, Any],
    *,
    call_role: Optional[str] = None,
    retry_count: Optional[int] = None,
    streaming: bool = False,
    action_id: Optional[str] = None,
) -> dict[str, Any]:
    """Attach one physical-request envelope through LiteLLM's log header.

    ``x-litellm-spend-logs-metadata`` is consumed by the proxy into
    ``StandardLoggingPayload.metadata.spend_logs_metadata``. Nesting under
    ``aos`` keeps high-cardinality values out of Prometheus unless an operator
    explicitly configures one of those paths as a label.
    """
    copied = dict(request)
    if not bool(getattr(agent, "_request_attribution_enabled", False)):
        return copied
    if not _is_configured_litellm_route(agent):
        return copied
    role = call_role or str(
        getattr(agent, "_request_attribution_call_role", "") or "primary"
    )
    retry = (
        retry_count
        if retry_count is not None
        else int(getattr(agent, "_request_attribution_retry_count", 0) or 0)
        + int(getattr(agent, "_request_attribution_stream_retry_count", 0) or 0)
    )
    envelope = build_request_envelope(
        agent,
        call_role=role,
        retry_count=retry,
        streaming=streaming,
        action_id=action_id,
    )
    request_model = str(copied.get("model") or "").strip()
    if request_model:
        envelope["model"] = request_model
        envelope["provider_slot"] = _provider_slot(request_model)
    existing_headers = copied.get("extra_headers")
    headers = dict(existing_headers) if isinstance(existing_headers, Mapping) else {}
    headers["x-litellm-spend-logs-metadata"] = json.dumps(
        {"aos": envelope},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    copied["extra_headers"] = headers
    return copied
