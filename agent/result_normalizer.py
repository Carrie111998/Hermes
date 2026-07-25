"""Provider-neutral normalization for terminal model results."""
from __future__ import annotations
from typing import Any

class UnusableModelResultError(RuntimeError):
    """A provider returned no consumable final answer."""

def _text(value: Any) -> str:
    if isinstance(value, str): return value.strip()
    if isinstance(value, list): return "".join(_text(x.get("text", "")) if isinstance(x, dict) else _text(x) for x in value).strip()
    if isinstance(value, dict): return _text(value.get("content") or value.get("text") or value.get("value"))
    return ""

def normalize_model_result(result: Any) -> dict[str, Any]:
    if isinstance(result, str): result = {"final_response": result}
    if not isinstance(result, dict): raise UnusableModelResultError("provider returned a non-mapping result")
    out = dict(result)
    text = _text(out.get("final_response")) or _text(out.get("message")) or _text(out.get("content"))
    if not text and isinstance(out.get("choices"), list) and out["choices"] and isinstance(out["choices"][0], dict):
        text = _text(out["choices"][0].get("message")) or _text(out["choices"][0].get("text"))
    text = text or _text(out.get("output"))
    if out.get("tool_calls"):
        out.update(final_response="", completed=False, failed=False); return out
    if text:
        out.setdefault("completed", True); out.setdefault("failed", False); out["final_response"] = text; return out
    detail = _text(out.get("error")) or "provider returned no usable content"
    out.update(final_response="", completed=False, failed=True, error=f"unusable_model_result: {detail}")
    return out
