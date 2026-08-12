"""What an agent run actually looked at, kept for administrators.

A run's final output says *what* the agent concluded. When someone asks "where
did this company name come from?", the answer has to be recoverable — otherwise
the only evidence is a log line that scrolls away.

Everything here is deterministic and defensive: extraction never executes or
fetches anything, and redaction runs before persistence, so a credential that
leaks into a tool result or a log line never reaches an admin JSON payload.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EvidenceInput",
    "REDACTED",
    "evidence_from_log",
    "evidence_from_output",
    "redact_evidence",
]

REDACTED = "[REDACTED]"

# Key names whose *values* are never safe to keep. Matched as substrings on the
# lowercased key, so `x-api-key`, `Authorization`, and `refresh_token` all hit.
_SECRET_KEY_PARTS = (
    "authorization", "cookie", "token", "secret", "password", "passwd",
    "api_key", "apikey", "access_key", "private_key", "credential",
    "session_id", "bearer", "signature",
)

# Whole keys that carry model/prompt internals or raw tool plumbing. Dropping
# the value (rather than the key) keeps the shape visible without the payload.
_OPAQUE_KEYS = (
    "system_prompt", "system", "prompt", "messages", "tool_args",
    "tool_arguments", "arguments", "input_schema", "raw",
)

# Where a source URL might be named in structured output.
_URL_KEYS = ("url", "source_url", "source", "link", "href", "permalink", "uri")
_FILE_KEYS = ("file", "file_reference", "path", "document", "filename")
_TITLE_KEYS = ("title", "name", "headline", "page_title")

_MAX_STRING = 2000
_MAX_ITEMS = 50
_MAX_DEPTH = 8

_URL_PATTERN = re.compile(r"https?://[^\s\"'<>()\[\]]{3,2000}")


@dataclass
class EvidenceInput:
    source_type: str
    source_url: str = ""
    file_reference: str = ""
    title: str = ""
    metadata: dict = field(default_factory=dict)
    result: Any = None

    def key(self) -> tuple[str, str, str]:
        return (self.source_type, self.source_url, self.file_reference)


def _is_secret_key(key: str) -> bool:
    # Hyphens and spaces fold to underscores so header-style names match the
    # same patterns as JSON keys: `X-Api-Key` must hit `api_key`.
    lowered = re.sub(r"[-\s.]+", "_", str(key).lower())
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def redact_evidence(value: Any, _depth: int = 0) -> Any:
    """Bound and de-credential a value before it is stored or served.

    Secret-looking keys keep their name but lose their value, so an operator can
    still see that a request carried an Authorization header without learning
    what it was. Sizes are capped so one enormous tool result can't turn an
    admin page into a several-megabyte download.
    """
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"

    if isinstance(value, dict):
        out: dict = {}
        for key, item in list(value.items())[:_MAX_ITEMS]:
            if _is_secret_key(key):
                out[key] = REDACTED
            elif str(key).lower() in _OPAQUE_KEYS:
                out[key] = "[OMITTED]"
            else:
                out[key] = redact_evidence(item, _depth + 1)
        if len(value) > _MAX_ITEMS:
            out["…"] = f"[{len(value) - _MAX_ITEMS} more keys]"
        return out

    if isinstance(value, (list, tuple)):
        out_list = [redact_evidence(item, _depth + 1) for item in list(value)[:_MAX_ITEMS]]
        if len(value) > _MAX_ITEMS:
            out_list.append(f"[{len(value) - _MAX_ITEMS} more items]")
        return out_list

    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "…"

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    return redact_evidence(str(value), _depth + 1)


def _first(mapping: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def evidence_from_output(output: dict) -> list[EvidenceInput]:
    """Collect explicitly-named sources from a run's final structured output.

    Only fields that *declare* a source count — a URL that happens to appear
    inside a description is prose, not provenance, and inventing evidence from
    it would make the audit trail less trustworthy, not more.
    """
    found: list[EvidenceInput] = []
    seen: set[tuple[str, str, str]] = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > _MAX_DEPTH or len(found) >= _MAX_ITEMS:
            return
        if isinstance(node, list):
            for item in node[:_MAX_ITEMS]:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return

        url = _first(node, _URL_KEYS)
        file_reference = _first(node, _FILE_KEYS)
        if url.startswith("http://") or url.startswith("https://"):
            candidate = EvidenceInput(
                source_type="web",
                source_url=url,
                title=_first(node, _TITLE_KEYS),
                metadata=redact_evidence(
                    {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
                ),
                result=redact_evidence(node.get("result", node)),
            )
        elif file_reference:
            candidate = EvidenceInput(
                source_type="file",
                file_reference=file_reference,
                title=_first(node, _TITLE_KEYS),
                metadata=redact_evidence(
                    {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
                ),
                result=redact_evidence(node.get("result", node)),
            )
        else:
            candidate = None

        if candidate is not None and candidate.key() not in seen:
            seen.add(candidate.key())
            found.append(candidate)

        for item in list(node.values())[:_MAX_ITEMS]:
            walk(item, depth + 1)

    walk(output)
    return found


def evidence_from_log(line: str) -> list[EvidenceInput]:
    """URLs a run reported visiting, from one line of its output.

    Log lines are raw model/tool stdout, so the line itself is never kept —
    only the URLs found in it, and only when the line does not look like it is
    carrying a credential.
    """
    if not line or _looks_like_secret_line(line):
        return []

    found: list[EvidenceInput] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.findall(line)[:_MAX_ITEMS]:
        url = match.rstrip(".,;:!?")
        if url in seen:
            continue
        seen.add(url)
        found.append(EvidenceInput(source_type="web", source_url=url))
    return found


def _looks_like_secret_line(line: str) -> bool:
    lowered = line.lower()
    return any(f"{part}" in lowered for part in ("authorization:", "api_key=", "token=",
                                                 "password=", "secret=", "bearer "))


def serialize_evidence(value: Any) -> str:
    """JSON for storage. Redaction has already run; this only bounds the size."""
    try:
        return json.dumps(redact_evidence(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps({"unserializable": True})
