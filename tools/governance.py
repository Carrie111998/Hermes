"""Cross-tool governance primitives.

The registry and approval layer use this module to describe a tool call without
persisting its raw arguments.  The argument digest binds an approval to the
exact call; the preview is recursively redacted for operator-facing UIs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Mapping


class RiskClass(str, Enum):
    """Ordered risk classes shared by built-in, plugin, and MCP tools."""

    READ = "read"
    WRITE = "write"
    EXEC = "exec"
    EXTERNAL = "external"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"

    @property
    def level(self) -> int:
        return _RISK_ORDER[self]

    @classmethod
    def parse(cls, value: "RiskClass | str") -> "RiskClass":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"Unknown risk class {value!r}; expected one of: {allowed}") from exc


_RISK_ORDER = {
    RiskClass.READ: 0,
    RiskClass.WRITE: 1,
    RiskClass.EXEC: 2,
    RiskClass.EXTERNAL: 3,
    RiskClass.DESTRUCTIVE: 4,
    RiskClass.PRIVILEGED: 5,
}


def risk_at_most(actual: RiskClass | str, ceiling: RiskClass | str) -> bool:
    return RiskClass.parse(actual).level <= RiskClass.parse(ceiling).level


_PRIVILEGED_TOKENS = (
    "submit_filing",
    "file_with_court",
    "issue_invoice",
    "publish_release",
    "deploy_production",
    "rotate_secret",
    "grant_role",
    "iam_",
)
_DESTRUCTIVE_TOKENS = (
    "delete",
    "destroy",
    "remove",
    "revoke",
    "purge",
    "drop_",
    "reset_",
)
_EXEC_NAMES = {
    "terminal",
    "execute_code",
    "process",
    "computer_use",
    "browser_press",
    "browser_click",
    "browser_type",
}
_EXTERNAL_TOKENS = (
    "send_",
    "publish",
    "upload",
    "create_issue",
    "create_pull",
    "comment",
    "browser_navigate",
    "web_search",
    "web_extract",
)
_WRITE_NAMES = {
    "write_file",
    "patch",
    "skill_manage",
    "memory",
}
_READ_PREFIXES = ("read_", "search_", "list_", "get_", "locate_", "map_")


def infer_risk_class(tool_name: str, toolset: str = "") -> RiskClass:
    """Return a conservative default for tools without explicit metadata.

    Explicit metadata always wins in the registry. Unknown MCP tools default to
    ``external`` because they may mutate a remote service; other unrecognised
    tools default to ``privileged`` so new plugins cannot silently inherit a
    read-only classification.
    """

    name = (tool_name or "").strip().lower()
    toolset_name = (toolset or "").strip().lower()
    if any(token in name for token in _PRIVILEGED_TOKENS):
        return RiskClass.PRIVILEGED
    if any(token in name for token in _DESTRUCTIVE_TOKENS):
        return RiskClass.DESTRUCTIVE
    if name in _EXEC_NAMES or name.startswith("terminal_"):
        return RiskClass.EXEC
    if name in _WRITE_NAMES or name.startswith(("write_", "patch_", "update_")):
        return RiskClass.WRITE
    if any(token in name for token in _EXTERNAL_TOKENS) or toolset_name in {
        "messaging",
        "browser",
    }:
        return RiskClass.EXTERNAL
    if name.startswith(_READ_PREFIXES):
        return RiskClass.READ
    if toolset_name.startswith("mcp-"):
        return RiskClass.EXTERNAL
    # Unknown plugin/tool names fail conservative: metadata must never silently
    # downgrade an unrecognised side-effecting tool to read-only.
    return RiskClass.PRIVILEGED


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "private_key",
}
_TARGET_KEYS = (
    "target",
    "recipient",
    "destination",
    "destination_path",
    "path",
    "url",
    "channel",
    "chat_id",
    "repo",
    "repository",
    "project_id",
    "name",
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|api_key|apikey|authorization|credential|password|secret|token)=)[^&#\s]+"
)
_URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")


def _redact_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        from agent.redact import redact_sensitive_text

        value = redact_sensitive_text(value)
    except Exception:
        pass
    value = _QUERY_SECRET_RE.sub(r"\1<redacted>", value)
    return _URL_USERINFO_RE.sub(r"\1<redacted>@", value)


def _redacted_copy(value: Any, *, key: str = "") -> Any:
    if key.strip().lower() in _SENSITIVE_KEYS:
        return "***REDACTED***"
    if isinstance(value, Mapping):
        return {str(k): _redacted_copy(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted_copy(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return _redact_scalar(value)
    return repr(value)


def _canonical_json(args: Mapping[str, Any] | None) -> str:
    return json.dumps(args or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=repr)


def infer_target(args: Mapping[str, Any] | None) -> str:
    if not isinstance(args, Mapping):
        return ""
    for key in _TARGET_KEYS:
        value = args.get(key)
        if value is not None and value != "":
            if isinstance(value, (dict, list, tuple)):
                return json.dumps(_redacted_copy(value), sort_keys=True, ensure_ascii=False)
            return str(_redact_scalar(value))
    command = args.get("command")
    if command:
        return str(_redact_scalar(command))[:240]
    return ""


@dataclass(frozen=True)
class ToolCallEnvelope:
    tool_name: str
    risk_class: str
    target: str
    args_digest: str
    args_preview: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def build_tool_call_envelope(
    tool_name: str,
    args: Mapping[str, Any] | None,
    *,
    risk_class: RiskClass | str,
    target_resolver: Callable[[Mapping[str, Any]], str] | None = None,
) -> ToolCallEnvelope:
    canonical = _canonical_json(args)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    redacted = _redacted_copy(args or {})
    preview = json.dumps(redacted, sort_keys=True, ensure_ascii=False, default=repr)
    if len(preview) > 4096:
        preview = preview[:4080] + "…<truncated>"
    if target_resolver is not None:
        target = str(target_resolver(args or {}))
        target = str(_redact_scalar(target))
    else:
        target = str(_redact_scalar(infer_target(args)))
    return ToolCallEnvelope(
        tool_name=tool_name,
        risk_class=RiskClass.parse(risk_class).value,
        target=target,
        args_digest=digest,
        args_preview=preview,
    )
