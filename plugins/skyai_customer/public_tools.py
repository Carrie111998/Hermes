"""Public-safe SkyVision tools for a customer-facing SkyAI Hermes runtime."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
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
CATALOG_INDEX_TTL_SECONDS = int(os.getenv("SKYAI_CATALOG_INDEX_TTL_SECONDS", "21600"))
VALUE_VOUCHER_PUBLIC_URL = f"{PUBLIC_SITE_BASE_URL}/подарък/ваучер-за-подарък-на-стойност/"
VALUE_VOUCHER_OPTION = {
    "title": "Ваучер за подарък на стойност",
    "public_url": VALUE_VOUCHER_PUBLIC_URL,
    "price_text": "стойност по избор",
    "location": "валиден за SkyVision каталога",
    "category_key": "voucher-value",
    "summary": (
        "Универсален, стилен подарък, който оставя избора на преживяване на получателя "
        "сред 1000+ SkyVision преживявания."
    ),
    "important_note": (
        "Не казвай, че само този ваучер дава свобода: всеки SkyVision ваучер работи като "
        "стойност/депозит. Разликата е, че ваучерът на стойност не изписва конкретна услуга "
        "и изглежда като съзнателно оставен избор за получателя."
    ),
}

_CATALOG_INDEX_CACHE: dict[str, Any] = {"expires_at": 0.0, "items": None}

_QUERY_STOPWORDS = frozenset(
    {
        "аз",
        "ако",
        "без",
        "бих",
        "във",
        "вече",
        "дали",
        "дайте",
        "добре",
        "ден",
        "до",
        "eur",
        "euro",
        "евро",
        "за",
        "има",
        "имате",
        "искам",
        "какво",
        "като",
        "към",
        "ли",
        "ми",
        "може",
        "мога",
        "моля",
        "помощ",
        "повод",
        "рожден",
        "на",
        "не",
        "нещо",
        "някакъв",
        "около",
        "от",
        "по",
        "предложиш",
        "препоръчаш",
        "със",
        "това",
        "трябва",
        "търся",
        "уникален",
        "уникална",
        "уникално",
        "въздействащ",
        "въздействаща",
        "въздействащо",
        "ще",
    }
)
_TOKEN_EXPANSIONS = {
    "двама": ("двойка", "двойки", "двоен"),
    "двойка": ("двама", "двойки"),
    "двойки": ("двама", "двойка"),
    "спа": ("spa", "уелнес", "релакс"),
    "spa": ("спа", "уелнес", "релакс"),
    "уелнес": ("спа", "spa", "релакс"),
    "приятелка": ("жена", "дама"),
    "майка": ("жена", "дама"),
    "съпруга": ("жена", "дама"),
    "дама": ("жена", "приятелка"),
}

_CALM_QUERY_TOKENS = frozenset(
    {
        "спокоен",
        "спокойна",
        "спокойно",
        "позитивен",
        "позитивна",
        "позитивно",
        "релакс",
        "лежерен",
        "лежерна",
        "нежен",
        "нежна",
    }
)
_CALM_PRODUCT_SIGNALS = frozenset(
    {
        "арт",
        "бюти",
        "вечеря",
        "винен",
        "вино",
        "визия",
        "гурме",
        "дегустация",
        "йога",
        "козметика",
        "кулинар",
        "масаж",
        "музикален",
        "нощувка",
        "почивка",
        "релакс",
        "рисуване",
        "романтика",
        "солна",
        "спа",
        "стил",
        "творчески",
        "терапия",
        "уелнес",
        "флотация",
    }
)
_SINGLE_RECIPIENT_GIFT_SIGNALS = frozenset(
    {
        "баща",
        "брат",
        "дама",
        "жена",
        "майка",
        "мъж",
        "приятел",
        "приятелка",
        "съпруг",
        "съпруга",
    }
)
_MULTI_RECIPIENT_GIFT_SIGNALS = frozenset(
    {
        "двама",
        "двойка",
        "двойки",
        "семейство",
        "семеен",
        "семейна",
        "група",
        "компания",
    }
)
_TWO_PERSON_PRODUCT_SIGNALS = frozenset(
    {
        "за двама",
        "двама",
        "двойка",
        "двойки",
        "романтика",
    }
)
_EXTREME_PRODUCT_SIGNALS = frozenset(
    {
        "адреналин",
        "акваланг",
        "атв",
        "бънджи",
        "гмуркане",
        "джет",
        "екстрем",
        "жирокоптер",
        "мотор",
        "офроуд",
        "парапланер",
        "парашут",
        "полет",
        "рафтинг",
        "самолет",
        "скок",
        "стрелба",
    }
)

_KNOWN_LOCATION_COORDS = {
    "сливен": (42.6817, 26.3229),
    "ямбол": (42.4842, 26.5035),
    "бургас": (42.5048, 27.4626),
    "стара загора": (42.4258, 25.6345),
    "казанлък": (42.6194, 25.3930),
    "павел баня": (42.5942, 25.2089),
    "могилово": (42.4250, 25.6270),
    "приморско": (42.2679, 27.7561),
    "созопол": (42.4173, 27.6962),
    "несебър": (42.6601, 27.7206),
    "балчик": (43.4217, 28.1585),
    "сопот": (42.6520, 24.7545),
    "пловдив": (42.1354, 24.7453),
    "житница": (42.3540, 24.7250),
    "пазарджик": (42.1928, 24.3336),
    "сърница": (41.7386, 24.0249),
    "софия": (42.6977, 23.3219),
    "варна": (43.2141, 27.9147),
    "велико търново": (43.0757, 25.6172),
    "русе": (43.8356, 25.9657),
    "велинград": (42.0275, 23.9916),
}
_LOCATION_ALIASES = {
    "sliven": "сливен",
    "yambol": "ямбол",
    "burgas": "бургас",
    "stara zagora": "стара загора",
    "stara zagora province": "стара загора",
    "plovdiv": "пловдив",
    "plovdiv province": "пловдив",
    "sofia": "софия",
    "sofia city province": "софия",
    "sofia province": "софия",
    "varna": "варна",
    "varna province": "варна",
    "dobrich province": "варна",
    "pazardzhik": "пазарджик",
    "pazardzhik province": "пазарджик",
    "sarnitsa": "сърница",
    "blagoevgrad province": "благоевград",
    "smoljan": "смолян",
    "montana province": "монтана",
    "район бургас": "бургас",
    "обл бургас": "бургас",
    "област бургас": "бургас",
    "район сливен": "сливен",
    "обл сливен": "сливен",
    "област сливен": "сливен",
    "район стара загора": "стара загора",
    "обл стара загора": "стара загора",
    "област стара загора": "стара загора",
}


@dataclass(frozen=True)
class QueryTraits:
    tokens: list[str]
    normalized: str
    requested_location: str | None = None
    requested_coordinates: tuple[float, float] | None = None
    calm_recipient: bool = False
    mature_recipient: bool = False
    broad_discovery: bool = False
    single_recipient_gift: bool = False

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


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _rating_value(item: dict[str, Any]) -> str | None:
    value = (
        item.get("rating")
        or item.get("averageRating")
        or item.get("avgRating")
        or item.get("ratingValue")
    )
    if value is None or value == "":
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return str(value)


def _is_on_offer(item: dict[str, Any], old_price_bgn: Any) -> bool | None:
    explicit = _boolish(
        item.get("isOnOffer")
        or item.get("is_on_offer")
        or item.get("hasDiscount")
    )
    if explicit is not None:
        return explicit
    return old_price_bgn is not None and old_price_bgn != ""


def _safe_limit(limit: int | None) -> int:
    if not limit:
        return 8
    return max(1, min(MAX_RETURN_ITEMS, int(limit)))


def _infer_price_bounds_from_query(
    query: str,
    *,
    min_price_eur: float | None,
    max_price_eur: float | None,
) -> tuple[float | None, float | None]:
    text = _normalize_search_text(query)
    inferred_min = min_price_eur
    inferred_max = max_price_eur
    if inferred_max is None:
        eur_match = re.search(r"(?:до|под|около|към)?\s*(\d+(?:[.,]\d+)?)\s*(?:евро|eur|euro|€)", text)
        if eur_match:
            inferred_max = _float_or_none(eur_match.group(1))
    if inferred_max is None:
        bgn_match = re.search(r"(?:до|под|около|към)?\s*(\d+(?:[.,]\d+)?)\s*(?:лв|лева|bgn)", text)
        bgn_value = _float_or_none(bgn_match.group(1)) if bgn_match else None
        if bgn_value is not None:
            inferred_max = float(Decimal(str(bgn_value)) / BGN_PER_EUR)
    return inferred_min, inferred_max


def _float_or_none(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def _catalog_search_candidates(
    *,
    query: str,
    direct_items: list[dict[str, Any]],
    min_price_eur: float | None,
    max_price_eur: float | None,
    limit: int,
) -> list[dict[str, Any]]:
    merged = _dedupe_products(direct_items)
    if query.strip():
        try:
            merged = _dedupe_products([*merged, *_catalog_index_items()])
        except Exception:
            pass
    if not query.strip():
        return _filter_products_by_budget(merged, min_price_eur, max_price_eur)[:limit]
    ranked = _rank_products(
        merged,
        query=query,
        min_price_eur=min_price_eur,
        max_price_eur=max_price_eur,
    )
    return ranked[:limit]


def _catalog_location_context(items: list[dict[str, Any]], traits: QueryTraits) -> dict[str, Any] | None:
    if not traits.requested_location:
        return None
    distances = sorted(
        {
            int(distance)
            for item in items
            if isinstance((distance := item.get("_skyai_distance_km")), int)
        }
    )
    if not distances:
        return {
            "requested_location": traits.requested_location,
            "guidance": (
                "Клиентът е дал локация, но върнатите кандидати нямат надеждна дистанция. "
                "Кажи това внимателно и не се преструвай, че са близо."
            ),
        }
    nearest = distances[0]
    farthest = distances[-1]
    if nearest <= 120:
        guidance = (
            "Има географски близки кандидати. Дръж отговора около най-близките релевантни "
            "варианти и не прескачай към далечни градове, освен ако клиентът поиска повече "
            "лукс, море или по-различна посока."
        )
    else:
        guidance = (
            "Няма силен близък кандидат. Обясни, че разширяваш радиуса, и попитай дали "
            "за клиента е по-важна близостта или най-точното преживяване."
        )
    return {
        "requested_location": traits.requested_location,
        "nearest_returned_distance_km": nearest,
        "farthest_returned_distance_km": farthest,
        "guidance": guidance,
    }


def _catalog_recipient_context(traits: QueryTraits) -> dict[str, Any] | None:
    if not traits.single_recipient_gift:
        return None
    return {
        "gift_recipient_scope": "single_recipient",
        "guidance": (
            "Клиентът изглежда избира подарък за един конкретен човек. Предпочитай индивидуални "
            "преживявания. Ако вариант за двама остава най-подходящ, рамкирай го като възможност "
            "получателят да сподели преживяването с близък човек, не като подарък за двама по default."
        ),
    }


def _catalog_value_voucher_option(items: list[dict[str, Any]], traits: QueryTraits) -> dict[str, Any] | None:
    if not traits.requested_location or not traits.broad_discovery:
        return None
    has_exact_location_match = any(_product_known_location(item) == traits.requested_location for item in items)
    if has_exact_location_match:
        return None
    return {
        **VALUE_VOUCHER_OPTION,
        "when_to_use": (
            "Добра алтернатива, когато няма достатъчно релевантно предложение в точната локация, "
            "или когато клиентът иска подарък, но не е сигурен кое преживяване ще зарадва получателя."
        ),
        "answer_guidance": (
            "Предложи го човешки като четвърта, универсална опция след най-близките релевантни cards. "
            "Не го представяй като отказ от търсене, а като елегантен начин подареният човек сам да избере."
        ),
    }


def _catalog_index_items() -> list[dict[str, Any]]:
    now = time.monotonic()
    cached = _CATALOG_INDEX_CACHE.get("items")
    if isinstance(cached, list) and now < float(_CATALOG_INDEX_CACHE.get("expires_at") or 0):
        return [item for item in cached if isinstance(item, dict)]
    url = f"{PUBLIC_CATALOG_BASE_URL}/products?page=1&size={MAX_CATALOG_SIZE}&sort=&minPrice=0&maxPrice=4000&search="
    items = _extract_products(_http_json(url))
    _CATALOG_INDEX_CACHE["items"] = items
    _CATALOG_INDEX_CACHE["expires_at"] = now + max(60, CATALOG_INDEX_TTL_SECONDS)
    return items


def _dedupe_products(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("id") or item.get("product_id") or item.get("slug") or item.get("name") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _rank_products(
    items: list[dict[str, Any]],
    *,
    query: str,
    min_price_eur: float | None,
    max_price_eur: float | None,
) -> list[dict[str, Any]]:
    traits = _query_traits(query)
    tokens = traits.tokens
    filtered = _filter_products_by_budget(items, min_price_eur, max_price_eur)
    scored: list[tuple[float, dict[str, Any]]] = []
    for index, item in enumerate(filtered):
        score = _product_relevance_score(item, traits=traits, max_price_eur=max_price_eur)
        if score <= 0:
            continue
        item = {**item}
        if traits.requested_location:
            item["_skyai_requested_location"] = traits.requested_location
        distance_km = _product_distance_km(item, traits)
        if distance_km is not None:
            item["_skyai_distance_km"] = round(distance_km)
        item["_skyai_category_key"] = _product_family_key(item)
        # Stable tie-breaker preserves catalog popularity/order when evidence is equal.
        scored.append((score - (index * 0.0001), item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored:
        return _diversify_ranked_products([item for _score, item in scored], traits=traits)
    return filtered


def _diversify_ranked_products(
    ranked: list[dict[str, Any]],
    *,
    tokens: list[str] | None = None,
    traits: QueryTraits | None = None,
) -> list[dict[str, Any]]:
    if traits is None:
        traits = QueryTraits(tokens=tokens or [], normalized="")
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    seen_generic_families: set[str] = set()
    for item in ranked:
        family = _product_family_key(item)
        if not family or _product_family_matches_query(family, traits.tokens) or family not in seen_generic_families:
            selected.append(item)
            if family and not _product_family_matches_query(family, traits.tokens):
                seen_generic_families.add(family)
            continue
        deferred.append(item)
    return [*selected, *deferred]


def _product_family_key(item: dict[str, Any]) -> str:
    category = item.get("category_slug") or item.get("categorySlug")
    if category:
        return _normalize_search_text(category)
    slug = str(item.get("slug") or "").strip("/")
    if slug:
        return _normalize_search_text(slug.split("/", 1)[0])
    title = _normalize_search_text(item.get("title") or item.get("name"))
    return " ".join(title.split()[:3])


def _product_family_matches_query(family: str, tokens: list[str]) -> bool:
    if not family or not tokens:
        return False
    return any(token in family for token in tokens)


def _query_traits(query: str) -> QueryTraits:
    normalized = _normalize_search_text(query)
    tokens = _query_tokens(query)
    requested_location = _find_known_location(normalized)
    requested_coordinates = _KNOWN_LOCATION_COORDS.get(requested_location or "")
    mature_recipient = bool(re.search(r"\b(?:50\s*\+|над\s+50|около\s+50)\b", normalized))
    calm_recipient = bool(
        any(token in _CALM_QUERY_TOKENS for token in tokens)
        or mature_recipient
        or "не екстрем" in normalized
    )
    broad_discovery = bool(
        any(token in tokens for token in ("подарък", "подаръка", "идея", "идеи"))
        and not any(_product_family_matches_query(family, tokens) for family in _SPECIFIC_PRODUCT_FAMILIES)
    )
    single_recipient_gift = bool(
        broad_discovery
        and any(token in _SINGLE_RECIPIENT_GIFT_SIGNALS for token in tokens)
        and not any(token in _MULTI_RECIPIENT_GIFT_SIGNALS for token in tokens)
    )
    return QueryTraits(
        tokens=tokens,
        normalized=normalized,
        requested_location=requested_location,
        requested_coordinates=requested_coordinates,
        calm_recipient=calm_recipient,
        mature_recipient=mature_recipient,
        broad_discovery=broad_discovery,
        single_recipient_gift=single_recipient_gift,
    )


_SPECIFIC_PRODUCT_FAMILIES = frozenset(
    {
        "атв",
        "бънджи",
        "жирокоптер",
        "масаж",
        "парапланер",
        "парашут",
        "самолет",
        "спа",
        "флотация",
    }
)


def _find_known_location(text: str) -> str | None:
    normalized = _normalize_search_text(text)
    for alias, canonical in sorted(_LOCATION_ALIASES.items(), key=lambda pair: len(pair[0]), reverse=True):
        if alias in normalized and canonical in _KNOWN_LOCATION_COORDS:
            return canonical
    for location in sorted(_KNOWN_LOCATION_COORDS, key=len, reverse=True):
        if location in normalized:
            return location
    return None


def _product_location_text(item: dict[str, Any]) -> str:
    return _normalize_search_text(
        " ".join(
            str(value or "")
            for value in (
                item.get("location"),
                item.get("locationName"),
                item.get("locationArea"),
                item.get("city"),
                item.get("region"),
            )
        )
    )


def _product_known_location(item: dict[str, Any]) -> str | None:
    for key in ("locationName", "location", "city"):
        location = _find_known_location(str(item.get(key) or ""))
        if location:
            return location
    for key in ("locationArea", "region"):
        location = _find_known_location(str(item.get(key) or ""))
        if location:
            return location
    return _find_known_location(_product_location_text(item))


def _product_distance_km(item: dict[str, Any], traits: QueryTraits) -> float | None:
    if not traits.requested_coordinates:
        return None
    product_location = _product_known_location(item)
    coordinates = _KNOWN_LOCATION_COORDS.get(product_location or "")
    if not coordinates:
        return None
    return _haversine_km(traits.requested_coordinates, coordinates)


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(hav))


def _filter_products_by_budget(
    items: list[dict[str, Any]],
    min_price_eur: float | None,
    max_price_eur: float | None,
) -> list[dict[str, Any]]:
    if min_price_eur is None and max_price_eur is None:
        return items
    filtered: list[dict[str, Any]] = []
    max_with_tolerance = max_price_eur * 1.08 if max_price_eur is not None else None
    for item in items:
        price_eur = _product_price_eur(item)
        if price_eur is None:
            filtered.append(item)
            continue
        if min_price_eur is not None and price_eur < min_price_eur * 0.92:
            continue
        if max_with_tolerance is not None and price_eur > max_with_tolerance:
            continue
        filtered.append(item)
    return filtered


def _product_relevance_score(
    item: dict[str, Any],
    *,
    traits: QueryTraits,
    max_price_eur: float | None,
) -> float:
    tokens = traits.tokens
    if not tokens:
        return 1.0
    title = _normalize_search_text(item.get("title") or item.get("name"))
    slug = _normalize_search_text(item.get("slug"))
    location = _product_location_text(item)
    provider = _normalize_search_text(_provider_name(item.get("provider")))
    restrictions = item.get("restrictions") if isinstance(item.get("restrictions"), dict) else {}
    detail = _normalize_search_text(
        " ".join(
            str(value or "")
            for value in (
                restrictions.get("duration"),
                restrictions.get("serviceForWho"),
                restrictions.get("forKids"),
                item.get("duration"),
                item.get("participants"),
            )
        )
    )
    combined = f"{title} {slug} {location} {provider} {detail}"
    score = 0.0
    for token in tokens:
        if token in title:
            score += 8.0
        if token in slug:
            score += 5.0
        if token in location:
            score += 6.0
        if token in detail:
            score += 3.0
        if token in provider:
            score += 1.0
        if token in combined:
            score += 1.0
    score += _location_relevance_score(item, traits)
    score += _recipient_fit_score(title=title, slug=slug, detail=detail, traits=traits)
    if max_price_eur is not None:
        price_eur = _product_price_eur(item)
        if price_eur is not None:
            closeness = max(0.0, 1.0 - min(abs(price_eur - max_price_eur) / max(max_price_eur, 1.0), 1.0))
            score += closeness * 8.0
            if price_eur < max_price_eur * 0.35:
                score -= 2.0
            elif price_eur < max_price_eur * 0.55:
                score -= 0.8
            elif price_eur >= max_price_eur * 0.55:
                score += 1.5
    orders_count = item.get("ordersCount")
    if isinstance(orders_count, int) and orders_count > 0:
        score += min(2.0, orders_count / 100.0)
    rating_count = item.get("ratingCount")
    if isinstance(rating_count, int) and rating_count > 0:
        score += min(1.0, rating_count / 50.0)
    return score


def _location_relevance_score(item: dict[str, Any], traits: QueryTraits) -> float:
    if not traits.requested_location:
        return 0.0
    product_location = _product_known_location(item)
    if not product_location:
        text = _product_location_text(item)
        return 1.5 if "онлайн" in text or "цяла българия" in text else -3.0
    if product_location == traits.requested_location:
        return 14.0
    distance = _product_distance_km(item, traits)
    if distance is None:
        return 0.0
    if traits.broad_discovery:
        if distance <= 45:
            return 24.0
        if distance <= 90:
            return 21.0
        if distance <= 120:
            return 15.0
        if distance <= 150:
            return -12.0
        if distance <= 200:
            return -28.0
        return -44.0
    if distance <= 45:
        return 16.0
    if distance <= 90:
        return 14.0
    if distance <= 130:
        return 10.0
    if distance <= 180:
        return -10.0
    if distance <= 240:
        return -12.0
    return -14.0


def _recipient_fit_score(
    *,
    title: str,
    slug: str,
    detail: str,
    traits: QueryTraits,
) -> float:
    if not traits.calm_recipient:
        return 0.0
    combined = f"{title} {slug} {detail}"
    calm_hits = sum(1 for signal in _CALM_PRODUCT_SIGNALS if signal in combined)
    extreme_hits = sum(1 for signal in _EXTREME_PRODUCT_SIGNALS if signal in combined)
    score = min(14.0, calm_hits * 4.0)
    if extreme_hits:
        score -= min(16.0, extreme_hits * 4.0)
    if traits.mature_recipient and any(signal in combined for signal in ("детски", "дете", "деца", "ученик", "тийн")):
        score -= 14.0
    if "жена" in traits.tokens and any(signal in combined for signal in ("козметика", "визия", "стил", "терапия", "релакс")):
        score += 3.0
    if traits.single_recipient_gift and any(signal in combined for signal in _TWO_PERSON_PRODUCT_SIGNALS):
        score -= 7.0
    return score


def _query_tokens(query: str) -> list[str]:
    normalized = _normalize_search_text(query)
    raw_tokens = re.findall(r"[a-zа-я0-9]+", normalized, flags=re.IGNORECASE)
    tokens: list[str] = []
    for token in raw_tokens:
        if token.isdigit() or token in _QUERY_STOPWORDS:
            continue
        if len(token) <= 2 and token not in {"atv", "спа", "spa"}:
            continue
        tokens.append(token)
        tokens.extend(_TOKEN_EXPANSIONS.get(token, ()))
    return _dedupe_strings(tokens)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _normalize_search_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = text.replace("ё", "е")
    text = re.sub(r"[_/\\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _product_price_eur(item: dict[str, Any]) -> float | None:
    price_eur = item.get("price_eur") or item.get("priceEur")
    if price_eur not in (None, ""):
        return _float_or_none(str(price_eur))
    price_bgn = item.get("price") or item.get("price_bgn") or item.get("priceBgn")
    if price_bgn in (None, ""):
        return None
    try:
        return float(Decimal(str(price_bgn)) / BGN_PER_EUR)
    except Exception:
        return None


def handle_skyai_catalog_search(
    query: str = "",
    min_price_eur: float | None = None,
    max_price_eur: float | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    inferred_min_price_eur, inferred_max_price_eur = _infer_price_bounds_from_query(
        query,
        min_price_eur=min_price_eur,
        max_price_eur=max_price_eur,
    )
    size = MAX_CATALOG_SIZE
    safe_limit = _safe_limit(limit)
    min_price_bgn = _money_eur_to_bgn(inferred_min_price_eur)
    max_price_bgn = _money_eur_to_bgn(inferred_max_price_eur) if inferred_max_price_eur is not None else 4000
    url = (
        f"{PUBLIC_CATALOG_BASE_URL}/products"
        f"?page=1&size={size}&sort=&minPrice={min_price_bgn}&maxPrice={max_price_bgn}"
        f"&search={quote(query or '')}"
    )
    direct_items = _extract_products(_http_json(url))
    items = _catalog_search_candidates(
        query=query,
        direct_items=direct_items,
        min_price_eur=inferred_min_price_eur,
        max_price_eur=inferred_max_price_eur,
        limit=safe_limit,
    )
    traits = _query_traits(query)
    return {
        "status": "ok",
        "source": "skyvision_public_cache",
        "query": query or "",
        "filters": {
            "min_price_eur": inferred_min_price_eur,
            "max_price_eur": inferred_max_price_eur,
            "min_price_bgn": min_price_bgn,
            "max_price_bgn": max_price_bgn,
            "inferred_from_query": {
                "min_price_eur": min_price_eur is None and inferred_min_price_eur is not None,
                "max_price_eur": max_price_eur is None and inferred_max_price_eur is not None,
            },
        },
        "count": len(items),
        "location_context": _catalog_location_context(items, traits),
        "recipient_context": _catalog_recipient_context(traits),
        "value_voucher_option": _catalog_value_voucher_option(items, traits),
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
        "sales_playbook": {
            "principles": [
                "Работи консултативно: първо разбери човека, повода, бюджета, локацията и желаното усещане.",
                "Давай малко, но силни предложения: 1-3 релевантни посоки с ясна причина са по-добри от дълъг списък.",
                "Използвай публични търговски предимства като леко, човешко намигване към покупката, не като сух списък.",
                "Ако клиентът уточни нещо, следващият отговор трябва да се промени според уточнението, а не да повтаря стария pitch.",
                "Не гадай лични профили и не измисляй факти; персонализирай само по казаното от клиента и по public tool evidence.",
            ],
            "skyvision_application": (
                "Бонусният полет, безплатната доставка, опаковките, BookNow, rating/цена и ваучерът на стойност "
                "са sales аргументи, които SkyAI избира по търговско усещане според разговора. "
                "Не ги изреждай всички и не повтаряй един и същи коз, ако клиентът не проявява интерес към него."
            ),
            "guardrail": "Това са общи поведенчески принципи, не keyword guard-и и не route правила.",
        },
        "active_campaigns": [
            {
                "name": "Подарък панорамен полет над морето",
                "public_url": "https://skyvision.bg/campaign/free-panoramic-flight/",
                "terms_url": "https://panel.skyvision.bg/kampaniya-bezplaten-polet-nad-moreto",
                "customer_summary": (
                    "SkyVision благодари на купувача/резервиращия с безплатен панорамен полет "
                    "над морето към покупка или директна BookNow резервация."
                ),
                "sales_tone": (
                    "Поднасяй бонуса като топла благодарност от SkyVision към купувача, че ни се доверява, "
                    "и като покана лично да усети магията на летенето. В sales разговор може да го споменеш "
                    "кратко още в първия полезен отговор, защото клиентът може да не зададе втори въпрос. "
                    "Не го повтаряй във всеки отговор и не го вкарвай, ако клиентът пита за чист support казус. "
                    "Не добавяй terms/URL/legal детайли, освен ако клиентът сам пита за условия или eligibility."
                ),
                "bonus_owner": {
                    "default": "купувачът/резервиращият, който прави успешната поръчка или BookNow резервация",
                    "customer_facing_framing": (
                        "Бонусът е нашето лично благодаря към теб като купувач, че избираш SkyVision. "
                        "Получателят на ваучера получава избраното преживяване, а ти получаваш възможност "
                        "да полетиш с нас над морето."
                    ),
                    "do_not_say": (
                        "Не казвай, че бонусът автоматично е за получателя на ваучера. Ако клиентът пита "
                        "дали може да го даде на получателя, обясни първо default-а и чак тогава човешката "
                        "възможност за изключение през founder_transfer_guidance."
                    ),
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
                    "how_to_book": "клиентът избира таймслот и резервира полета през системата",
                    "booknow_timing": "при BookNow подаръчният полет се резервира след изпълнение на основната услуга",
                    "not_lottery": "без томбола и без игра на късмета",
                },
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
                    "public_url": (
                        "https://skyvision.bg/подарък/полет-с-жирокоптер/панорамен-полет-над-морето/"
                    ),
                    "price_eur": "0.00",
                    "duration": "10 мин.",
                    "participants": "1 човек",
                    "location": "Летище Приморско",
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
                "Ломлиев е съосновател, собственик, пилот-инструктор и изпитващ, и лично разглежда "
                "такива случаи. Default-ът остава: бонусът е благодарност към купувача. Ако клиентът "
                "изрично поиска получателят на подаръка да използва бонуса, SkyAI може топло да каже, "
                "че Емил лично разглежда такива молби, защото мисията на SkyVision е да прави хората "
                "щастливи и да споделя любовта към летенето."
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
                "След въвеждане или промяна на пожеланието клиентът натиска „Редактирай поздрава“, за да се обнови preview-то.",
                "Полето „Име на ползвател“ е отделно от пожеланието.",
                "Ако пожеланието трябва да се коригира след поръчка, това може да стане от панела или през екипа на SkyVision, докато ваучерът още не е подготвен/изпратен.",
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
            "answer_guidance": (
                "Ако клиентът пита за официален подарък, бланка или пожелание, обясни ясно разликата "
                "между бланка, поздрав, име на ползвател, опаковка и електронен ваучер. "
                "Когато споменаваш цени, поставяй EUR първо; BGN е вторична ориентировъчна стойност."
            ),
        },
        "delivery": {
            "courier": "Speedy",
            "current_fee": "безплатна доставка",
            "office_locator_url": "https://www.speedy.bg/bg/speedy-offices-automats",
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
    old_price_bgn = item.get("oldPrice") or item.get("old_price") or item.get("regularPrice")
    old_price_eur = item.get("oldPriceEur") or item.get("old_price_eur") or _money_bgn_to_eur(old_price_bgn)
    return {
        "id": item.get("id") or item.get("product_id"),
        "title": item.get("title") or item.get("name"),
        "slug": slug or None,
        "category_slug": item.get("category_slug") or item.get("categorySlug"),
        "category_key": item.get("_skyai_category_key") or _product_family_key(item),
        "public_url": item.get("url") or _public_product_url(slug),
        "location": item.get("location") or item.get("locationName") or item.get("city") or item.get("region"),
        "location_area": item.get("locationArea"),
        "distance_from_requested_location_km": item.get("_skyai_distance_km"),
        "requested_location": item.get("_skyai_requested_location"),
        "price_bgn": _money_decimal_string(price_bgn),
        "price_eur": _money_decimal_string(price_eur),
        "old_price_bgn": _money_decimal_string(old_price_bgn),
        "old_price_eur": _money_decimal_string(old_price_eur),
        "rating": _rating_value(item),
        "rating_count": _int_or_none(item.get("ratingCount") or item.get("reviewsCount")),
        "orders_count": _int_or_none(item.get("ordersCount")),
        "is_on_offer": _is_on_offer(item, old_price_bgn),
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
    old_price_bgn = source.get("oldPrice") or source.get("old_price") or source.get("regularPrice")
    old_price_eur = source.get("oldPriceEur") or source.get("old_price_eur") or _money_bgn_to_eur(old_price_bgn)
    return {
        "id": source.get("id") or source.get("product_id"),
        "title": source.get("title") or source.get("name"),
        "slug": slug or None,
        "public_url": metadata.get("canonical") or source.get("url") or _public_product_url(slug),
        "location": source.get("location") or source.get("locationName") or source.get("city"),
        "location_area": source.get("locationArea") or source.get("region"),
        "price_bgn": _money_decimal_string(price_bgn),
        "price_eur": _money_decimal_string(price_eur),
        "old_price_bgn": _money_decimal_string(old_price_bgn),
        "old_price_eur": _money_decimal_string(old_price_eur),
        "rating": _rating_value(source),
        "rating_count": _int_or_none(source.get("ratingCount") or source.get("reviewsCount")),
        "orders_count": _int_or_none(source.get("ordersCount")),
        "is_on_offer": _is_on_offer(source, old_price_bgn),
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
