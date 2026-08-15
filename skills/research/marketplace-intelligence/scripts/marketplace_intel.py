#!/usr/bin/env python3
"""Validate and reconcile third-party marketplace observations.

The script is intentionally source-agnostic: Hermes gathers public evidence with
its existing browser/search tools, records one JSON object per observation, and
uses this program for deterministic matching, validation, deduplication, and
status accounting. It does not log in, scrape private surfaces, or contact a
marketplace participant.

Usage:
    python3 marketplace_intel.py reconcile \
        --profile product.json --observations observations.jsonl \
        --format json|markdown [--out report]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any, Iterable
from urllib.parse import urlsplit

ALLOWED_STATUSES = {"offered", "pending", "sold", "completed", "removed", "unknown"}
ALLOWED_METHODS = {
    "live_page",
    "browser",
    "api",
    "archive",
    "search_index",
    "search_snippet",
    "other",
}
ALLOWED_SCOPES = {"full_page", "listing_card", "snippet", "title_only"}
ALLOWED_STATUS_BASES = {
    "platform_marker",
    "seller_statement",
    "buyer_statement",
    "moderator_marker",
    "search_snippet",
    "archive_snapshot",
    "unspecified",
}
CONFIRMABLE_SCOPES = {"full_page", "listing_card"}
WEAK_METHODS = {"search_index", "search_snippet"}
WEAK_SCOPES = {"snippet", "title_only"}


class ValidationError(ValueError):
    """Raised when a profile or observation violates the evidence contract."""


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def normalize_text(value: str) -> str:
    """Normalize marketplace text while preserving token boundaries."""
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    # Marketplace titles vary between `128GB` and `128 GB`. Treat only common
    # capacity units this way; do not concatenate arbitrary adjacent tokens.
    text = re.sub(r"\b(\d+)\s+(gb|tb|mb)\b", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains_phrase(haystack: str, phrase: str) -> bool:
    normalized = normalize_text(phrase)
    if not normalized:
        raise ValidationError("profile phrases must not normalize to empty text")
    return f" {normalized} " in f" {haystack} "


def validate_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValidationError("profile must be a JSON object")
    name = _require_text(profile.get("name"), "profile.name")
    match_any = profile.get("match_any")
    if not isinstance(match_any, list) or not match_any:
        raise ValidationError("profile.match_any must be a non-empty list of phrase groups")
    clean_groups: list[list[str]] = []
    for group_index, group in enumerate(match_any):
        if not isinstance(group, list) or not group:
            raise ValidationError(
                f"profile.match_any[{group_index}] must be a non-empty list of phrases"
            )
        clean_groups.append(
            [
                _require_text(phrase, f"profile.match_any[{group_index}] phrase")
                for phrase in group
            ]
        )
    exclude_any = profile.get("exclude_any", [])
    if not isinstance(exclude_any, list):
        raise ValidationError("profile.exclude_any must be a list")
    clean_exclusions = [
        _require_text(phrase, "profile.exclude_any phrase") for phrase in exclude_any
    ]
    return {"name": name, "match_any": clean_groups, "exclude_any": clean_exclusions}


def _parse_timestamp(value: Any, field: str) -> str:
    from datetime import datetime

    text = _require_text(value, field)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must include a timezone")
    return text


def _timestamp_sort_key(value: str):
    from datetime import datetime

    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(candidate)


def _parse_price(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a positive decimal amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field} must be a positive decimal amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValidationError(f"{field} must be a positive decimal amount")
    try:
        rounded = amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValidationError(f"{field} must be a positive decimal amount") from exc
    if rounded <= 0:
        raise ValidationError(f"{field} must be at least 0.01 after rounding")
    return rounded


def validate_observation(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValidationError(f"observation[{index}] must be a JSON object")
    result = dict(row)
    prefix = f"observation[{index}]"
    for field in ("source", "listing_id", "title"):
        result[field] = _require_text(result.get(field), f"{prefix}.{field}")

    url = _require_text(result.get("url"), f"{prefix}.url")
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValidationError(f"{prefix}.url must be an http(s) URL")
    result["url"] = url

    result["observed_at"] = _parse_timestamp(
        result.get("observed_at"), f"{prefix}.observed_at"
    )
    if result.get("posted_at") is not None:
        result["posted_at"] = _parse_timestamp(
            result.get("posted_at"), f"{prefix}.posted_at"
        )

    method = _require_text(
        result.get("retrieval_method"), f"{prefix}.retrieval_method"
    )
    if method not in ALLOWED_METHODS:
        raise ValidationError(
            f"{prefix}.retrieval_method must be one of {sorted(ALLOWED_METHODS)}"
        )
    result["retrieval_method"] = method

    scope = _require_text(result.get("evidence_scope"), f"{prefix}.evidence_scope")
    if scope not in ALLOWED_SCOPES:
        raise ValidationError(f"{prefix}.evidence_scope must be one of {sorted(ALLOWED_SCOPES)}")
    result["evidence_scope"] = scope

    status = _require_text(result.get("status"), f"{prefix}.status")
    if status not in ALLOWED_STATUSES:
        raise ValidationError(f"{prefix}.status must be one of {sorted(ALLOWED_STATUSES)}")
    result["status"] = status
    status_basis = result.get("status_basis", "unspecified")
    status_basis = _require_text(status_basis, f"{prefix}.status_basis")
    if status_basis not in ALLOWED_STATUS_BASES:
        raise ValidationError(
            f"{prefix}.status_basis must be one of {sorted(ALLOWED_STATUS_BASES)}"
        )
    result["status_basis"] = status_basis

    quantity = result.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValidationError(f"{prefix}.quantity must be a positive integer")
    result["quantity"] = quantity

    ask_price = _parse_price(result.get("ask_price"), f"{prefix}.ask_price")
    realized_price = _parse_price(result.get("realized_price"), f"{prefix}.realized_price")
    result["ask_price"] = ask_price
    result["realized_price"] = realized_price
    if realized_price is not None and status not in {"sold", "completed"}:
        raise ValidationError(
            f"{prefix}.realized_price requires status sold or completed"
        )
    if ask_price is not None or realized_price is not None:
        currency = _require_text(result.get("currency"), f"{prefix}.currency").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValidationError(f"{prefix}.currency must be a three-letter code")
        result["currency"] = currency
    elif result.get("currency") is not None:
        currency = _require_text(result.get("currency"), f"{prefix}.currency").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValidationError(f"{prefix}.currency must be a three-letter code")
        result["currency"] = currency

    for optional in (
        "seller_id",
        "campaign_key",
        "inventory_key",
        "status_evidence",
        "text",
        "condition",
    ):
        if result.get(optional) is not None:
            result[optional] = _require_text(result[optional], f"{prefix}.{optional}")
    return result


def match_observation(profile: dict[str, Any], row: dict[str, Any]) -> tuple[bool, str]:
    text = normalize_text(" ".join([row["title"], str(row.get("text", ""))]))
    for phrase in profile["exclude_any"]:
        if _contains_phrase(text, phrase):
            return False, f"excluded phrase: {phrase}"
    for group in profile["match_any"]:
        if all(_contains_phrase(text, phrase) for phrase in group):
            return True, "matched phrase group"
    return False, "no match phrase group"


def _evidence_classification(row: dict[str, Any]) -> str:
    status = row["status"]
    if status in {"sold", "completed"}:
        has_evidence = bool(row.get("status_evidence"))
        strong_scope = row["evidence_scope"] in CONFIRMABLE_SCOPES
        strong_method = row["retrieval_method"] not in WEAK_METHODS
        if (
            row["status_basis"] == "platform_marker"
            and has_evidence
            and strong_scope
            and strong_method
        ):
            return "confirmed_sold"
        if (
            row["status_basis"] == "seller_statement"
            and has_evidence
            and strong_scope
            and strong_method
        ):
            return "seller_reported_sold"
        return "weak_sold"
    if status == "pending":
        return (
            "unknown"
            if row["retrieval_method"] in WEAK_METHODS
            or row["evidence_scope"] in WEAK_SCOPES
            else "pending"
        )
    if status == "offered":
        return (
            "weak_offered"
            if row["retrieval_method"] in WEAK_METHODS
            or row["evidence_scope"] in WEAK_SCOPES
            else "offered"
        )
    return "unknown"


def _money_summary(values: Iterable[Decimal]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "values": [], "min": None, "median": None, "max": None}
    return {
        "count": len(ordered),
        "values": [f"{value:.2f}" for value in ordered],
        "min": f"{ordered[0]:.2f}",
        "median": f"{Decimal(str(median(ordered))):.2f}",
        "max": f"{ordered[-1]:.2f}",
    }


def reconcile(profile_data: Any, observations: Iterable[Any]) -> dict[str, Any]:
    profile = validate_profile(profile_data)
    rows = [validate_observation(row, index) for index, row in enumerate(observations)]

    matched: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for row in rows:
        is_match, reason = match_observation(profile, row)
        if is_match:
            matched.append(row)
        else:
            excluded.append(
                {
                    "source": row["source"],
                    "listing_id": row["listing_id"],
                    "url": row["url"],
                    "reason": reason,
                }
            )

    excluded.sort(
        key=lambda row: (
            row["source"],
            row["listing_id"],
            row["url"],
            row["reason"],
        )
    )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in matched:
        explicit = row.get("campaign_key")
        key = f"{row['source']}:{explicit or row['listing_id']}"
        grouped.setdefault(key, []).append(row)

    campaigns: list[dict[str, Any]] = []
    active_asks: dict[str, list[Decimal]] = {}
    weak_asks: dict[str, list[Decimal]] = {}
    realized_sales: dict[str, list[Decimal]] = {}
    seller_reported_sales: dict[str, list[Decimal]] = {}
    weak_sale_prices: dict[str, list[Decimal]] = {}

    for key in sorted(grouped):
        campaign_rows = sorted(
            grouped[key],
            key=lambda row: (_timestamp_sort_key(row["observed_at"]), row["listing_id"]),
        )
        sellers = {row.get("seller_id") for row in campaign_rows if row.get("seller_id")}
        if len(sellers) > 1:
            raise ValidationError(
                f"campaign {key!r} cannot merge observations from different sellers"
            )
        inventory_keys = {
            row.get("inventory_key") for row in campaign_rows if row.get("inventory_key")
        }
        if len(inventory_keys) > 1:
            raise ValidationError(
                f"campaign {key!r} cannot span different inventory keys"
            )
        inventory_key = next(iter(inventory_keys), None)
        latest_timestamp = _timestamp_sort_key(campaign_rows[-1]["observed_at"])
        tied_latest = [
            row
            for row in campaign_rows
            if _timestamp_sort_key(row["observed_at"]) == latest_timestamp
        ]
        material_states = {
            (
                row["listing_id"],
                row["url"],
                row["title"],
                row.get("seller_id"),
                row["status"],
                row["status_basis"],
                row["retrieval_method"],
                row["evidence_scope"],
                row.get("status_evidence"),
                row["quantity"],
                row["ask_price"],
                row["realized_price"],
                row.get("currency"),
            )
            for row in tied_latest
        }
        if len(material_states) > 1:
            raise ValidationError(
                f"campaign {key!r} has conflicting observations at its latest timestamp"
            )
        latest = tied_latest[-1]
        classification = _evidence_classification(latest)
        quantity = latest["quantity"]
        campaigns.append(
            {
                "campaign_key": key,
                "inventory_key": inventory_key,
                "source": latest["source"],
                "seller_id": latest.get("seller_id"),
                "latest_listing_id": latest["listing_id"],
                "latest_url": latest["url"],
                "title": latest["title"],
                "status": latest["status"],
                "status_basis": latest["status_basis"],
                "classification": classification,
                "quantity": quantity,
                "observed_at": latest["observed_at"],
                "ask_price": (
                    f"{latest['ask_price']:.2f}" if latest["ask_price"] is not None else None
                ),
                "realized_price": (
                    f"{latest['realized_price']:.2f}"
                    if latest["realized_price"] is not None
                    else None
                ),
                "currency": latest.get("currency"),
                "retrieval_method": latest["retrieval_method"],
                "evidence_scope": latest["evidence_scope"],
                "status_evidence": latest.get("status_evidence"),
                "observation_count": len(campaign_rows),
                "listing_ids": [row["listing_id"] for row in campaign_rows],
            }
        )

    inventory_groups: dict[str, list[dict[str, Any]]] = {}
    for campaign in campaigns:
        key = (
            f"inventory:{campaign['inventory_key']}"
            if campaign.get("inventory_key")
            else f"campaign:{campaign['campaign_key']}"
        )
        inventory_groups.setdefault(key, []).append(campaign)

    priority = {
        "unknown": 0,
        "weak_offered": 1,
        "offered": 2,
        "pending": 3,
        "weak_sold": 4,
        "seller_reported_sold": 5,
        "confirmed_sold": 6,
    }
    buckets = {classification: 0 for classification in priority}
    inventories: list[dict[str, Any]] = []
    for key in sorted(inventory_groups):
        inventory_campaigns = inventory_groups[key]
        quantities = {campaign["quantity"] for campaign in inventory_campaigns}
        if len(quantities) > 1:
            raise ValidationError(
                f"inventory {key!r} has inconsistent quantities across source campaigns"
            )
        quantity = next(iter(quantities))
        signals = {campaign["classification"] for campaign in inventory_campaigns}
        sold_signals = {"confirmed_sold", "seller_reported_sold", "weak_sold"}
        live_signals = {"pending", "offered", "weak_offered"}
        if signals.intersection(sold_signals) and signals.intersection(live_signals):
            classification = "unknown"
        else:
            classification = max(signals, key=priority.__getitem__)
        buckets[classification] += quantity
        if classification in {"offered", "pending"}:
            for campaign in inventory_campaigns:
                if (
                    campaign["classification"] in {"offered", "pending"}
                    and campaign["ask_price"] is not None
                ):
                    active_asks.setdefault(campaign["currency"], []).append(
                        Decimal(campaign["ask_price"])
                    )
        elif classification == "weak_offered":
            for campaign in inventory_campaigns:
                if (
                    campaign["classification"] == "weak_offered"
                    and campaign["ask_price"] is not None
                ):
                    weak_asks.setdefault(campaign["currency"], []).append(
                        Decimal(campaign["ask_price"])
                    )
        elif classification in {
            "confirmed_sold",
            "seller_reported_sold",
            "weak_sold",
        }:
            sale_prices = {
                (campaign["currency"], campaign["realized_price"])
                for campaign in inventory_campaigns
                if campaign["classification"] == classification
                and campaign["realized_price"] is not None
            }
            if len(sale_prices) > 1:
                raise ValidationError(
                    f"inventory {key!r} has inconsistent realized sale prices"
                )
            if sale_prices:
                currency, amount = next(iter(sale_prices))
                target = {
                    "confirmed_sold": realized_sales,
                    "seller_reported_sold": seller_reported_sales,
                    "weak_sold": weak_sale_prices,
                }[classification]
                target.setdefault(currency, []).append(Decimal(amount))
        inventories.append(
            {
                "inventory_key": key,
                "classification": classification,
                "signals": sorted(signals, key=priority.__getitem__, reverse=True),
                "quantity": quantity,
                "campaign_keys": [
                    campaign["campaign_key"] for campaign in inventory_campaigns
                ],
            }
        )

    physical_units = sum(inventory["quantity"] for inventory in inventories)
    return {
        "schema_version": 1,
        "product": profile,
        "totals": {
            "input_observations": len(rows),
            "matched_observations": len(matched),
            "excluded_observations": len(excluded),
            "distinct_campaigns": len(campaigns),
            "distinct_inventories": len(inventories),
            "physical_units": physical_units,
            "publicly_confirmed_sold_units": buckets["confirmed_sold"],
            "seller_reported_sold_units": buckets["seller_reported_sold"],
            "weak_sold_signal_units": buckets["weak_sold"],
            "pending_units": buckets["pending"],
            "offered_units": buckets["offered"],
            "weak_offered_signal_units": buckets["weak_offered"],
            "unknown_units": buckets["unknown"],
        },
        "prices": {
            "active_asks_by_currency": {
                currency: _money_summary(values)
                for currency, values in sorted(active_asks.items())
            },
            "weak_asks_by_currency": {
                currency: _money_summary(values)
                for currency, values in sorted(weak_asks.items())
            },
            "realized_sales_by_currency": {
                currency: _money_summary(values)
                for currency, values in sorted(realized_sales.items())
            },
            "seller_reported_sales_by_currency": {
                currency: _money_summary(values)
                for currency, values in sorted(seller_reported_sales.items())
            },
            "weak_sale_prices_by_currency": {
                currency: _money_summary(values)
                for currency, values in sorted(weak_sale_prices.items())
            },
        },
        "campaigns": campaigns,
        "inventories": inventories,
        "excluded": excluded,
        "caveat": (
            "Absence of publicly confirmed sale evidence does not prove that no private "
            "transaction occurred."
        ),
    }


def _escape_markdown(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "\\|")


def render_markdown(result: dict[str, Any]) -> str:
    totals = result["totals"]
    lines = [
        f"# Marketplace intelligence: {result['product']['name']}",
        "",
        (
            f"{totals['matched_observations']} raw matching observations → "
            f"{totals['distinct_campaigns']} campaigns → "
            f"{totals['distinct_inventories']} inventories → "
            f"{totals['physical_units']} units"
        ),
        "",
        "| Measure | Count |",
        "|---|---:|",
        f"| Input observations | {totals['input_observations']} |",
        f"| Excluded observations | {totals['excluded_observations']} |",
        f"| Publicly confirmed sold units | {totals['publicly_confirmed_sold_units']} |",
        f"| Seller-reported sold units | {totals['seller_reported_sold_units']} |",
        f"| Weak sold-signal units | {totals['weak_sold_signal_units']} |",
        f"| Pending units | {totals['pending_units']} |",
        f"| Offered units | {totals['offered_units']} |",
        f"| Weak offered-signal units | {totals['weak_offered_signal_units']} |",
        f"| Unknown units | {totals['unknown_units']} |",
        "",
        "## Campaigns",
        "",
        "| Source | Latest listing | Status | Evidence class | Qty | Ask | Realized |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for campaign in result["campaigns"]:
        currency = campaign.get("currency") or ""
        ask = f"{currency} {campaign['ask_price']}" if campaign.get("ask_price") else "—"
        realized = (
            f"{currency} {campaign['realized_price']}"
            if campaign.get("realized_price")
            else "—"
        )
        lines.append(
            "| "
            + " | ".join(
                _escape_markdown(value)
                for value in (
                    campaign["source"],
                    campaign["latest_listing_id"],
                    campaign["status"],
                    campaign["classification"],
                    campaign["quantity"],
                    ask,
                    realized,
                )
            )
            + " |"
        )
    if not result["campaigns"]:
        lines.append("| — | — | — | — | 0 | — | — |")
    lines.extend(["", f"> {result['caveat']}", ""])
    return "\n".join(lines)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON from {path}: {exc}") from exc


def _load_observations(path: Path) -> list[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot read observations from {path}: {exc}") from exc
    if path.suffix.casefold() == ".jsonl":
        rows = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValidationError(f"invalid JSONL at line {line_number}: {exc}") from exc
        return rows
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid observation JSON: {exc}") from exc
    if isinstance(value, dict) and isinstance(value.get("observations"), list):
        return value["observations"]
    if not isinstance(value, list):
        raise ValidationError("observation JSON must be a list or contain an observations list")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    reconcile_parser = subparsers.add_parser("reconcile", help="reconcile marketplace evidence")
    reconcile_parser.add_argument("--profile", type=Path, required=True)
    reconcile_parser.add_argument("--observations", type=Path, required=True)
    reconcile_parser.add_argument("--format", choices=("json", "markdown"), default="json")
    reconcile_parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    try:
        result = reconcile(_load_json(args.profile), _load_observations(args.observations))
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rendered = (
        json.dumps(result, indent=2, ensure_ascii=False)
        if args.format == "json"
        else render_markdown(result)
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
