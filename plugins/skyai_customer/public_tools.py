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
MAX_RETURN_ITEMS = 12
MAX_EVENT_PROPERTY_VALUE_LENGTH = 500
MAX_TEXT_FIELD_LENGTH = 900
MAX_DETAIL_LIST_ITEMS = 8
MAX_CONFIGURATOR_OPTIONS = 10
PUBLIC_SITE_BASE_URL = "https://skyvision.bg"
_MISSING = object()

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
EVENT_PROPERTY_SCHEMA: dict[str, dict[str, Any]] = {
    "product_id": {"type": "integer", "minimum": 1},
    "product_slug": {"type": "string", "maxLength": 300},
    "product_url": {"type": "string", "maxLength": 500},
    "surface": {"type": "string", "maxLength": 120},
    "position": {"type": "integer", "minimum": 0},
    "quantity": {"type": "integer", "minimum": 1},
    "value_eur": {"type": "number", "minimum": 0},
    "currency": {"type": "string", "enum": ["EUR"]},
    "campaign_id": {"type": "string", "maxLength": 120},
    "experiment_id": {"type": "string", "maxLength": 120},
    "variant_id": {"type": "string", "maxLength": 120},
    "reason_code": {"type": "string", "maxLength": 120},
    "feedback_code": {"type": "string", "maxLength": 120},
    "result_code": {"type": "string", "maxLength": 120},
    "success": {"type": "boolean"},
}
REGISTERED_EVENT_SECRET_ENV_NAMES = (
    "SKYAI_V2_CANARY_TOKEN",
    "SKYAI_DISCORD_BOT_TOKEN",
    "SKYAI_DISCORD_MIRROR_DATABASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_CODEX_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
)


SKYAI_CATALOG_SEARCH_SCHEMA = {
    "name": "skyai_catalog_search",
    "description": (
        "Query SkyVision's public catalog cache with the model-authored query and explicit "
        "EUR price filters. The tool converts explicit prices to the cache's BGN protocol, "
        "preserves backend order, and returns public product facts without interpreting or "
        "reranking the query."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "maxLength": MAX_TEXT_FIELD_LENGTH,
                "description": "Exact search query to send to the public catalog API.",
            },
            "min_price_eur": {
                "type": "number",
                "minimum": 0,
                "description": "Optional explicit lower budget in EUR.",
            },
            "max_price_eur": {
                "type": "number",
                "minimum": 0,
                "description": "Optional explicit upper budget in EUR.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RETURN_ITEMS,
                "default": 8,
                "description": "Maximum returned products.",
            },
        },
        "additionalProperties": False,
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
        "additionalProperties": False,
    },
}

SKYAI_PRODUCT_SLOTS_SCHEMA = {
    "name": "skyai_product_slots",
    "description": (
        "Fetch public fixed slots, working periods, and request slots for one SkyVision product id. "
        "Use only for public availability facts; do not create reservations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "product_id": {"type": "integer", "description": "SkyVision product id."},
            "start_date": {"type": "string", "description": "Optional YYYY-MM-DD start."},
            "end_date": {"type": "string", "description": "Optional YYYY-MM-DD end."},
        },
        "required": ["product_id"],
        "additionalProperties": False,
    },
}

SKYAI_CAMPAIGN_KNOWLEDGE_SCHEMA = {
    "name": "skyai_campaign_knowledge",
    "description": (
        "Return curated public SkyVision campaign and brand facts for customer conversations. "
        "Use when the customer asks about bonuses, active campaigns, the free panoramic flight, "
        "or when active public campaign facts are relevant to a purchase decision. "
        "Treat the result as evidence; Hermes decides whether and how it belongs in the answer."
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
        "additionalProperties": False,
    },
}

SKYAI_SUPPORT_KNOWLEDGE_SCHEMA = {
    "name": "skyai_support_knowledge",
    "description": (
        "Return curated public SkyVision commerce/support facts for customer conversations: "
        "gift voucher blanks and packaging, Speedy delivery, checkout payment methods, official contacts, "
        "voucher extension flow, using/combining voucher value, and customer-safe email support learning. "
        "Treat the result as evidence."
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
        "additionalProperties": False,
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
            "anonymous_id": {
                "type": "string",
                "maxLength": MAX_EVENT_PROPERTY_VALUE_LENGTH,
                "description": "Opaque anonymous id, if already safe.",
            },
            "conversation_id": {
                "type": "string",
                "maxLength": MAX_EVENT_PROPERTY_VALUE_LENGTH,
                "description": "Opaque conversation id.",
            },
            "properties": {
                "type": "object",
                "properties": EVENT_PROPERTY_SCHEMA,
                "additionalProperties": False,
                "description": "Exact structured analytics metadata only; no free-form chat content.",
            },
        },
        "required": ["event_type"],
        "additionalProperties": False,
    },
}

SKYAI_VOICE_TRANSFER_TO_HUMAN_SCHEMA = {
    "name": "skyai_voice_transfer_to_human",
    "description": (
        "Voice-only structured action tool. Use when Hermes independently decides that a "
        "phone call should be handed to a human SkyVision teammate. This tool does not "
        "perform PBX/SIP actions; it returns a canonical voice action request for the "
        "voice gateway. Do not use it for normal chat answers or questions SkyAI can answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "maxLength": 120,
                "description": "Short customer-safe reason for the handoff decision.",
            },
            "spoken_reply": {
                "type": "string",
                "maxLength": 220,
                "description": (
                    "Short Bulgarian phrase to say before transfer. Keep it natural for TTS; "
                    "do not include phone numbers, email addresses, URLs, or markdown."
                ),
            },
        },
        "required": ["reason", "spoken_reply"],
        "additionalProperties": False,
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


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _first_present(
    mapping: dict[str, Any],
    *keys: str,
    default: Any = None,
) -> Any:
    """Return the first structurally present field without truthiness repair."""

    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _rating_value(item: dict[str, Any]) -> str | None:
    value = _first_present(
        item,
        "rating",
        "averageRating",
        "avgRating",
        "ratingValue",
    )
    if value is None or value == "":
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return str(value)


