"""GigaChat tool-schema sanitization.

GigaChat's function-call decoder degrades on *wide* parameter schemas. Past
roughly a dozen properties it stops emitting a well-formed arguments object:
it silently drops keys the prose marked required and writes a JSON formatting
fragment (``",\n    "``) into an unrelated field in place of a real value. The
call still arrives as parseable JSON, so nothing upstream notices — the tool
just rejects it, the model reads the rejection as a tool malfunction, and
retries the identical call.

Measured against GigaChat-2-Max through the local shim, asking it to create a
cron job:

    cronjob as shipped (17 properties, 10.0 KB)  -> 3/3 malformed
                                                    ('prompt' dropped,
                                                     '"script": ",\\n    "')
    same schema trimmed to 8 properties (4.4 KB) -> 3/3 correct

Width is what matters, not byte size: a 6-property schema whose descriptions
were shortened to 2.3 KB *also* failed, because this model leans on the verbose
per-property prose to fill required fields. So trim the property count and
leave the descriptions alone.

Properties are kept in declaration order — which in this codebase runs
most-important-first — and anything named in ``required`` is always kept, even
if that pushes the result past the cap. Advanced options beyond the cap become
unreachable in GigaChat sessions; that is the deliberate trade for tool calls
that are well-formed at all.
"""

from __future__ import annotations

from typing import Any, Dict, List

# 10 properties still round-tripped cleanly in testing and 17 did not; 8 keeps
# a margin below the observed edge without cutting into the common fields of
# any shipped tool.
MAX_GIGACHAT_PROPERTIES = 8


def is_gigachat_model(model: str | None) -> bool:
    """Return whether *model* is served by GigaChat."""
    return bool(model) and str(model).strip().lower().startswith("gigachat")


def sanitize_gigachat_tool_parameters(params: Any) -> Any:
    """Narrow a JSON-Schema ``parameters`` object to a GigaChat-safe width.

    Returns *params* unchanged (identity) when nothing needs trimming, so
    callers can cheaply detect a no-op.
    """
    if not isinstance(params, dict):
        return params

    properties = params.get("properties")
    if not isinstance(properties, dict) or len(properties) <= MAX_GIGACHAT_PROPERTIES:
        return params

    required = params.get("required")
    required_names = [n for n in required if isinstance(n, str)] if isinstance(required, list) else []

    kept: Dict[str, Any] = {name: properties[name] for name in required_names if name in properties}
    for name, spec in properties.items():
        if len(kept) >= MAX_GIGACHAT_PROPERTIES:
            break
        kept.setdefault(name, spec)

    # Preserve the original declaration order rather than required-first, so
    # the model reads the schema in the order its descriptions were written.
    ordered = {name: properties[name] for name in properties if name in kept}
    return {**params, "properties": ordered}


def sanitize_gigachat_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply ``sanitize_gigachat_tool_parameters`` to every tool's parameters."""
    if not tools:
        return tools

    sanitized: List[Dict[str, Any]] = []
    any_change = False
    for tool in tools:
        if not isinstance(tool, dict):
            sanitized.append(tool)
            continue
        fn = tool.get("function")
        if not isinstance(fn, dict):
            sanitized.append(tool)
            continue
        params = fn.get("parameters")
        repaired = sanitize_gigachat_tool_parameters(params)
        if repaired is not params:
            any_change = True
            sanitized.append({**tool, "function": {**fn, "parameters": repaired}})
        else:
            sanitized.append(tool)

    return sanitized if any_change else tools
