"""GradeOS internal function tools for the controlled teacher assistant runtime."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from tools.registry import registry, tool_error, tool_result


TOOLSET = "gradeos_tools"
DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_TOKEN = "gradeos-local-dev-token"
_SESSION_SCOPES: Dict[str, Dict[str, Any]] = {}


def set_gradeos_session_scope(
    session_id: str,
    *,
    teacher_id: str,
    scope: Optional[Dict[str, Any]] = None,
) -> None:
    """Bind a Hermes session to the authenticated GradeOS teacher and scope."""

    if not session_id:
        return
    scoped = dict(scope or {})
    scoped["teacher_id"] = teacher_id
    _SESSION_SCOPES[session_id] = scoped


def _base_url() -> str:
    return os.getenv("GRADEOS_INTERNAL_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _service_token() -> str:
    return (
        os.getenv("GRADEOS_INTERNAL_SERVICE_TOKEN")
        or os.getenv("HERMES_AGENT_SERVICE_TOKEN")
        or os.getenv("HERMES_INTERNAL_KEY")
        or DEFAULT_TOKEN
    )


def _timeout() -> float:
    try:
        return float(os.getenv("GRADEOS_INTERNAL_API_TIMEOUT_SECONDS", "30"))
    except ValueError:
        return 30.0


def _clean_params(params: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value == "":
            continue
        if isinstance(value, list) and not value:
            continue
        clean[key] = value
    return clean


def _json_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> str:
    query = urlencode(_clean_params(params or {}), doseq=True)
    url = f"{_base_url()}{path}"
    if query:
        url = f"{url}?{query}"

    data = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {_service_token()}",
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urlopen(request, timeout=_timeout()) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return tool_error(
            "GradeOS internal API returned an error",
            status_code=exc.code,
            detail=detail[:1200],
            path=path,
        )
    except URLError as exc:
        return tool_error(
            "GradeOS internal API is unreachable",
            detail=str(exc.reason),
            path=path,
        )

    if not raw:
        return tool_result({"status": "ok"})
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return tool_result({"raw": raw})
    return tool_result(parsed)


def _scope_query(args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "teacher_id": args.get("teacher_id"),
        "class_id": args.get("class_id"),
        "homework_id": args.get("homework_id"),
    }


def _require_fields(args: Dict[str, Any], fields: Iterable[str]) -> Optional[str]:
    missing = [field for field in fields if not args.get(field)]
    if missing:
        return f"Missing required field(s): {', '.join(missing)}"
    return None


def _session_scope(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    session_id = kwargs.get("session_id")
    return _SESSION_SCOPES.get(str(session_id), {}) if session_id else {}


def _scoped_args(
    args: Dict[str, Any],
    kwargs: Dict[str, Any],
    *,
    required: Iterable[str],
) -> tuple[Dict[str, Any], Optional[str]]:
    bound = _session_scope(kwargs)
    merged = {**args}
    for key in ("teacher_id", "batch_id", "class_id", "homework_id"):
        bound_value = bound.get(key)
        provided_value = merged.get(key)
        if bound_value:
            if provided_value and str(provided_value) != str(bound_value):
                return {}, f"{key} is outside the current GradeOS session scope"
            merged[key] = bound_value

    error = _require_fields(merged, required)
    return merged, error


def _get_batch_summary(args: Dict[str, Any], **kwargs: Any) -> str:
    scoped, error = _scoped_args(args, kwargs, required=("teacher_id", "batch_id"))
    if error:
        return tool_error(error)
    return _json_request(
        "GET",
        f"/api/internal/hermes/batches/{quote(str(scoped['batch_id']), safe='')}/summary",
        params=_scope_query(scoped),
    )


def _get_batch_rubric(args: Dict[str, Any], **kwargs: Any) -> str:
    scoped, error = _scoped_args(args, kwargs, required=("teacher_id", "batch_id"))
    if error:
        return tool_error(error)
    return _json_request(
        "GET",
        f"/api/internal/hermes/batches/{quote(str(scoped['batch_id']), safe='')}/rubric",
        params=_scope_query(scoped),
    )


def _get_student_result(args: Dict[str, Any], **kwargs: Any) -> str:
    scoped, error = _scoped_args(
        args,
        kwargs,
        required=("teacher_id", "batch_id", "student_id"),
    )
    if error:
        return tool_error(error)
    return _json_request(
        "GET",
        f"/api/internal/hermes/batches/{quote(str(scoped['batch_id']), safe='')}/students/"
        f"{quote(str(scoped['student_id']), safe='')}/result",
        params=_scope_query(scoped),
    )


def _get_question_statistics(args: Dict[str, Any], **kwargs: Any) -> str:
    scoped, error = _scoped_args(
        args,
        kwargs,
        required=("teacher_id", "batch_id", "question_id"),
    )
    if error:
        return tool_error(error)
    return _json_request(
        "GET",
        f"/api/internal/hermes/batches/{quote(str(scoped['batch_id']), safe='')}/questions/"
        f"{quote(str(scoped['question_id']), safe='')}/statistics",
        params=_scope_query(scoped),
    )


def _get_risk_signals(args: Dict[str, Any], **kwargs: Any) -> str:
    scoped, error = _scoped_args(args, kwargs, required=("teacher_id", "batch_id"))
    if error:
        return tool_error(error)
    return _json_request(
        "GET",
        f"/api/internal/hermes/batches/{quote(str(scoped['batch_id']), safe='')}/risk-signals",
        params=_scope_query(scoped),
    )


def _controlled_evidence_search(args: Dict[str, Any], **kwargs: Any) -> str:
    scoped, error = _scoped_args(args, kwargs, required=("teacher_id", "batch_id", "query"))
    if error:
        return tool_error(error)
    scope = {
        "teacher_id": scoped.get("teacher_id"),
        "batch_id": scoped.get("batch_id"),
        "class_id": scoped.get("class_id"),
        "homework_id": scoped.get("homework_id"),
        "student_ids": scoped.get("student_ids") or [],
        "question_ids": scoped.get("question_ids") or [],
        "source_ids": scoped.get("source_ids") or [],
    }
    return _json_request(
        "POST",
        "/api/internal/hermes/retrieval/search",
        body={
            "query": scoped.get("query"),
            "scope": scope,
            "limit": scoped.get("limit") or 8,
            "refresh": scoped.get("refresh", True),
        },
    )


def _audit_tool_call(args: Dict[str, Any], **kwargs: Any) -> str:
    scoped, error = _scoped_args(args, kwargs, required=("teacher_id", "tool_name"))
    if error:
        return tool_error(error)
    return _json_request(
        "POST",
        "/api/internal/hermes/audit/tool-call",
        body={
            "session_id": scoped.get("session_id") or kwargs.get("session_id"),
            "teacher_id": scoped.get("teacher_id"),
            "batch_id": scoped.get("batch_id"),
            "tool_name": scoped.get("tool_name"),
            "scope": scoped.get("scope") if isinstance(scoped.get("scope"), dict) else {},
            "status": scoped.get("status") or "ok",
            "metadata": (
                scoped.get("metadata") if isinstance(scoped.get("metadata"), dict) else {}
            ),
        },
    )


def _available() -> bool:
    return bool(_base_url() and _service_token())


_SCOPE_PROPERTIES = {
    "teacher_id": {
        "type": "string",
        "description": "Authenticated GradeOS teacher id from the request.",
    },
    "batch_id": {"type": "string", "description": "Scoped grading batch id."},
    "class_id": {"type": "string", "description": "Optional scoped class id."},
    "homework_id": {"type": "string", "description": "Optional scoped homework id."},
}


def _schema(
    name: str,
    description: str,
    properties: Dict[str, Any],
    required: list[str],
) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


registry.register(
    name="gradeos_batch_summary",
    toolset=TOOLSET,
    schema=_schema(
        "gradeos_batch_summary",
        "Read a scoped GradeOS grading batch summary through the internal API.",
        _SCOPE_PROPERTIES,
        ["teacher_id", "batch_id"],
    ),
    handler=_get_batch_summary,
    check_fn=_available,
    max_result_size_chars=12000,
)

registry.register(
    name="gradeos_batch_rubric",
    toolset=TOOLSET,
    schema=_schema(
        "gradeos_batch_rubric",
        "Read the scoped rubric or scoring criteria for a GradeOS grading batch.",
        _SCOPE_PROPERTIES,
        ["teacher_id", "batch_id"],
    ),
    handler=_get_batch_rubric,
    check_fn=_available,
    max_result_size_chars=12000,
)

registry.register(
    name="gradeos_student_result",
    toolset=TOOLSET,
    schema=_schema(
        "gradeos_student_result",
        "Read one scoped student's grading result from a GradeOS batch.",
        {
            **_SCOPE_PROPERTIES,
            "student_id": {"type": "string", "description": "Scoped student id."},
        },
        ["teacher_id", "batch_id", "student_id"],
    ),
    handler=_get_student_result,
    check_fn=_available,
    max_result_size_chars=16000,
)

registry.register(
    name="gradeos_question_statistics",
    toolset=TOOLSET,
    schema=_schema(
        "gradeos_question_statistics",
        "Read scoped aggregate statistics for one question in a GradeOS batch.",
        {
            **_SCOPE_PROPERTIES,
            "question_id": {"type": "string", "description": "Scoped question id."},
        },
        ["teacher_id", "batch_id", "question_id"],
    ),
    handler=_get_question_statistics,
    check_fn=_available,
    max_result_size_chars=12000,
)

registry.register(
    name="gradeos_risk_signals",
    toolset=TOOLSET,
    schema=_schema(
        "gradeos_risk_signals",
        "Read scoped low-confidence or risk signals for a GradeOS grading batch.",
        _SCOPE_PROPERTIES,
        ["teacher_id", "batch_id"],
    ),
    handler=_get_risk_signals,
    check_fn=_available,
    max_result_size_chars=12000,
)

registry.register(
    name="gradeos_controlled_evidence_search",
    toolset=TOOLSET,
    schema=_schema(
        "gradeos_controlled_evidence_search",
        "Search scoped GradeOS grading evidence through controlled keyword/filter retrieval.",
        {
            **_SCOPE_PROPERTIES,
            "query": {"type": "string", "description": "Evidence search query."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "refresh": {"type": "boolean"},
            "student_ids": {"type": "array", "items": {"type": "string"}},
            "question_ids": {"type": "array", "items": {"type": "string"}},
            "source_ids": {"type": "array", "items": {"type": "string"}},
        },
        ["teacher_id", "batch_id", "query"],
    ),
    handler=_controlled_evidence_search,
    check_fn=_available,
    max_result_size_chars=20000,
)

registry.register(
    name="gradeos_audit_tool_call",
    toolset=TOOLSET,
    schema=_schema(
        "gradeos_audit_tool_call",
        "Write an audit record for a GradeOS tool call. This does not execute business actions.",
        {
            "teacher_id": {"type": "string"},
            "tool_name": {"type": "string"},
            "status": {"type": "string"},
            "session_id": {"type": "string"},
            "batch_id": {"type": "string"},
            "scope": {"type": "object"},
            "metadata": {"type": "object"},
        },
        ["teacher_id", "tool_name"],
    ),
    handler=_audit_tool_call,
    check_fn=_available,
    max_result_size_chars=4000,
)