def _exact_optional_bool_field(
    item: dict[str, Any],
    *keys: str,
) -> bool | None:
    value = _first_present(item, *keys, default=_MISSING)
    if value is _MISSING or value is None:
        return None
    if type(value) is not bool:
        rendered = "/".join(keys)
        raise ValueError(f"{rendered} must be an exact boolean")
    return value


def _exact_optional_public_php_bool_field(
    item: dict[str, Any],
    *keys: str,
) -> bool | None:
    value = _first_present(item, *keys, default=_MISSING)
    if value is _MISSING or value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    rendered = "/".join(keys)
    raise ValueError(f"{rendered} must be an exact boolean")


def _safe_limit(limit: int | None) -> int:
    if limit is None:
        return 8
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit_must_be_an_integer")
    if not 1 <= limit <= MAX_RETURN_ITEMS:
        raise ValueError("limit_out_of_range")
    return limit


def _validate_catalog_price(name: str, value: float | int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{name}_must_be_a_number"
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        return f"{name}_must_be_finite"
    if decimal < 0:
        return f"{name}_must_be_non_negative"
    return None


def handle_skyai_catalog_search(
    query: str = "",
    min_price_eur: float | None = None,
    max_price_eur: float | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    if not isinstance(query, str):
        return {"status": "error", "error": "query_must_be_a_string"}
    if len(query) > MAX_TEXT_FIELD_LENGTH:
        return {"status": "error", "error": "query_too_long"}
    for name, value in (
        ("min_price_eur", min_price_eur),
        ("max_price_eur", max_price_eur),
    ):
        validation_error = _validate_catalog_price(name, value)
        if validation_error:
            return {"status": "error", "error": validation_error}
    if (
        min_price_eur is not None
        and max_price_eur is not None
        and Decimal(str(min_price_eur)) > Decimal(str(max_price_eur))
    ):
        return {"status": "error", "error": "min_price_eur_exceeds_max_price_eur"}
    try:
        safe_limit = _safe_limit(limit)
    except (TypeError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}

    min_price_bgn = _money_eur_to_bgn(min_price_eur)
    max_price_bgn = _money_eur_to_bgn(max_price_eur) if max_price_eur is not None else 4000
    url = (
        f"{PUBLIC_CATALOG_BASE_URL}/products"
        f"?page=1&size={safe_limit}&sort=&minPrice={min_price_bgn}&maxPrice={max_price_bgn}"
        f"&search={quote(query, safe='')}"
    )
    items = _extract_products(_http_json(url))[:safe_limit]
    return {
        "status": "ok",
        "source": "skyvision_public_cache",
        "query": query,
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
    try:
        payload = _http_json(url)
    except Exception:
        return {
            "status": "error",
            "source": "skyvision_public_cache",
            "error": "product_detail_fetch_failed",
            "product_path": normalized_path,
            "detail": {
                "cancellation_policy": None,
                "cancellation": _structured_cancellation_policy(None, fetch_failed=True),
            },
        }
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
    if type(product_id) is not int or product_id <= 0:
        raise ValueError("product_id must be a positive integer")
    if start_date is None and end_date is None:
        today = date.today()
        start_date = today.isoformat()
        end_date = (today + timedelta(days=14)).isoformat()
    elif start_date is None or end_date is None:
        raise ValueError("start_date and end_date must be provided together")
    if type(start_date) is not str or not start_date:
        raise ValueError("start_date must be a nonempty string")
    if type(end_date) is not str or not end_date:
        raise ValueError("end_date must be a nonempty string")
    _validate_iso_date(start_date)
    _validate_iso_date(end_date)
    query = f"?startDate={quote(start_date)}&endDate={quote(end_date)}"
    url = f"{PUBLIC_EVENTS_BASE_URL}/{product_id}{query}"
    payload = _http_json(url)
    if type(payload) is not dict:
        raise ValueError("public events response must be an object")
    fixed = _exact_list_field(payload, "fixedSlots")
    request_slots = _exact_list_field(payload, "requestSlots")
    working_periods = _exact_list_field(payload, "workingPeriods")
    return {
        "status": "ok",
        "source": "skyvision_public_events",
        "product_id": product_id,
        "range": {"start_date": start_date, "end_date": end_date},
        "fixed_slots_count": len(fixed),
        "request_slots_count": len(request_slots),
        "working_periods_count": len(working_periods),
        "fixed_slots": _compact_fixed_slots(fixed, 12),
        "request_slots": _compact_request_slots(request_slots, 12),
        "working_periods": _first_items(working_periods, 6),
    }


def handle_skyai_campaign_knowledge(
    topic: str = "",
    include_terms: bool = False,
) -> dict[str, Any]:
    """Return curated public campaign facts without exposing internal state."""
    if type(topic) is not str:
        return {"status": "error", "error": "topic_must_be_a_string"}
    if len(topic) > 300:
        return {"status": "error", "error": "topic_too_long"}
    if type(include_terms) is not bool:
        return {"status": "error", "error": "include_terms_must_be_a_boolean"}
    return {
        "status": "ok",
        "source": "skyvision_curated_public_campaign_knowledge",
        "topic": topic,
        "tool_contract": {
            "purpose": "public_facts_only",
            "reasoning_owner": "hermes",
            "notes": (
                "Този tool не връща готови customer-visible реплики, keyword правила или flow за копиране. "
                "Върнатите данни са evidence pack; Hermes сам решава как да ги използва в разговора."
            ),
        },
        "active_campaigns": [
            {
                "name": "Подарък панорамен полет над морето",
                "public_url": "https://skyvision.bg/campaign/free-panoramic-flight/",
                "terms_url": "https://panel.skyvision.bg/kampaniya-bezplaten-polet-nad-moreto",
                "customer_summary": (
                    "SkyVision благодари на човека, който купува или резервира, с безплатен панорамен полет "
                    "над морето към покупка или директна BookNow резервация."
                ),
                "brand_story_facts": [
                    "SkyVision е създаден през 2007 от Емил и Малина.",
                    "Основателите споделят страстта си към летенето с клиенти и приятели на SkyVision.",
                    "SkyVision вече предлага над 1000 преживявания в много категории.",
                    "Летенето остава част от ДНК-то на бранда.",
                    "Емил и Малина все още лично летят и изпълняват част от полетите.",
                    "Бонусният полет е начин SkyVision да благодари на хората, които избират платформата.",
                ],
                "bonus_owner": {
                    "default": "човекът, който прави успешната поръчка или BookNow резервация",
                    "is_automatic_for_voucher_recipient": False,
                    "transfer_is_manual_exception": True,
                    "customer_can_self_transfer": False,
                    "default_use_scope": "свързан е с акаунта, имейла и данните на купувача/резервиращия",
                    "manual_exception_approver": "Емил Ломлиев",
                },
                "campaign_2026_facts": {
                    "public_page": "https://skyvision.bg/campaign/free-panoramic-flight/",
                    "bonus_product_url": (
                        "https://skyvision.bg/подарък/полет-с-жирокоптер/панорамен-полет-над-морето/"
                    ),
                    "archive_2025_url": "https://skyvision.bg/campaign/free-panoramic-flight-2025/",
                    "period": "от 1 април 2026 г. до изчерпване на капацитета",
                    "capacity": "500 полета през активния период",
                    "validity": "12 месеца от датата на покупката",
                    "how_customer_gets_it": "появява се автоматично в секция „Ваучери“ в профила",
                    "gift_entitlement_profile_linking": {
                        "has_voucher_or_serial_number": False,
                        "manual_add_by_customer": False,
                        "customer_can_self_transfer": False,
                        "logged_in_order": "ако клиентът е бил логнат при поръчката, подаръкът се добавя автоматично в профила",
                        "guest_or_no_profile_order": "ако клиентът не е бил логнат или няма профил, подаръкът се обвързва с имейла от поръчката",
                        "later_profile_with_same_email": (
                            "когато после се създаде профил със същия имейл, подаръкът трябва да се появи "
                            "автоматично в профила"
                        ),
                        "missing_entitlement_resolution": (
                            "Подаръчният бонус не се добавя ръчно с бутон „Добави ваучер“; при липсващ "
                            "подарък проверката е по имейла от поръчката и свързания профил."
                        ),
                    },
                    "how_to_book": "клиентът избира таймслот и резервира полета през системата",
                    "booknow_timing": "при BookNow подаръчният полет се резервира след изпълнение на основната услуга",
                    "not_lottery": "без томбола и без игра на късмета",
                    "main_service_location_independent": (
                        "Бонусът е към всяка допустима поръчка/резервация от сайта, независимо къде се "
                        "изпълнява основната закупена услуга."
                    ),
                    "bonus_execution_location": (
                        "Подаръчният панорамен полет над морето е отделен бонус продукт и се изпълнява "
                        "единствено от летище Приморско."
                    ),
                    "location_confusion_answer": (
                        "Ако клиентът пита защо основната услуга е в Созопол, Белоградчик, София или друга "
                        "локация, а бонусният полет е в Приморско, обясни спокойно, че това е нормално: "
                        "основната услуга и бонусният полет са различни преживявания; мястото на основната "
                        "услуга няма значение за мястото на изпълнение на бонусния полет."
                    ),
                },
                "booknow_nuance": (
                    "При BookNow бонусният полет се ползва след основното преживяване, защото BookNow "
                    "е конкретна резервация без предварително купуване на ваучер, със защита за клиента: "
                    "ако изпълнителят не може да проведе резервацията, парите ще бъдат възстановени."
                ),
                "voucher_nuance": (
                    "При ваучер сделката е за период на валидност, не за конкретен слот; ако дата отпадне, "
                    "клиентът може да резервира друга дата в рамките на валидността."
                ),
                "bonus_product": {
                    "name": "Панорамен полет над морето",
                    "product_id": 95435,
                    "public_url": (
                        "https://skyvision.bg/подарък/полет-с-жирокоптер/панорамен-полет-над-морето/"
                    ),
                    "price_eur": "0.00",
                    "duration": "10 мин.",
                    "participants": "1 човек",
                    "location": "Летище Приморско",
                    "location_note": (
                        "Тази локация не зависи от локацията на основната услуга в поръчката; "
                        "подаръчният полет над морето винаги се изпълнява от летище Приморско."
                    ),
                    "min_age": "16",
                    "max_weight": "100 kg",
                    "season": "от началото на юни до октомври, при благоприятни метеорологични условия",
                    "schedule": "8:00-19:30 ч. всеки ден от седмицата през активния сезон",
                    "includes": [
                        "опитен пилот-инструктор",
                        "необходимата летателна екипировка",
                        "инструктаж преди излитане и по време на полета",
                        "полет за един човек с жирокоптер MTO Sport с продължителност 10 мин.",
                    ],
                    "availability_tool": "skyai_product_slots",
                    "availability_facts": {
                        "catalog_visibility": "скрит бонус продукт за кампанията",
                        "slots_tool_product_id": 95435,
                        "reservation_channel": "реалната резервация се прави през профила на клиента",
                    },
                },
            }
        ],
        "founder_transfer_facts": {
            "context": "само когато клиентът пита дали бонусният полет може да се използва от друг човек",
            "facts": {
                "default_owner": "купувачът или човекът, който прави директната BookNow резервация",
                "default_rule": (
                    "по условия и по системна логика бонусният полет не се ползва от друг човек автоматично; "
                    "той е свързан с акаунта/имейла/данните на купувача или резервиращия"
                ),
                "recipient_transfer": "не е автоматично право; разглежда се като човешко изключение с лично одобрение",
                "founder_name": "Емил Ломлиев",
                "cofounder_name": "Малина Ломлиева",
                "founder_role": "съосновател на SkyVision, пилот-инструктор и изпитващ",
                "founding_story": (
                    "Емил и Малина основават SkyVision през 2007 г., за да споделят страстта си към летенето "
                    "с всеки, който иска да се докосне до небето"
                ),
                "platform_scale": "SkyVision вече предлага над 1000 преживявания, но летенето остава личната страст на основателите",
                "personal_flight_fact": "Емил и Малина все още лично летят и изпълняват част от полетите",
                "reason": "мисията на SkyVision е да споделя любовта към летенето и да радва хората",
                "recipient_transfer_approval": (
                    "използване на бонуса от друг човек е изключение с лично одобрение от Емил; "
                    "публичният му телефон е наличен в public_founder_contact"
                ),
            },
            "public_founder_contact": "+359 886 417 142",
        },
        "terms": {
            "include_terms_requested": include_terms,
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
    if type(topic) is not str:
        return {"status": "error", "error": "topic_must_be_a_string"}
    if len(topic) > 300:
        return {"status": "error", "error": "topic_too_long"}
    if type(include_contacts) is not bool:
        return {"status": "error", "error": "include_contacts_must_be_a_boolean"}
    contacts = {
        "contacts_page": "https://skyvision.bg/контакти/",
        "phones": ["+359 (0) 700 20 200", "+359 (0) 2 425 9795"],
        "email": "info@skyvision.bg",
        "client_working_hours": "Понеделник - Петък, 09:00-17:00",
        "closed": "Събота, неделя и официални празници",
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
                "На продукт бутонът „Купи ваучер“ добавя преживяването в кошницата и отваря създателя на ваучер.",
                "В създателя има полета „Име на ползвател“ и „Поздрав“; текстът в „Поздрав“ е личното пожелание и се показва веднага в preview-то на ваучера.",
                "„Редактирай поздрава“ отваря само layout настройки като размер на шрифта и височина на реда; не е необходим, за да се появи или обнови написаният поздрав.",
                "Преди плащане поздравът може да се редактира от „Кошница“ чрез молива до „Бланка“; бутонът „Запази“ в модала запазва промяната.",
                "„Резервирай“ / BookNow е директна резервация с плащане с карта, не издава подаръчен ваучер и няма персонализация на поздрав.",
            ],
            "packaging_options": [
                {
                    "name": "Безплатна опаковка",
                    "price_eur": "0.00",
                    "price_bgn": "0.00",
                    "note": "универсална подаръчна опаковка",
                },
                {
                    "name": "Син плик „Лукс“",
                    "price_eur": "2.00",
                    "price_bgn": "3.91",
                    "note": "класическият SkyVision син плик с червен восъчен печат; разпознаваем премиум вариант",
                },
                {
                    "name": "Плик с кауза „Пингвин“",
                    "price_eur": "5.00",
                    "price_bgn": "9.78",
                    "note": "подаръчен плик с кауза",
                },
                {
                    "name": "Електронен ваучер",
                    "price_eur": "0.00",
                    "price_bgn": "0.00",
                    "note": "най-бързият вариант, когато подаръкът трябва да се изпрати веднага онлайн",
                },
            ],
            "display_facts": {
                "voucher_blank": "визията/темата на самия ваучер",
                "greeting": "личното пожелание в поле „Поздрав“",
                "packaging": "плик/опаковка за хартиен ваучер или електронен ваучер за имейл",
                "price_display": "EUR е основната цена; BGN е вторична стойност.",
            },
        },
        "delivery": {
            "courier": "Speedy",
            "current_fee": "безплатна доставка",
            "office_locator_url": "https://www.speedy.bg/bg/speedy-offices-automats",
            "office_or_locker_steps": [
                "При доставка до офис или автомат на Speedy клиентът трябва да избере населеното място.",
                "После трябва да маркира опцията за доставка до офис/автомат на Speedy.",
                "След това избира конкретния офис или автомат от падащото меню.",
                "Ако адресът на офис на Speedy се въведе като обикновен адрес, пратката може да не бъде разпозната като офис/автомат и да се забави.",
            ],
            "dispatch_cutoff": (
                "Физически ваучери, поръчани в работен ден до 15:00, обичайно се обработват и предават "
                "на куриер същия ден; след 15:00 или през уикенд/празник - на първия следващ работен ден."
            ),
            "speedy_working_hours_fact": "Работното време зависи от конкретния офис/автомат и се проверява в Speedy локатора.",
        },
        "payment_methods": {
            "online_checkout_options": ["Карта", "EasyPay", "Наложен платеж"],
            "cash_on_delivery": {
                "available_for": "печатен/хартиен ваучер с доставка",
                "not_for": "електронен ваучер и директна BookNow резервация",
            },
            "bank_transfer": {
                "available_in_online_checkout": False,
                "online_checkout_label": None,
            },
        },
        "order_and_invoice_support": {
            "order_note_handling": (
                "Ако клиентът е оставил забележка към поръчката, екипът я преглежда и при нужда "
                "се свързва с клиента."
            ),
            "next_day_delivery_fact": (
                "За хартиен ваучер, поръчан в работен ден до 15:00, SkyVision обичайно го обработва "
                "и предава на Speedy същия ден. В потвърдителния имейл има и резервен линк за сваляне "
                "на ваучера, ако доставката се забави."
            ),
            "invoice_self_service": (
                "В потвърдителния имейл за поръчката има линк за фактура. Клиентът може да въведе "
                "данните си и да изтегли фактура до 5 дни от датата на поръчката."
            ),
            "beneficiary_name_note": (
                "Ако име на ползвател е написано в забележка вместо в полето на ваучера, екипът може "
                "да го коригира, ако ваучерът още не е подготвен или изпратен."
            ),
        },
        "reservation_support": {
            "customer_contact_email": "info@skyvision.bg",
            "automated_notification_address": {
                "email": "reservations@skyvision.bg",
                "role": "автоматични известия за резервации",
                "monitored": True,
                "accepts_customer_replies": False,
            },
            "customer_profile_section": "Профил -> Резервации",
            "self_service_cancel_action": "Анулиране на резервацията",
            "customer_cancel_endpoint_pattern": "POST reservation/cancel/<voucher>",
            "public_terms_url": "https://skyvision.bg/общи-условия/",
            "public_terms_sections": ["1.2", "4.1", "17.2", "17.4", "17.5"],
            "platform_reservation_management_available": True,
            "reservation_changes_through_platform": True,
            "provider_defined_change_conditions": True,
            "provider_defined_cutoff_before_slot": True,
            "provider_defined_fees_possible": True,
            "service_may_have_no_customer_cancellation": True,
            "platform_enforces_cancel_cutoff": True,
            "global_cancel_hours": None,
            "self_service_cancel_after_cutoff_available": False,
            "self_service_cancel_after_unavailable_action_available": False,
            "exception_review_guaranteed": False,
            "voucher_exchange_requires_released_voucher": True,
            "missing_confirmation": (
                "При липсващ потвърдителен имейл за резервация екипът може да провери и да помогне; "
                "SkyAI не твърди, че сам е изпратил или преиздал потвърждение."
            ),
            "expired_response_deadline": (
                "Ако срокът за отговор по запитване е изтекъл, това не е self-service действие. "
                "Екипът проверява запитването, клиента и изпълнителя, преди да удължи срок."
            ),
        },
        "vouchers": {
            "issuer_scope": {
                "skyvision_issued_vouchers": {
                    "profile_compatible": True,
                    "service_authority": "SkyVision",
                },
                "externally_issued_vouchers": {
                    "profile_compatible": False,
                    "service_authority": "платформата, търговецът или доставчикът, който е издал ваучера",
                },
                "catalog_overlap_changes_issuer_or_compatibility": False,
                "compatibility_is_issuer_scoped": True,
                "unspecified_origin_in_skyvision_chat": (
                    "Неуточнен ваучер в разговор със SkyVision обичайно означава ваучер, "
                    "издаден от SkyVision."
                ),
            },
            "customer_panel": {
                "scope": "Процесът в SkyVision профила е за ваучери, издадени от SkyVision.",
                "main_area": "Профил -> Ваучери / Моите ваучери",
                "left_navigation": ["Ваучери", "Резервации", "Запитвания", "Поръчки", "Настройки", "Изход"],
                "voucher_filters": [
                    "Всички",
                    "Неизползвани",
                    "Използвани",
                    "Изтекли",
                    "Резервирани",
                    "Чакащо запитване",
                ],
                "empty_state": "Ако няма добавени ваучери, панелът показва бутон „Добави ваучер“.",
                "add_voucher_flow": [
                    "За ваучер, издаден от SkyVision, клиентът отваря „Ваучери“ в профила си.",
                    "Натиска „Добави ваучер“.",
                    "В модала въвежда серийния номер на ваучера на латиница, както е изписан на гърба му.",
                    "Натиска „Добави ваучер“ и после управлява ваучера от списъка.",
                ],
                "addition_problem_facts": {
                    "serial_format": "Серийният номер се въвежда на латиница, както е изписан на ваучера.",
                    "possible_issuer_mismatch": True,
                    "other_possible_states": [
                        "ваучерът вече е добавен в друг профил",
                        "ваучерът е с особен статус",
                        "ваучерът е подарък/бонус без сериен номер за ръчно добавяне",
                    ],
                },
                "voucher_list_columns": ["Услуга", "Ваучер", "Депозит", "Статус", "Валидност", "Действия"],
                "common_actions": [
                    "„Използвай“ за активен ваучер, когато клиентът иска да избере/резервира услуга.",
                    "„Удължи валидност“ за ваучери, при които панелът показва тази опция.",
                    "Икона кошче за премахване от профила, когато действието е налично.",
                ],
                "customer_remains_actor": True,
                "chat_privacy_note": "SkyAI не добавя, не използва и не изтрива ваучери вместо клиента и не иска пълен код в публичния чат.",
            },
            "campaign_gifts": {
                "not_regular_vouchers": (
                    "Подаръците/бонусите към поръчка не са стандартни ваучери и нямат ваучерен/сериен номер "
                    "за ръчно добавяне."
                ),
                "manual_add_available": False,
                "profile_linking": {
                    "logged_in_order": "ако клиентът е бил логнат при поръчката, подаръкът се добавя автоматично в профила",
                    "guest_or_no_profile_order": "ако не е бил логнат или няма профил, подаръкът се обвързва с имейла от поръчката",
                    "later_profile_with_same_email": "при създаване на профил със същия имейл подаръкът трябва вече да е вътре автоматично",
                },
                "customer_next_step": (
                    "Ако клиентът не вижда подаръка, не го карай да въвежда сериен номер. "
                    "Първо ориентирай дали профилът е със същия имейл като поръчката; при разминаване или липса "
                    "на подаръка насочи към екипа за проверка."
                ),
            },
            "profile_extension_available": True,
            "extension_steps": [
                "Клиентът влиза в профила си в SkyVision.",
                "Отваря „Ваучери“ / „Моите ваучери“ и добавя ваучера от бутона „Добави ваучер“, ако още не е добавен.",
                "Отваря конкретния ваучер и използва опцията за удължаване.",
                "Ако има проблем, особен статус или клиентът не успява да завърши удължаването, екипът на SkyVision обработва казуса с номер на ваучера/поръчката.",
            ],
            "manual_extension_refresh": (
                "Ако екипът ръчно удължи ваучер или остатъчен ваучер, клиентът трябва да изтрие ваучера "
                "от профила си и да го добави отново с „Добави ваучер“, за да се обновят валидността "
                "и депозитът в панела."
            ),
            "ownership_conflict": (
                "Ако сайтът показва, че ваучерът е добавен от друг потребител, обичайно човекът, който "
                "го е добавил, трябва да го премахне от своя профил. Ако клиентът не знае кой е това, "
                "официалният екип може да помогне след проверка и потвърждение."
            ),
            "residual_voucher": {
                "automatic_issue": (
                    "Когато ваучер се използва за по-евтина услуга, системата автоматично издава нов "
                    "остатъчен ваучер с остатъчната стойност."
                ),
                "email_subject": "Издаване на остатък за ваучер от SkyVision",
                "customer_next_step": (
                    "Клиентът добавя новия остатъчен ваучер в профила си и го използва за следваща резервация."
                ),
                "missing_email": "Ако клиентът не намира имейла с остатъчния ваучер, екипът може да провери и помогне.",
            },
            "merge_two_vouchers_into_one": {
                "self_service_available": False,
                "handled_by": "екипа на SkyVision",
                "handling": "ръчна обработка",
                "customer_data_needed_by_support": "номер на ваучер/поръчка през официалните контактни канали",
                "chat_privacy_note": "Кодове на ваучери не се обработват в публичния чат.",
                "result": "нов резултатен ваучер с комбинирана стойност и допълнителен 1 месец валидност",
                "original_vouchers_after_merge": "маркират се като използвани/изпълнени и не се използват повторно",
                "different_holders": "ако ваучерите са на различни хора, екипът може да поиска съгласие",
            },
            "wrong_reservation_or_split_residual_repair": (
                "Ако клиентът е избрал грешна опция при резервация и сумата се е разделила между основен "
                "и остатъчен ваучер, първо се анулира грешната резервация през линка в имейла за резервация. "
                "След това екипът може да помогне да се възстанови стойността към основния ваучер; след такава "
                "ръчна корекция клиентът изтрива и добавя ваучера отново в профила."
            ),
            "refund_and_cancellation": (
                "Сторно/рефънд е support операция. Екипът проверява поръчка, фактура, плащане и дали "
                "кампанийният бонус вече е използван. Ако бонусният полет вече е използван, сторно може "
                "да бъде отказано."
            ),
            "privacy_policy": "Кодове на ваучери не се обработват в публичния чат; официалният екип работи с номер на ваучер/поръчка през контактните канали.",
        },
        "email_case_learning": {
            "source": "customer_safe_email_case_learning_2025-07-07_to_2026-07-07",
            "privacy": "Обобщено обучение от реални support казуси; без сурови имена, имейли, телефони, ваучерни кодове, order ids или вътрешна кореспонденция.",
            "scale": {
                "human_operational_case_records": 6979,
                "grouped_cases": 3099,
                "source_window": "2025-07-07..2026-07-07",
            },
            "frequent_customer_intents": [
                {
                    "intent": "voucher_help",
                    "customer_safe_scope": (
                        "активация в профил, регистрация, валидност, удължаване, липсващ PDF/имейл, "
                        "остатъчен ваучер, прехвърляне на използване и повторно изпращане на инструкции"
                    ),
                    "support_posture": (
                        "Разбери състоянието на ваучера и насочи клиента към точната self-service стъпка "
                        "или към екипа с минималния нужен идентификатор през официален канал."
                    ),
                },
                {
                    "intent": "reservation_status",
                    "customer_safe_scope": (
                        "потвърдена резервация, чакащо одобрение от изпълнител, предложена друга дата, "
                        "SMS/email потвърждение, резервация през „Моят ваучер“"
                    ),
                    "support_posture": (
                        "Мисли в състояния: чака потвърждение, потвърдено, отказано, предложена нова дата, "
                        "клиентът трябва да приеме, или е нужен екипът."
                    ),
                },
                {
                    "intent": "checkout_payment_problem",
                    "customer_safe_scope": (
                        "проблем с плащане, контактни данни, адрес/телефон, пожелание, бланка, доставка, "
                        "фактура и объркано поле при поръчка"
                    ),
                    "support_posture": "Първо помогни на клиента да продължи покупката; не го пращай към екипа, ако има ясна self-service стъпка.",
                },
                {
                    "intent": "courier_delivery_details",
                    "customer_safe_scope": "доставка със Speedy, офис/автомат, грешен телефон/адрес, закъсняваща пратка, PDF ваучер като резервен вариант",
                    "support_posture": "Дай практична стъпка за доставка и поддържай покупката спокойна; при нужда дай официалните контакти.",
                },
                {
                    "intent": "refund_cancellation_return",
                    "customer_safe_scope": "анулирана резервация/поръчка, невъзможност за присъствие, връщане, алтернативна дата",
                    "support_posture": "Не обещавай ръчно решение без проверка; обясни логиката и събери минимален контекст за екипа.",
                },
                {
                    "intent": "provider_unreachable",
                    "customer_safe_scope": "забавяне или неяснота от страна на изпълнител",
                    "support_posture": "Клиентът чува, че екипът проверява с изпълнителя и ще последва отговор; не разкривай вътрешни ескалации.",
                },
                {
                    "intent": "post_experience_media",
                    "customer_safe_scope": "липсващи снимки/видео след преживяване, очакване за получаване на медии",
                    "support_posture": "Обясни очакването и насочи към екипа, когато е нужна проверка с изпълнителя или медийния доставчик.",
                },
            ],
            "state_reasoning": [
                "Не свеждай ваучерен казус до „има/няма резервация“; мисли за статус, валидност, депозит, остатък, профил, собственик и следваща безопасна стъпка.",
                "При weather-sensitive услуги нормализирай, че дата може да се промени, и насочи към нова дата/наличност без драматизиране.",
                "При изтекъл или почти изтекъл ваучер не обещавай автоматично удължаване; насочи към профила, а при особен статус към екипа.",
                "Ако клиент пише към no-reply/system имейл или се е объркал къде да отговори, признай конкретния проблем и дай правилния официален канал.",
            ],
            "operator_handoff": {
                "minimum_safe_identifier": "номер на ваучер или поръчка през официалните контакти, когато е нужен екипът",
                "do_not_request_in_public_chat": ["пълен ваучерен код", "карта/плащане", "лични документи", "пароли"],
                "tone": "човешки, полезно и уверено; клиентът трябва да усеща, че SkyVision знае тези казуси от практика.",
            },
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
    if not isinstance(anonymous_id, str):
        return {"status": "error", "error": "anonymous_id_must_be_a_string"}
    if not isinstance(conversation_id, str):
        return {"status": "error", "error": "conversation_id_must_be_a_string"}
    if len(anonymous_id) > MAX_EVENT_PROPERTY_VALUE_LENGTH:
        return {"status": "error", "error": "anonymous_id_too_long"}
    if len(conversation_id) > MAX_EVENT_PROPERTY_VALUE_LENGTH:
        return {"status": "error", "error": "conversation_id_too_long"}
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        return {"status": "error", "error": "properties_must_be_an_object"}
    properties_error = _validate_event_properties(properties)
    if properties_error:
        return {"status": "error", "error": properties_error, "written": False}
    sanitized_properties = _redact_registered_event_secrets(properties)

    event = {
        "schema": "skyai_ci.events.local_jsonl.v2",
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "anonymous_id_hash": _hash_optional(anonymous_id),
        "conversation_id_hash": _hash_optional(conversation_id),
        "properties": sanitized_properties,
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


def handle_skyai_voice_transfer_to_human(
    reason: str,
    spoken_reply: str,
) -> dict[str, Any]:
    if not isinstance(reason, str):
        return {"status": "error", "error": "reason_must_be_a_string"}
    if not isinstance(spoken_reply, str):
        return {"status": "error", "error": "spoken_reply_must_be_a_string"}
    if len(reason) > 120:
        return {"status": "error", "error": "reason_exceeds_120_characters"}
    if len(spoken_reply) > 220:
        return {"status": "error", "error": "spoken_reply_exceeds_220_characters"}
    if not reason:
        return {"status": "error", "error": "reason_must_be_nonempty"}
    if not spoken_reply:
        return {"status": "error", "error": "spoken_reply_must_be_nonempty"}
    return {
        "status": "ok",
        "voice_action": "transfer_to_human",
        "transfer": {"target": "operator_queue", "reason": reason},
        "spoken_reply": spoken_reply,
        "display_reply": spoken_reply,
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
    raw_slug = _first_present(item, "slug", default="")
    if type(raw_slug) is not str:
        raise ValueError("slug must be a string")
    slug = raw_slug.strip("/")
    price_bgn = _first_present(item, "price", "price_bgn", "priceBgn")
    price_eur = _first_present(
        item,
        "price_eur",
        "priceEur",
        default=_MISSING,
    )
    if price_eur is _MISSING:
        price_eur = _money_bgn_to_eur(price_bgn)
    old_price_bgn = _first_present(
        item,
        "oldPrice",
        "old_price",
        "regularPrice",
    )
    old_price_eur = _first_present(
        item,
        "oldPriceEur",
        "old_price_eur",
        default=_MISSING,
    )
    if old_price_eur is _MISSING:
        old_price_eur = _money_bgn_to_eur(old_price_bgn)
    explicit_url = _first_present(item, "url", default=_MISSING)
    return {
        "id": _first_present(item, "id", "product_id"),
        "title": _first_present(item, "title", "name"),
        "slug": slug or None,
        "category_slug": _first_present(item, "category_slug", "categorySlug"),
        "public_url": (
            _public_product_url(slug)
            if explicit_url is _MISSING
            else explicit_url
        ),
        "location": _first_present(
            item,
            "location",
            "locationName",
            "city",
            "region",
        ),
        "location_area": item.get("locationArea"),
        "price_bgn": _money_decimal_string(price_bgn),
        "price_eur": _money_decimal_string(price_eur),
        "old_price_bgn": _money_decimal_string(old_price_bgn),
        "old_price_eur": _money_decimal_string(old_price_eur),
        "rating": _rating_value(item),
        "rating_count": _int_or_none(
            _first_present(item, "ratingCount", "reviewsCount")
        ),
        "orders_count": _int_or_none(item.get("ordersCount")),
        "is_on_offer": _exact_optional_bool_field(
            item,
            "isOnOffer",
            "is_on_offer",
            "hasDiscount",
        ),
        "duration": item.get("duration"),
        "participants": _first_present(
            item,
            "participants",
            "participant_count",
        ),
        "provider": _provider_name(item.get("provider")),
        "image": _first_image(item),
    }


def _normalize_search_text(value: Any) -> str:
    return str(value or "").casefold()


def _structured_cancellation_policy(policy: Any, *, fetch_failed: bool = False) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", str(policy or "")).strip()
    if fetch_failed:
        status = "unverified"
        hours = None
        normalized_policy = None
    elif not text:
        status = "unknown"
        hours = None
        normalized_policy = None
    else:
        normalized = _normalize_search_text(text)
        hours_match = re.search(r"до\s+(\d+)\s*час", normalized)
        hours = int(hours_match.group(1)) if hours_match else None
        if "няма" in normalized and "безплат" in normalized and "анулиран" in normalized:
            status = "no_free_cancellation"
            hours = None
        elif hours is not None and "безплат" in normalized and "анулиран" in normalized:
            status = "free_until_hours_before_slot"
        else:
            status = "specified"
        normalized_policy = text
    return {
        "source_field": "cancellationPolicy",
        "policy": normalized_policy,
        "status": status,
        "hours_before_slot": hours,
    }


def _sanitize_product_detail(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"raw_type": type(payload).__name__}
    source = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_slug = _first_present(source, "slug", default="")
    if type(raw_slug) is not str:
        raise ValueError("slug must be a string")
    slug = raw_slug.strip("/")
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    price_bgn = _first_present(source, "price", "price_bgn", "priceBgn")
    price_eur = _first_present(
        source,
        "price_eur",
        "priceEur",
        default=_MISSING,
    )
    if price_eur is _MISSING:
        price_eur = _money_bgn_to_eur(price_bgn)
    old_price_bgn = _first_present(
        source,
        "oldPrice",
        "old_price",
        "regularPrice",
    )
    old_price_eur = _first_present(
        source,
        "oldPriceEur",
        "old_price_eur",
        default=_MISSING,
    )
    if old_price_eur is _MISSING:
        old_price_eur = _money_bgn_to_eur(old_price_bgn)
    canonical = _first_present(metadata, "canonical", default=_MISSING)
    source_url = _first_present(source, "url", default=_MISSING)
    if canonical is not _MISSING:
        public_url = canonical
    elif source_url is not _MISSING:
        public_url = source_url
    else:
        public_url = _public_product_url(slug)
    return {
        "id": _first_present(source, "id", "product_id"),
        "title": _first_present(source, "title", "name"),
        "slug": slug or None,
        "category_slug": _first_present(
            source,
            "category_slug",
            "categorySlug",
            default=(slug.split("/", 1)[0] if "/" in slug else None),
        ),
        "public_url": public_url,
        "location": _first_present(
            source,
            "location",
            "locationName",
            "city",
        ),
        "location_area": _first_present(
            source,
            "locationArea",
            "region",
        ),
        "price_bgn": _money_decimal_string(price_bgn),
        "price_eur": _money_decimal_string(price_eur),
        "old_price_bgn": _money_decimal_string(old_price_bgn),
        "old_price_eur": _money_decimal_string(old_price_eur),
        "rating": _rating_value(source),
        "rating_count": _int_or_none(
            _first_present(source, "ratingCount", "reviewsCount")
        ),
        "orders_count": _int_or_none(source.get("ordersCount")),
        "is_on_offer": _exact_optional_bool_field(
            source,
            "isOnOffer",
            "is_on_offer",
            "hasDiscount",
        ),
        "duration": source.get("duration"),
        "minimum_age": _first_present(source, "minimumAge", "min_age"),
        "maximum_weight": _first_present(source, "maximumWeight", "maxWeight"),
        "for_kids": _first_present(
            source,
            "forKids",
            "children",
            "isForChildren",
        ),
        "weather": source.get("weather"),
        "service_for_who": source.get("serviceForWho"),
        "schedule": _truncate_text(source.get("schedule")),
        "cancellation_policy": _structured_cancellation_policy(source.get("cancellationPolicy"))["policy"],
        "cancellation": _structured_cancellation_policy(source.get("cancellationPolicy")),
        "can_book": _exact_optional_public_php_bool_field(source, "canBook"),
        "can_buy_voucher": _exact_optional_public_php_bool_field(
            source,
            "canBuyVoucher",
        ),
        "includes_bonus": _exact_optional_public_php_bool_field(
            source,
            "includesBonus",
            "canReceiveBonusProduct",
        ),
        "provider": _provider_name(source.get("provider")),
        "description": _truncate_text(
            _first_present(source, "description", "aboutDescription")
        ),
        "included": _compact_text_list(source.get("included")),
        "needed": _compact_text_list(source.get("needed")),
        "important": _truncate_text(source.get("important")),
        "restrictions": _truncate_text(source.get("otherRestrictions")),
        "locations": _compact_locations(source.get("locations")),
        "configurator": _compact_configurator(source.get("configurator")),
        "images": _compact_gallery(
            _first_present(source, "gallery", "images")
        ),
    }


def _exact_list_field(mapping: dict[str, Any], key: str) -> list[Any]:
    if key not in mapping or mapping[key] is None:
        return []
    value = mapping[key]
    if type(value) is not list:
        raise ValueError(f"{key} must be a list")
    return value


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
        result = _first_present(value, "name", "title")
        if result is not None and type(result) is not str:
            raise ValueError("provider name/title must be a string")
        return result
    if isinstance(value, str):
        return value
    return None


def _first_image(item: dict[str, Any]) -> str | None:
    for key in ("image", "thumbnail", "cover"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    gallery = _first_present(item, "gallery", "images")
    if isinstance(gallery, list) and gallery:
        first = gallery[0]
        if isinstance(first, dict):
            result = _first_present(first, "src", "url")
            if result is not None and type(result) is not str:
                raise ValueError("gallery src/url must be a string")
            return result
        if isinstance(first, str):
            return first
    return None


def _compact_gallery(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    gallery: list[dict[str, str]] = []
    for item in value[:4]:
        if isinstance(item, dict):
            src = _first_present(item, "src", "url")
            if src is None:
                continue
            if type(src) is not str:
                raise ValueError("gallery src/url must be a string")
            alt = item.get("alt", "")
            if type(alt) is not str:
                raise ValueError("gallery alt must be a string")
            gallery.append({"src": src, "alt": alt[:160]})
        elif isinstance(item, str):
            gallery.append({"src": item, "alt": ""})
    return gallery


def _compact_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for index, item in enumerate(value[:MAX_DETAIL_LIST_ITEMS]):
        if type(item) is not str:
            raise ValueError(f"text list item {index} must be a string")
        result.append(_truncate_text(item, 260) or "")
    return result


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
        raw_options = addition.get("options", [])
        if type(raw_options) is not list:
            raise ValueError("configurator options must be a list")
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            price_bgn = option.get("price")
            options.append(
                {
                    "label": _truncate_text(
                        _first_present(option, "label", "labelVoucher"),
                        220,
                    ),
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
    if type(value) is not str:
        raise ValueError("text value must be a string")
    if len(value) <= limit:
        return value
    return value[:limit]


def _validate_iso_date(value: str) -> None:
    date.fromisoformat(value)


def _event_log_path() -> Path:
    override = os.getenv("SKYAI_V2_EVENT_LOG_PATH")
    if override:
        return Path(override).expanduser()
    return get_hermes_home() / "skyai_v2" / "events.jsonl"


def _hash_optional(value: str) -> str:
    if value == "":
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_event_properties(properties: dict[str, Any]) -> str | None:
    for key, value in properties.items():
        if not isinstance(key, str) or key not in EVENT_PROPERTY_SCHEMA:
            return "unsupported_event_property"
        rule = EVENT_PROPERTY_SCHEMA[key]
        expected_type = rule["type"]
        if expected_type == "string":
            if not isinstance(value, str):
                return f"{key}_must_be_a_string"
            if len(value) > int(rule.get("maxLength", MAX_EVENT_PROPERTY_VALUE_LENGTH)):
                return f"{key}_too_long"
            allowed_values = rule.get("enum")
            if allowed_values is not None and value not in allowed_values:
                return f"{key}_unsupported_value"
        elif expected_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"{key}_must_be_an_integer"
            if "minimum" in rule and value < int(rule["minimum"]):
                return f"{key}_below_minimum"
        elif expected_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{key}_must_be_a_number"
            decimal = Decimal(str(value))
            if not decimal.is_finite():
                return f"{key}_must_be_finite"
            if "minimum" in rule and decimal < Decimal(str(rule["minimum"])):
                return f"{key}_below_minimum"
        elif expected_type == "boolean":
            if not isinstance(value, bool):
                return f"{key}_must_be_a_boolean"
        else:
            return "invalid_event_property_schema"
    return None


def _registered_event_secret_values() -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for name in REGISTERED_EVENT_SECRET_ENV_NAMES:
        value = os.environ.get(name)
        if not value or len(value) < 8 or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return tuple(values)


def _redact_registered_event_secrets(properties: dict[str, Any]) -> dict[str, Any]:
    secrets = _registered_event_secret_values()
    if not secrets:
        return dict(properties)
    redacted: dict[str, Any] = {}
    for key, value in properties.items():
        if not isinstance(value, str):
            redacted[key] = value
            continue
        redacted_value = value
        for secret in secrets:
            redacted_value = redacted_value.replace(secret, "[redacted-secret]")
        redacted[key] = redacted_value
    return redacted
