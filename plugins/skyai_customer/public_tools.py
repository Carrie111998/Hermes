"""Public-safe SkyVision tools for a customer-facing SkyAI Hermes runtime."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen

from hermes_constants import get_hermes_home


PUBLIC_CATALOG_BASE_URL = os.getenv(
    "SKYAI_PUBLIC_CATALOG_BASE_URL",
    "https://cache.skyvision.bg/api/v2",
).rstrip("/")
PUBLIC_EVENTS_BASE_URL = os.getenv(
    "SKYAI_PUBLIC_EVENTS_BASE_URL",
    "https://panel.skyvision.bg/api/product/events",
).rstrip("/")
BGN_PER_EUR = Decimal("1.95583")
DEFAULT_HTTP_TIMEOUT_SECONDS = 8.0
MAX_CATALOG_SIZE = 3000
MAX_RETURN_ITEMS = 12
MAX_EVENT_PROPERTY_VALUE_LENGTH = 500

ALLOWED_EVENT_TYPES = frozenset(
    {
        "chat_started",
        "chat_message_customer",
        "chat_message_assistant",
        "product_viewed",
        "product_recommended",
        "card_clicked",
        "add_to_cart",
        "checkout_started",
        "purchase_completed",
        "abandoned_cart_candidate",
        "support_escalation",
        "qa_feedback",
    }
)
SENSITIVE_PROPERTY_KEYS = frozenset(
    {
        "email",
        "phone",
        "telephone",
        "mobile",
        "voucher",
        "voucher_code",
        "order_id",
        "payment_id",
        "card",
        "card_number",
        "name",
        "full_name",
        "address",
        "ip",
        "raw_message",
        "message",
        "secret",
        "token",
        "password",
        "api_key",
    }
)
SENSITIVE_VALUE_RE = re.compile(
    r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|(?:\+?\d[\d\s().-]{7,}\d)|\b(?:sk-|ghp_|xox[baprs]-)[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


SKYAI_CATALOG_SEARCH_SCHEMA = {
    "name": "skyai_catalog_search",
    "description": (
        "Search SkyVision's public catalog cache for customer-safe product candidates. "
        "Use for sales discovery and recommendations. Prices are accepted in EUR and "
        "converted to the public cache BGN filters internally."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Customer search phrase."},
            "min_price_eur": {"type": "number", "description": "Optional lower budget in EUR."},
            "max_price_eur": {"type": "number", "description": "Optional upper budget in EUR."},
            "limit": {"type": "integer", "description": "Maximum returned products, default 8, max 12."},
        },
    },
}

SKYAI_PRODUCT_DETAIL_SCHEMA = {
    "name": "skyai_product_detail",
    "description": (
        "Fetch public product detail from SkyVision cache by product URL or slug path. "
        "The tool normalizes /подарък/ URLs to the API product path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "product_url": {"type": "string", "description": "Full public product URL."},
            "product_path": {"type": "string", "description": "Path after skyvision.bg, with or without /подарък/."},
        },
    },
}

SKYAI_PRODUCT_SLOTS_SCHEMA = {
    "name": "skyai_product_slots",
    "description": (
        "Fetch public fixed slots, working periods, and request slots for one SkyVision product id. "
        "Use only for public availability guidance; do not create reservations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "product_id": {"type": "integer", "description": "SkyVision product id."},
            "start_date": {"type": "string", "description": "Optional YYYY-MM-DD start."},
            "end_date": {"type": "string", "description": "Optional YYYY-MM-DD end."},
        },
        "required": ["product_id"],
    },
}

SKYAI_EVENT_LOG_APPEND_SCHEMA = {
    "name": "skyai_event_log_append",
    "description": (
        "Append one sanitized SkyAI customer-intelligence event. Do not pass raw chat text, "
        "voucher codes, names, emails, phone numbers, IPs, tokens, or payment/order data. "
        "This is a local/dev append-only stub; Cloud SQL is the production target."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "event_type": {"type": "string", "enum": sorted(ALLOWED_EVENT_TYPES)},
            "anonymous_id": {"type": "string", "description": "Opaque anonymous id, if already safe."},
            "conversation_id": {"type": "string", "description": "Opaque conversation id."},
            "properties": {"type": "object", "description": "Sanitized metadata only."},
        },
        "required": ["event_type"],
    },
}


def _http_json(url: str, *, timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS) -> Any:
    request = Request(url, headers={"User-Agent": "SkyAI-Hermes-v2/0.1"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _money_eur_to_bgn(value: float | int | str | None) -> int:
    if value is None or value == "":
        return 0
    decimal = Decimal(str(value)) * BGN_PER_EUR
    return int(decimal.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _safe_limit(limit: int | None) -> int:
    if not limit:
        return 8
    return max(1, min(MAX_RETURN_ITEMS, int(limit)))


def handle_skyai_catalog_search(
    query: str = "",
    min_price_eur: float | None = None,
    max_price_eur: float | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    size = MAX_CATALOG_SIZE
    min_price_bgn = _money_eur_to_bgn(min_price_eur)
    max_price_bgn = _money_eur_to_bgn(max_price_eur) if max_price_eur is not None else 4000
    url = (
        f"{PUBLIC_CATALOG_BASE_URL}/products"
        f"?page=1&size={size}&sort=&minPrice={min_price_bgn}&maxPrice={max_price_bgn}"
        f"&search={quote(query or '')}"
    )
    payload = _http_json(url)
    items = _extract_products(payload)[: _safe_limit(limit)]
    return {
        "status": "ok",
        "source": "skyvision_public_cache",
        "query": query or "",
        "filters": {
            "min_price_eur": min_price_eur,
            "max_price_eur": max_price_eur,
            "min_price_bgn": min_price_bgn,
            "max_price_bgn": max_price_bgn,
        },
        "count": len(items),
        "items": [_sanitize_product_summary(item) for item in items],
    }


def handle_skyai_product_detail(product_url: str = "", product_path: str = "") -> dict[str, Any]:
    normalized_path = normalize_product_path(product_url=product_url, product_path=product_path)
    if not normalized_path:
        return {"status": "error", "error": "product_url_or_product_path_required"}
    url = f"{PUBLIC_CATALOG_BASE_URL}/product/{quote(normalized_path, safe='/')}"
    payload = _http_json(url)
    return {
        "status": "ok",
        "source": "skyvision_public_cache",
        "product_path": normalized_path,
        "detail": _sanitize_product_detail(payload),
    }


def handle_skyai_product_slots(
    product_id: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    if int(product_id) <= 0:
        return {"status": "error", "error": "invalid_product_id"}
    query = ""
    if start_date and end_date:
        _validate_iso_date(start_date)
        _validate_iso_date(end_date)
        query = f"?startDate={quote(start_date)}&endDate={quote(end_date)}"
    url = f"{PUBLIC_EVENTS_BASE_URL}/{int(product_id)}{query}"
    payload = _http_json(url)
    fixed = payload.get("fixedSlots") or []
    request_slots = payload.get("requestSlots") or []
    working_periods = payload.get("workingPeriods") or []
    return {
        "status": "ok",
        "source": "skyvision_public_events",
        "product_id": int(product_id),
        "fixed_slots_count": len(fixed) if isinstance(fixed, list) else 0,
        "request_slots_count": len(request_slots) if isinstance(request_slots, list) else 0,
        "working_periods_count": len(working_periods) if isinstance(working_periods, list) else 0,
        "fixed_slots": _first_items(fixed, 12),
        "request_slots": _first_items(request_slots, 12),
        "working_periods": _first_items(working_periods, 12),
    }


def handle_skyai_event_log_append(
    event_type: str,
    anonymous_id: str = "",
    conversation_id: str = "",
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if event_type not in ALLOWED_EVENT_TYPES:
        return {"status": "error", "error": "unsupported_event_type"}
    properties = properties or {}
    sensitive_reason = _sensitive_payload_reason(properties)
    if sensitive_reason:
        return {"status": "blocked", "reason": sensitive_reason, "written": False}

    event = {
        "schema": "skyai_ci.events.local_jsonl.v1",
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "anonymous_id_hash": _hash_optional(anonymous_id),
        "conversation_id_hash": _hash_optional(conversation_id),
        "properties": properties,
    }
    path = _event_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "status": "ok",
        "written": True,
        "storage": "local_jsonl_append_only",
        "path": str(path),
        "schema": event["schema"],
    }


def normalize_product_path(*, product_url: str = "", product_path: str = "") -> str:
    raw = product_path or ""
    if product_url:
        split = urlsplit(product_url)
        raw = split.path or raw
        if not raw and split.query:
            raw = parse_qs(split.query).get("path", [""])[0]
    raw = unquote(raw).strip()
    raw = raw.split("#", 1)[0].split("?", 1)[0].strip("/")
    if raw.startswith("подарък/"):
        raw = raw[len("подарък/") :]
    return raw.strip("/")


def _extract_products(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "products", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_products(value)
            if nested:
                return nested
    return []


def _sanitize_product_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id") or item.get("product_id"),
        "title": item.get("title") or item.get("name"),
        "slug": item.get("slug"),
        "category_slug": item.get("category_slug") or item.get("categorySlug"),
        "url": item.get("url"),
        "location": item.get("location") or item.get("city") or item.get("region"),
        "price": item.get("price") or item.get("price_bgn") or item.get("priceBgn"),
        "price_eur": item.get("price_eur") or item.get("priceEur"),
        "duration": item.get("duration"),
        "participants": item.get("participants") or item.get("participant_count"),
    }


def _sanitize_product_detail(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"raw_type": type(payload).__name__}
    source = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    allowed = {
        "id",
        "title",
        "name",
        "slug",
        "category_slug",
        "categorySlug",
        "url",
        "location",
        "city",
        "region",
        "price",
        "price_bgn",
        "priceBgn",
        "price_eur",
        "priceEur",
        "duration",
        "participants",
        "participant_count",
        "min_age",
        "max_weight",
        "description",
        "included",
        "requirements",
        "variants",
        "options",
        "images",
        "provider",
    }
    return {key: value for key, value in source.items() if key in allowed}


def _first_items(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[:limit]


def _validate_iso_date(value: str) -> None:
    date.fromisoformat(value)


def _event_log_path() -> Path:
    override = os.getenv("SKYAI_V2_EVENT_LOG_PATH")
    if override:
        return Path(override).expanduser()
    return get_hermes_home() / "skyai_v2" / "events.jsonl"


def _hash_optional(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sensitive_payload_reason(properties: dict[str, Any]) -> str | None:
    for key, value in properties.items():
        normalized = str(key).strip().lower()
        if normalized in SENSITIVE_PROPERTY_KEYS:
            return f"sensitive_property_key:{normalized}"
        if len(json.dumps(value, ensure_ascii=False, default=str)) > MAX_EVENT_PROPERTY_VALUE_LENGTH:
            return f"property_value_too_large:{normalized}"
        if isinstance(value, str) and SENSITIVE_VALUE_RE.search(value):
            return f"sensitive_property_value:{normalized}"
    return None
