"""Pure helpers for provider value-for-money comparison.

Two data sources feed the same normalized row shape:

1. **Live OpenRouter catalog** (:func:`fetch_openrouter_models`) — the
   public ``/api/v1/models`` endpoint carries per-model pricing (USD per
   token), context length, and ``supported_parameters`` (tool calling,
   reasoning effort). The agentic filter (tool calling) is what keeps
   TTS / embedding / reranker models out of value rankings.

2. **Bundled models.dev registry** (:func:`offline_rows`) — per-model
   context, cost, and capability flags served from local caches with
   zero network I/O (``agent.models_dev``'s no-network-on-hot-paths
   invariant). Enumerates the curated agentic OpenRouter snapshot.

Normalized row shape (dict):

    id, lab, name, context, in, out, agentic, reasoning

- ``in`` / ``out``: USD per 1M tokens (``0.0`` == free).

This module must stay import-light at module level (stdlib only) so the
CLI command and its tests never pull the heavy hermes imports eagerly.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, List, Optional

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_USER_AGENT = "hermes-providers/1.0"

# Labs commonly used as coding backends. Soft bias for the ``--task code``
# preset on top of the agentic filter — documented, not a quality score.
_CODE_LABS = frozenset(
    {
        "anthropic",
        "openai",
        "deepseek",
        "qwen",
        "google",
        "x-ai",
        "xai",
        "z-ai",
        "glm",
        "moonshotai",
        "mistralai",
        "meta",
        "minimax",
        "kilocode",
        "tencent",
        "nvidia",
        "stepfun",
    }
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _parse_price(raw: Any) -> float:
    """Per-token price string -> USD per 1M tokens; anything invalid -> 0.0."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    # Reject NaN and negative values.
    if not (val > 0 and val == val):
        return 0.0
    return val * 1_000_000


def normalize_from_openrouter(item: dict) -> Optional[dict]:
    """Map one raw ``/api/v1/models`` entry to the normalized row shape."""
    mid = item.get("id")
    if not mid:
        return None
    pricing = item.get("pricing") or {}
    params = item.get("supported_parameters") or []
    reasoning_block = item.get("reasoning") or {}
    str_mid = str(mid)
    return {
        "id": str_mid,
        "lab": str_mid.split("/", 1)[0] if "/" in str_mid else "",
        "name": str(item.get("name") or str_mid),
        "context": int(item.get("context_length") or 0),
        "in": _parse_price(pricing.get("prompt")),
        "out": _parse_price(pricing.get("completion")),
        "agentic": "tools" in params,
        "reasoning": bool(
            "reasoning_effort" in params
            or reasoning_block.get("supported_efforts")
        ),
    }


def normalize_from_openrouter_payload(raw_items: list) -> List[dict]:
    """Map a raw ``data`` array from ``/api/v1/models`` to rows."""
    rows: List[dict] = []
    for item in raw_items or []:
        if isinstance(item, dict):
            row = normalize_from_openrouter(item)
            if row:
                rows.append(row)
    return rows


def normalize_from_models_dev(info: Any) -> Optional[dict]:
    """Map one ``agent.models_dev.ModelInfo`` to the normalized row shape."""
    if info is None:
        return None
    str_mid = str(info.id or "")
    if not str_mid:
        return None
    return {
        "id": str_mid,
        "lab": str_mid.split("/", 1)[0] if "/" in str_mid else "",
        "name": str(info.name or str_mid),
        "context": int(info.context_window or 0),
        "in": float(info.cost_input or 0.0),
        "out": float(info.cost_output or 0.0),
        "agentic": bool(info.tool_call),
        "reasoning": bool(info.reasoning),
    }


def offline_rows() -> List[dict]:
    """Rows from the bundled models.dev registry — no network I/O.

    Enumerates the curated agentic OpenRouter snapshot (hand-picked agent
    backends, so every row that survives models.dev lookup is a real agent
    model) and enriches it with models.dev context/cost/capabilities.
    """
    from agent.models_dev import get_model_info
    from hermes_cli.models import OPENROUTER_MODELS

    rows: List[dict] = []
    for mid, _desc in OPENROUTER_MODELS:
        info = get_model_info("openrouter", mid)
        if info is None:
            continue
        row = normalize_from_models_dev(info)
        if row:
            rows.append(row)
    return rows


