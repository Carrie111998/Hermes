"""Public-safe SkyVision tools for a customer-facing SkyAI Hermes runtime."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
MAX_TEXT_FIELD_LENGTH = 900
MAX_DETAIL_LIST_ITEMS = 8
MAX_CONFIGURATOR_OPTIONS = 10
PUBLIC_SITE_BASE_URL = "https://skyvision.bg"

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

SKYAI_CAMPAIGN_KNOWLEDGE_SCHEMA = {
    "name": "skyai_campaign_knowledge",
    "description": (
        "Return curated public SkyVision campaign and brand guidance for customer conversations. "
        "Use when the customer asks about bonuses, active campaigns, the free panoramic flight, "
        "or when a light sales note about an active campaign can help a purchase decision. "
        "Do not use it as a keyword router and do not force campaign text into unrelated support answers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Short description of the customer context or question.",
            },
            "include_terms": {
                "type": "boolean",
                "description": "Whether the customer explicitly needs campaign terms or eligibility details.",
            },
        },
    },
}

SKYAI_SUPPORT_KNOWLEDGE_SCHEMA = {
    "name": "skyai_support_knowledge",
    "description": (
        "Return curated public SkyVision commerce/support facts for customer conversations: "
        "gift voucher blanks and packaging, Speedy delivery, checkout payment methods, official contacts, "
        "voucher extension flow, and using/combining voucher value. Use as evidence, not as a keyword router."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Short description of the customer question or support context.",
            },
            "include_contacts": {
                "type": "boolean",
                "description": "Whether to include official SkyVision contact details in the answer.",
            },
        },
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


def _money_bgn_to_eur(value: float | int | str | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        decimal = Decimal(str(value)) / BGN_PER_EUR
    except Exception:
        return None
    return str(decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _money_decimal_string(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return str(value)


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
    if not start_date and not end_date:
        today = date.today()
        start_date = today.isoformat()
        end_date = (today + timedelta(days=14)).isoformat()
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
    fixed_count = len(fixed) if isinstance(fixed, list) else 0
    request_count = len(request_slots) if isinstance(request_slots, list) else 0
    working_count = len(working_periods) if isinstance(working_periods, list) else 0
    if fixed_count:
        availability_mode = "fixed_slots_available_direct_booking"
        visible_request_slots: list[dict[str, Any]] = []
    elif request_count:
        availability_mode = "request_slots_possible_no_fixed_slots_in_range"
        visible_request_slots = _compact_request_slots(request_slots, 12)
    elif working_count:
        availability_mode = "working_periods_only"
        visible_request_slots = []
    else:
        availability_mode = "no_public_slots_in_range"
        visible_request_slots = []
    return {
        "status": "ok",
        "source": "skyvision_public_events",
        "product_id": int(product_id),
        "range": {"start_date": start_date, "end_date": end_date},
        "availability_mode": availability_mode,
        "guidance": (
            "Фиксираните слотове са за директна резервация. Запитванията са ориентир само "
            "когато няма фиксирани слотове за периода; не смесвай двата режима в отговора."
        ),
        "fixed_slots_count": fixed_count,
        "request_slots_count": request_count,
        "working_periods_count": working_count,
        "fixed_slots": _compact_fixed_slots(fixed, 12),
        "request_slots": visible_request_slots,
        "working_periods": _first_items(working_periods, 6),
    }


def handle_skyai_campaign_knowledge(
    topic: str = "",
    include_terms: bool = False,
) -> dict[str, Any]:
    """Return curated public campaign facts without exposing internal state."""
    return {
        "status": "ok",
        "source": "skyvision_curated_public_campaign_knowledge",
        "topic": _truncate_text(topic, 300),
        "active_campaigns": [
            {
                "name": "Подарък панорамен полет над морето",
                "public_url": "https://skyvision.bg/campaign/free-panoramic-flight/",
                "terms_url": "https://panel.skyvision.bg/kampaniya-bezplaten-polet-nad-moreto",
                "customer_summary": (
                    "SkyVision благодари на клиентите с безплатен панорамен полет над морето "
                    "към покупка или директна BookNow резервация, според условията на кампанията."
                ),
                "sales_tone": (
                    "Поднасяй бонуса като приятен SkyVision жест, не като суха правна бележка. "
                    "Не го повтаряй във всеки отговор и не го вкарвай, ако клиентът пита за чист support казус."
                ),
                "booknow_nuance": (
                    "При BookNow бонусният полет се ползва след основното преживяване, защото BookNow "
                    "е конкретна резервация със защита за клиента и възможно възстановяване на пари, "
                    "ако изпълнителят не може да проведе резервацията."
                ),
                "voucher_nuance": (
                    "При ваучер сделката е за период на валидност, не за конкретен слот; ако дата отпадне, "
                    "клиентът може да резервира друга дата в рамките на валидността."
                ),
                "bonus_product": {
                    "name": "Панорамен полет над морето",
                    "product_id": 95435,
                    "availability_tool": "skyai_product_slots",
                    "availability_guidance": (
                        "Това е скритият бонус продукт за кампанията. Ако клиентът пита за свободни "
                        "часове или резервация на подаръчния полет, използвай skyai_product_slots "
                        "с product_id=95435 и обясни, че реалната резервация се прави през профила."
                    ),
                },
            }
        ],
        "founder_transfer_guidance": {
            "use_only_when_customer_asks_to_transfer_bonus_flight": True,
            "summary": (
                "Емил Ломлиев и Малина основават SkyVision през 2007, за да споделят страстта си към "
                "летенето. При казуси за преотстъпване на бонусния полет SkyAI може да обясни, че Емил "
                "лично разглежда такива случаи и досега SkyVision не е отказвал, когато клиентът иска "
                "жестът да зарадва друг човек."
            ),
            "public_founder_contact": "+359 886 417 142",
        },
        "terms": {
            "include_terms_requested": bool(include_terms),
            "terms_url": "https://panel.skyvision.bg/kampaniya-bezplaten-polet-nad-moreto",
            "general_terms_url": "https://skyvision.bg/общи-условия/",
            "privacy_notice_url": "https://skyvision.bg/уведомление-за-обработване-на-лични-д/",
        },
    }


def handle_skyai_support_knowledge(
    topic: str = "",
    include_contacts: bool = False,
) -> dict[str, Any]:
    """Return curated public commerce/support facts without exposing internal state."""
    contacts = {
        "contacts_page": "https://skyvision.bg/контакти/",
        "phones": ["+359 (0) 700 20 200", "+359 (0) 2 425 9795"],
        "email": "info@skyvision.bg",
        "client_working_hours": "Понеделник - Петък, 09:00-17:00",
        "closed": "Събота, неделя и официални празници",
        "tone_guidance": "Когато насочваш към екипа, дай точните контакти и кажи човешки, че ще се радваме да помогнем.",
    }
    return {
        "status": "ok",
        "source": "skyvision_curated_public_support_knowledge",
        "topic": _truncate_text(topic, 300),
        "gift_voucher_presentation": {
            "voucher_blanks": [
                "Класик",
                "Романс",
                "Честитка",
                "Вдъхновение",
                "Адреналин",
                "Vibe",
            ],
            "wish_flow": [
                "При покупка на ваучер клиентът избира бланка и може да попълни поле „Поздрав“.",
                "Полето „Поздрав“ е личното пожелание и се показва веднага в интерактивния preview на ваучера.",
                "Полето „Име на ползвател“ е отделно от пожеланието.",
                "Ако пожеланието трябва да се коригира след поръчка, това може да стане от панела или през екипа на SkyVision, докато ваучерът още не е подготвен/изпратен.",
            ],
            "packaging_options": [
                {
                    "name": "Безплатна опаковка",
                    "price_eur": "0.00",
                    "price_bgn": "0.00",
                    "note": "универсална физическа опаковка",
                },
                {
                    "name": "Син плик Лукс",
                    "price_eur": "2.00",
                    "price_bgn": "3.91",
                    "note": "по-официален и премиум вид",
                },
                {
                    "name": "Плик с кауза „Пингвин“",
                    "price_eur": "5.00",
                    "price_bgn": "9.78",
                    "note": "физически плик с кауза",
                },
                {
                    "name": "Електронен ваучер",
                    "price_eur": "0.00",
                    "price_bgn": "0.00",
                    "note": "най-бързият вариант; не е физическа опаковка",
                },
            ],
            "answer_guidance": (
                "Ако клиентът пита за официален подарък, бланка или пожелание, обясни ясно разликата "
                "между бланка, поздрав, име на ползвател, физическа опаковка и електронен ваучер."
            ),
        },
        "delivery": {
            "courier": "Speedy",
            "current_fee": "безплатна доставка",
            "office_locator_url": "https://www.speedy.bg/bg/speedy-offices",
            "office_or_locker_flow": [
                "При доставка до офис или автомат на Speedy клиентът трябва да избере населеното място.",
                "После трябва да маркира опцията за доставка до офис/автомат на Speedy.",
                "След това избира конкретния офис или автомат от падащото меню.",
                "Не е добре просто да се изпише адресът на офис на Speedy като обикновен адрес, защото пратката може да не бъде разпозната като офис/автомат и да се забави.",
            ],
            "working_hours_guidance": (
                "Работното време зависи от конкретния офис/автомат и трябва да се провери в Speedy локатора."
            ),
        },
        "payment_methods": {
            "online_checkout_options": ["Карта", "EasyPay", "Наложен платеж"],
            "bank_transfer": {
                "available_in_online_checkout": False,
                "answer_only_if_asked": True,
                "guidance": "Ако клиентът пита за банков превод, кажи кратко, че не е онлайн checkout опция.",
            },
            "guidance": (
                "Не изброявай липсващи методи без причина. Ако клиентът пита как може да плати, "
                "кажи наличните checkout опции ясно и кратко."
            ),
        },
        "vouchers": {
            "extension_flow": [
                "Клиентът влиза в профила си в SkyVision.",
                "Отваря „Моят ваучер“/„Ваучери“ и добавя ваучера, ако още не е добавен.",
                "Отваря конкретния ваучер и използва наличната опция за удължаване, ако системата я показва.",
                "Ако опцията не се вижда, има проблем или ваучерът е в особен статус, насочи към официалните контакти с номер на ваучера/поръчката.",
            ],
            "combine_or_use_multiple_vouchers_flow": [
                "Клиентът добавя двата ваучера в профила си от „Ваучери“.",
                "Стойността им се използва като ваучерна стойност/депозит в SkyVision профила.",
                "След това избира преживяване, минава през „Резервирай/BookNow“ и избира „Имам ваучер“.",
                "Ако целта е два ваучера да се използват за една конкретна резервация и интерфейсът не позволява това, насочи към екипа на SkyVision с номер на ваучер/поръчка.",
            ],
            "privacy_guidance": "Не искай кодове на ваучери в публичния чат; за съдействие насочи към официалните контакти.",
        },
        "official_contacts": contacts if include_contacts else {"available_if_needed": True},
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
    slug = str(item.get("slug") or "").strip("/")
    price_bgn = item.get("price") or item.get("price_bgn") or item.get("priceBgn")
    price_eur = item.get("price_eur") or item.get("priceEur") or _money_bgn_to_eur(price_bgn)
    return {
        "id": item.get("id") or item.get("product_id"),
        "title": item.get("title") or item.get("name"),
        "slug": slug or None,
        "category_slug": item.get("category_slug") or item.get("categorySlug"),
        "public_url": item.get("url") or _public_product_url(slug),
        "location": item.get("location") or item.get("locationName") or item.get("city") or item.get("region"),
        "location_area": item.get("locationArea"),
        "price_bgn": _money_decimal_string(price_bgn),
        "price_eur": _money_decimal_string(price_eur),
        "duration": item.get("duration"),
        "participants": item.get("participants") or item.get("participant_count"),
        "provider": _provider_name(item.get("provider")),
        "image": _first_image(item),
    }


def _sanitize_product_detail(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"raw_type": type(payload).__name__}
    source = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    slug = str(source.get("slug") or "").strip("/")
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    price_bgn = source.get("price") or source.get("price_bgn") or source.get("priceBgn")
    price_eur = source.get("price_eur") or source.get("priceEur") or _money_bgn_to_eur(price_bgn)
    return {
        "id": source.get("id") or source.get("product_id"),
        "title": source.get("title") or source.get("name"),
        "slug": slug or None,
        "public_url": metadata.get("canonical") or source.get("url") or _public_product_url(slug),
        "location": source.get("location") or source.get("locationName") or source.get("city"),
        "location_area": source.get("locationArea") or source.get("region"),
        "price_bgn": _money_decimal_string(price_bgn),
        "price_eur": _money_decimal_string(price_eur),
        "duration": source.get("duration"),
        "minimum_age": source.get("minimumAge") or source.get("min_age"),
        "maximum_weight": source.get("maximumWeight") or source.get("maxWeight"),
        "for_kids": source.get("forKids") or source.get("children") or source.get("isForChildren"),
        "weather": source.get("weather"),
        "service_for_who": source.get("serviceForWho"),
        "schedule": _truncate_text(source.get("schedule")),
        "cancellation_policy": source.get("cancellationPolicy"),
        "can_book": _boolish(source.get("canBook")),
        "can_buy_voucher": _boolish(source.get("canBuyVoucher")),
        "includes_bonus": _boolish(source.get("includesBonus") or source.get("canReceiveBonusProduct")),
        "provider": _provider_name(source.get("provider")),
        "description": _truncate_text(source.get("description") or source.get("aboutDescription")),
        "included": _compact_text_list(source.get("included")),
        "needed": _compact_text_list(source.get("needed")),
        "important": _truncate_text(source.get("important")),
        "restrictions": _truncate_text(source.get("otherRestrictions")),
        "locations": _compact_locations(source.get("locations")),
        "configurator": _compact_configurator(source.get("configurator")),
        "images": _compact_gallery(source.get("gallery") or source.get("images")),
    }


def _first_items(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[:limit]


def _compact_fixed_slots(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        slots = item.get("slots") if isinstance(item.get("slots"), list) else []
        free_slots = [slot for slot in slots if isinstance(slot, dict) and slot.get("status") == "free"]
        compact.append(
            {
                "event_id": item.get("id"),
                "start": item.get("start"),
                "end": item.get("end"),
                "free_slots_count": len(free_slots),
                "first_free_slot": {
                    "id": free_slots[0].get("id"),
                    "start": free_slots[0].get("start"),
                    "end": free_slots[0].get("end"),
                }
                if free_slots
                else None,
            }
        )
    return compact


def _compact_request_slots(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            compact.append({"start": item.get("start"), "end": item.get("end")})
    return compact


def _public_product_url(slug: str) -> str | None:
    slug = (slug or "").strip("/")
    if not slug:
        return None
    if slug.startswith("подарък/"):
        slug = slug[len("подарък/") :]
    return f"{PUBLIC_SITE_BASE_URL}/подарък/{slug}/"


def _provider_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name") or value.get("title")
    if isinstance(value, str):
        return value
    return None


def _first_image(item: dict[str, Any]) -> str | None:
    for key in ("image", "thumbnail", "cover"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    gallery = item.get("gallery") or item.get("images")
    if isinstance(gallery, list) and gallery:
        first = gallery[0]
        if isinstance(first, dict):
            return first.get("src") or first.get("url")
        if isinstance(first, str):
            return first
    return None


def _compact_gallery(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    gallery: list[dict[str, str]] = []
    for item in value[:4]:
        if isinstance(item, dict):
            src = item.get("src") or item.get("url")
            if src:
                gallery.append({"src": str(src), "alt": str(item.get("alt") or "")[:160]})
        elif isinstance(item, str):
            gallery.append({"src": item, "alt": ""})
    return gallery


def _compact_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_truncate_text(item, 260) for item in value[:MAX_DETAIL_LIST_ITEMS] if item]


def _compact_locations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    locations: list[dict[str, Any]] = []
    for item in value[:MAX_DETAIL_LIST_ITEMS]:
        if not isinstance(item, dict):
            continue
        coordinates = item.get("coordinates") if isinstance(item.get("coordinates"), dict) else {}
        locations.append(
            {
                "name": item.get("name"),
                "lat": coordinates.get("lat"),
                "lng": coordinates.get("lng"),
            }
        )
    return locations


def _compact_configurator(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    additions = value.get("additions") if isinstance(value.get("additions"), list) else []
    options: list[dict[str, Any]] = []
    for addition in additions:
        if not isinstance(addition, dict):
            continue
        for option in addition.get("options") or []:
            if not isinstance(option, dict):
                continue
            price_bgn = option.get("price")
            options.append(
                {
                    "label": _truncate_text(option.get("label") or option.get("labelVoucher"), 220),
                    "price_bgn": _money_decimal_string(price_bgn),
                    "price_eur": _money_bgn_to_eur(price_bgn),
                }
            )
            if len(options) >= MAX_CONFIGURATOR_OPTIONS:
                break
        if len(options) >= MAX_CONFIGURATOR_OPTIONS:
            break
    return {
        "name": value.get("name"),
        "voucher_name": value.get("nameVoucher"),
        "validity": value.get("validity"),
        "base_price_bgn": _money_decimal_string(value.get("price")),
        "base_price_eur": _money_bgn_to_eur(value.get("price")),
        "options": options,
    }


def _truncate_text(value: Any, limit: int = MAX_TEXT_FIELD_LENGTH) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _boolish(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "да"}
    return bool(value)


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