def fetch_openrouter_models(timeout: float = 10.0) -> List[dict]:
    """Fetch the live OpenRouter catalog. Fails soft -> [] on any error."""
    try:
        req = urllib.request.Request(
            OPENROUTER_MODELS_URL,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:
        return []
    return normalize_from_openrouter_payload(payload.get("data", []))


def fetch_model_endpoints(model_id: str, timeout: float = 10.0) -> List[dict]:
    """Fetch the providers serving a model (``/api/v1/models/{id}/endpoints``).

    The model id must appear in the URL path as-is (the slash stays
    unencoded — percent-encoding the id returns 404). Fails soft -> [].
    """
    from urllib.parse import quote

    # Only encode chars that are illegal in a path — never the slash, and
    # never ':' (OpenRouter uses it in variant ids like ``:free``).
    safe_id = quote(model_id, safe="/:")
    url = f"https://openrouter.ai/api/v1/models/{safe_id}/endpoints"
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:
        return []
    return normalize_endpoint_payload(payload.get("data", {}).get("endpoints", []))


def normalize_endpoint(ep: dict) -> Optional[dict]:
    """Map one endpoint entry to the endpoint table row shape."""
    provider = ep.get("provider_name")
    if not provider:
        return None
    pricing = ep.get("pricing") or {}
    discount = pricing.get("discount")
    try:
        d = float(discount) if discount not in (None, "") else 0.0
    except (TypeError, ValueError):
        d = 0.0
    discount_pct = round(d * 100) if d > 0 else None
    latency = ep.get("latency_last_30m")
    throughput = ep.get("throughput_last_30m")
    uptime = ep.get("uptime_last_30m")
    return {
        "provider": str(provider),
        "in": _parse_price(pricing.get("prompt")),
        "out": _parse_price(pricing.get("completion")),
        "cache": _parse_price(pricing.get("input_cache_read")),
        "discount_pct": discount_pct,
        "context": int(ep.get("context_length") or 0),
        "latency": float(latency) if isinstance(latency, (int, float)) else None,
        "throughput": float(throughput) if isinstance(throughput, (int, float)) else None,
        "uptime": float(uptime) if isinstance(uptime, (int, float)) else None,
    }


def normalize_endpoint_payload(raw_endpoints: list) -> List[dict]:
    """Map a raw ``data.endpoints`` array to endpoint rows."""
    rows: List[dict] = []
    for ep in raw_endpoints or []:
        if isinstance(ep, dict):
            row = normalize_endpoint(ep)
            if row:
                rows.append(row)
    return rows


def format_endpoint_rows(rows: List[dict]) -> List[str]:
    """Render endpoint rows as an aligned table (2-space indented lines)."""
    if not rows:
        return ["  (no endpoints for this model)"]
    headers = ["PROVIDER", "IN $/M", "OUT $/M", "CACHE $/M", "DISC", "CTX", "LAT", "TPS", "UP%"]
    widths = [len(h) for h in headers]
    cells: List[List[str]] = []
    for row in rows:
        disc = f"-{row['discount_pct']}%" if row.get("discount_pct") else "-"
        line = [
            str(row.get("provider", "")),
            fmt_cost(float(row.get("in") or 0.0)),
            fmt_cost(float(row.get("out") or 0.0)),
            fmt_cost(float(row.get("cache") or 0.0)),
            disc,
            f"{int(row.get('context') or 0):,}",
            f"{row['latency']:.2f}s" if row.get("latency") is not None else "-",
            str(int(row["throughput"])) if row.get("throughput") is not None else "-",
            f"{row['uptime']:.2f}" if row.get("uptime") is not None else "-",
        ]
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))
        cells.append(line)
    out = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append("  " + "  ".join("-" * w for w in widths))
    for line in cells:
        out.append("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(line)))
    return out


# ---------------------------------------------------------------------------
# Ranking / search
# ---------------------------------------------------------------------------


def _row_cost(row: dict) -> float:
    return float(row.get("in") or 0.0) + float(row.get("out") or 0.0)


def _matches_task(row: dict, task: Optional[str]) -> bool:
    if task is None or task == "chat":
        return True
    if task == "reasoning":
        return bool(row.get("reasoning"))
    if task == "code":
        return row.get("lab") in _CODE_LABS
    return True


def rank_by_value(
    rows: List[dict],
    *,
    min_context: int = 0,
    task: Optional[str] = None,
    top: Optional[int] = None,
    include_all: bool = False,
) -> List[dict]:
    """Rank models by value: agentic-capable, cheapest first.

    Free models sort first; ties break toward larger context windows, then
    model id for determinism. ``include_all`` keeps non-agentic models
    (TTS/embeddings/rerankers) in the ranking. ``top`` truncates the result.
    """
    ranked: List[dict] = []
    for row in rows:
        if not include_all and not row.get("agentic"):
            continue
        if int(row.get("context") or 0) < min_context:
            continue
        if not _matches_task(row, task):
            continue
        ranked.append(row)
    ranked.sort(
        key=lambda r: (_row_cost(r), -int(r.get("context") or 0), r.get("id", ""))
    )
    if top and top > 0:
        ranked = ranked[:top]
    return ranked


def search_models(rows: List[dict], query: str) -> List[dict]:
    """Case-insensitive substring match on model id / name / lab."""
    q = (query or "").strip().lower()
    if not q:
        return []
    hits: List[dict] = []
    for row in rows:
        haystack = (
            f"{row.get('id', '')} {row.get('name', '')} {row.get('lab', '')}"
        ).lower()
        if q in haystack:
            hits.append(row)
    return hits


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def fmt_cost(per_m: float) -> str:
    """USD per 1M tokens -> display string; 0 -> 'free'."""
    if per_m <= 0:
        return "free"
    if per_m < 0.01:
        return f"${per_m:.4f}"
    return f"${per_m:.2f}"


def format_rows(rows: List[dict]) -> List[str]:
    """Render normalized rows as an aligned table (2-space indented lines)."""
    if not rows:
        return ["  (no models match)"]
    headers = ["MODEL", "CONTEXT", "IN $/M", "OUT $/M", "CAPS"]
    widths = [len(h) for h in headers]
    cells: List[List[str]] = []
    for row in rows:
        caps = []
        if row.get("agentic"):
            caps.append("tools")
        if row.get("reasoning"):
            caps.append("reasoning")
        line = [
            str(row.get("id", "")),
            f"{int(row.get('context') or 0):,}",
            fmt_cost(float(row.get("in") or 0.0)),
            fmt_cost(float(row.get("out") or 0.0)),
            "+".join(caps) if caps else "-",
        ]
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))
        cells.append(line)
    out = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append("  " + "  ".join("-" * w for w in widths))
    for line in cells:
        out.append("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(line)))
    return out
