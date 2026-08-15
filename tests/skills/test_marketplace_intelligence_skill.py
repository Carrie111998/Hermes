from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "skills"
    / "research"
    / "marketplace-intelligence"
    / "scripts"
    / "marketplace_intel.py"
)
SPEC = importlib.util.spec_from_file_location("marketplace_intel", SCRIPT)
marketplace_intel = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(marketplace_intel)


def observation(
    listing_id: str,
    title: str,
    *,
    source: str = "reddit",
    seller_id: str = "seller-a",
    observed_at: str = "2026-08-15T12:00:00Z",
    retrieval_method: str = "live_page",
    evidence_scope: str = "full_page",
    status: str = "offered",
    quantity: int = 1,
    ask_price: str | None = None,
    realized_price: str | None = None,
    campaign_key: str | None = None,
    inventory_key: str | None = None,
    status_evidence: str | None = None,
    status_basis: str | None = None,
) -> dict:
    row = {
        "source": source,
        "listing_id": listing_id,
        "url": f"https://example.com/{source}/{listing_id}",
        "title": title,
        "seller_id": seller_id,
        "observed_at": observed_at,
        "retrieval_method": retrieval_method,
        "evidence_scope": evidence_scope,
        "status": status,
        "quantity": quantity,
        "currency": "USD",
    }
    if ask_price is not None:
        row["ask_price"] = ask_price
    if realized_price is not None:
        row["realized_price"] = realized_price
    if campaign_key is not None:
        row["campaign_key"] = campaign_key
    if inventory_key is not None:
        row["inventory_key"] = inventory_key
    if status_evidence is not None:
        row["status_evidence"] = status_evidence
    if status_basis is not None:
        row["status_basis"] = status_basis
    return row


def test_rtx_profile_excludes_full_power_and_wrong_generation() -> None:
    profile = {
        "name": "NVIDIA RTX PRO 6000 Blackwell Max-Q",
        "match_any": [
            ["RTX PRO 6000", "Blackwell", "Max-Q"],
            ["900-5G153-2500-000"],
        ],
        "exclude_any": ["Workstation Edition", "Ada Generation"],
    }
    rows = [
        observation("maxq", "NVIDIA RTX PRO 6000 Blackwell Max-Q 96GB", ask_price="13999"),
        observation("mpn", "NVIDIA 900-5G153-2500-000 96 GB GPU", ask_price="14500"),
        observation("full", "RTX PRO 6000 Blackwell Workstation Edition 96GB"),
        observation("ada", "RTX 6000 Ada Generation 48GB"),
    ]

    result = marketplace_intel.reconcile(profile, rows)

    assert result["totals"] == {
        "input_observations": 4,
        "matched_observations": 2,
        "excluded_observations": 2,
        "distinct_campaigns": 2,
        "distinct_inventories": 2,
        "physical_units": 2,
        "publicly_confirmed_sold_units": 0,
        "seller_reported_sold_units": 0,
        "weak_sold_signal_units": 0,
        "pending_units": 0,
        "offered_units": 2,
        "weak_offered_signal_units": 0,
        "unknown_units": 0,
    }
    assert result["prices"]["active_asks_by_currency"]["USD"]["count"] == 2
    assert {row["listing_id"] for row in result["excluded"]} == {"full", "ada"}


def test_excluded_observations_are_input_order_independent() -> None:
    profile = {
        "name": "Original PS5 disc",
        "match_any": [["PS5", "disc"]],
        "exclude_any": ["Slim", "Digital"],
    }
    slim = observation("z-slim", "PS5 Slim disc console", source="ebay")
    digital = observation("a-digital", "PS5 Digital console", source="reddit")

    forward = marketplace_intel.reconcile(profile, [slim, digital])
    reverse = marketplace_intel.reconcile(profile, [digital, slim])

    assert forward["excluded"] == reverse["excluded"]
    assert [row["listing_id"] for row in forward["excluded"]] == [
        "z-slim",
        "a-digital",
    ]


def test_dgx_reposts_do_not_inflate_campaigns_or_units() -> None:
    profile = {
        "name": "NVIDIA DGX Spark",
        "match_any": [["DGX Spark"], ["MSI EdgeXpert"]],
        "exclude_any": ["DGX Station"],
    }
    rows = [
        observation(
            "nv-first",
            "[US-NV] Two NVIDIA DGX Spark systems",
            campaign_key="nv-pair",
            quantity=2,
            ask_price="9000",
            observed_at="2026-08-12T12:00:00Z",
        ),
        observation(
            "nv-repost",
            "[US-NV] 2x NVIDIA DGX Spark with connect cable",
            campaign_key="nv-pair",
            quantity=2,
            ask_price="8500",
            observed_at="2026-08-12T12:07:00Z",
        ),
        observation(
            "me-first",
            "MSI EdgeXpert DGX Spark platform",
            campaign_key="me-single",
            ask_price="4200",
            observed_at="2026-07-17T10:00:00Z",
        ),
        observation(
            "me-repost",
            "MSI EdgeXpert based on NVIDIA DGX Spark",
            campaign_key="me-single",
            ask_price="4000",
            observed_at="2026-08-01T10:00:00Z",
        ),
        observation(
            "tx-pair",
            "[US-TX] Pair of NVIDIA DGX Spark systems",
            campaign_key="tx-pair",
            quantity=2,
            ask_price="7600",
            observed_at="2026-07-20T10:00:00Z",
        ),
        observation(
            "tx-repost",
            "[US-TX] 2 DGX Spark price reduced",
            campaign_key="tx-pair",
            quantity=2,
            ask_price="7100",
            observed_at="2026-07-22T10:00:00Z",
        ),
    ]

    result = marketplace_intel.reconcile(profile, rows)

    assert result["totals"]["matched_observations"] == 6
    assert result["totals"]["distinct_campaigns"] == 3
    assert result["totals"]["distinct_inventories"] == 3
    assert result["totals"]["physical_units"] == 5
    assert result["totals"]["publicly_confirmed_sold_units"] == 0
    assert result["prices"]["active_asks_by_currency"]["USD"]["values"] == [
        "4000.00",
        "7100.00",
        "8500.00",
    ]


def test_m3_max_128gb_profile_rejects_capacity_and_generation_near_matches() -> None:
    profile = {
        "name": "Apple MacBook Pro M3 Max 128GB",
        "match_any": [["MacBook Pro", "M3 Max", "128 GB"]],
        "exclude_any": ["M4 Max", "64 GB", "96 GB"],
    }
    rows = [
        observation("exact", 'Apple MacBook Pro 16-inch M3 Max 128GB 2TB'),
        observation("m4", 'Apple MacBook Pro 16-inch M4 Max 128 GB 2TB'),
        observation("64", 'Apple MacBook Pro M3 Max 64GB 2TB'),
        observation("desktop", 'Mac Studio M3 Ultra 128GB'),
    ]

    result = marketplace_intel.reconcile(profile, rows)

    assert result["totals"]["matched_observations"] == 1
    assert result["campaigns"][0]["latest_listing_id"] == "exact"


def test_ps5_profile_does_not_collapse_slim_digital_or_pro_variants() -> None:
    profile = {
        "name": "Sony PlayStation 5 original disc console",
        "match_any": [["PlayStation 5", "disc"], ["PS5", "disc"]],
        "exclude_any": [
            "Slim",
            "Digital Edition",
            "PS5 Pro",
            "for parts",
            "disc drive",
            "controller only",
        ],
    }
    rows = [
        observation("original", "Sony PS5 PlayStation 5 original disc console CFI-1015A"),
        observation("slim", "Sony PlayStation 5 Slim disc console CFI-2015"),
        observation("digital", "Sony PS5 Digital Edition console"),
        observation("pro", "Sony PlayStation 5 Pro console"),
        observation("parts", "PS5 disc console for parts or repair"),
        observation("accessory", "Sony PS5 disc drive accessory"),
    ]

    result = marketplace_intel.reconcile(profile, rows)

    assert result["totals"]["matched_observations"] == 1
    assert result["campaigns"][0]["latest_listing_id"] == "original"


def test_search_snippet_sold_signal_is_not_publicly_confirmed() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    rows = [
        observation(
            "snippet",
            "PS5 disc console sold",
            retrieval_method="search_index",
            evidence_scope="snippet",
            status="sold",
            realized_price="350",
            status_evidence="Search result contains the word sold",
        )
    ]

    result = marketplace_intel.reconcile(profile, rows)

    assert result["totals"]["publicly_confirmed_sold_units"] == 0
    assert result["totals"]["weak_sold_signal_units"] == 1
    assert result["prices"]["realized_sales_by_currency"] == {}
    assert result["prices"]["weak_sale_prices_by_currency"]["USD"]["values"] == [
        "350.00"
    ]


def test_live_sold_marker_with_evidence_is_confirmed() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    rows = [
        observation(
            "sold",
            "PS5 disc console",
            source="ebay",
            evidence_scope="listing_card",
            status="completed",
            realized_price="375",
            status_evidence="Completed listing card displays Sold",
            status_basis="platform_marker",
        )
    ]

    result = marketplace_intel.reconcile(profile, rows)

    assert result["totals"]["publicly_confirmed_sold_units"] == 1
    assert result["prices"]["realized_sales_by_currency"]["USD"]["values"] == ["375.00"]


def test_seller_statement_is_not_a_publicly_confirmed_sale() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    row = observation(
        "seller-report",
        "PS5 disc console",
        status="sold",
        realized_price="340",
        status_evidence="Seller wrote that payment and handoff were completed",
        status_basis="seller_statement",
    )

    result = marketplace_intel.reconcile(profile, [row])

    assert result["totals"]["publicly_confirmed_sold_units"] == 0
    assert result["totals"]["seller_reported_sold_units"] == 1
    assert result["prices"]["seller_reported_sales_by_currency"]["USD"]["values"] == [
        "340.00"
    ]
    assert result["prices"]["realized_sales_by_currency"] == {}


def test_search_snippet_offer_is_not_active_inventory() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    row = observation(
        "snippet-offer",
        "PS5 disc console $350",
        retrieval_method="search_snippet",
        evidence_scope="snippet",
        ask_price="350",
    )

    result = marketplace_intel.reconcile(profile, [row])

    assert result["totals"]["offered_units"] == 0
    assert result["totals"]["weak_offered_signal_units"] == 1
    assert result["prices"]["active_asks_by_currency"] == {}
    assert result["prices"]["weak_asks_by_currency"]["USD"]["values"] == ["350.00"]


def test_prices_are_never_aggregated_across_currencies() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    usd = observation("usd", "PS5 disc console", ask_price="350")
    cad = observation("cad", "PS5 disc console", ask_price="450")
    cad["currency"] = "CAD"

    result = marketplace_intel.reconcile(profile, [usd, cad])

    assert result["prices"]["active_asks_by_currency"]["USD"]["values"] == ["350.00"]
    assert result["prices"]["active_asks_by_currency"]["CAD"]["values"] == ["450.00"]


def test_campaign_key_cannot_merge_different_sellers() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    first = observation("one", "PS5 disc console", campaign_key="same")
    second = observation("two", "PS5 disc console", campaign_key="same", seller_id="seller-b")

    with pytest.raises(marketplace_intel.ValidationError, match="different sellers"):
        marketplace_intel.reconcile(profile, [first, second])


def test_conflicting_tied_campaign_observations_fail_closed() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    first = observation("a", "PS5 disc console", campaign_key="same", ask_price="350")
    second = observation("b", "PS5 disc console", campaign_key="same", ask_price="375")

    with pytest.raises(marketplace_intel.ValidationError, match="latest timestamp"):
        marketplace_intel.reconcile(profile, [first, second])

    with pytest.raises(marketplace_intel.ValidationError, match="latest timestamp"):
        marketplace_intel.reconcile(profile, [second, first])


def test_tied_campaign_evidence_strength_cannot_change_result_by_input_order() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    native = observation(
        "same",
        "PS5 disc console",
        campaign_key="same",
        status="sold",
        status_basis="platform_marker",
        status_evidence="Native card says Sold",
    )
    snippet = dict(native)
    snippet["retrieval_method"] = "search_snippet"
    snippet["evidence_scope"] = "snippet"

    for rows in ([native, snippet], [snippet, native]):
        with pytest.raises(marketplace_intel.ValidationError, match="latest timestamp"):
            marketplace_intel.reconcile(profile, rows)


def test_tied_campaign_display_fields_cannot_change_output_by_input_order() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    first = observation("same", "PS5 console with controller", campaign_key="same")
    second = observation("same", "PS5 console excellent condition", campaign_key="same")
    second["url"] = "https://example.com/revised/same"

    for rows in ([first, second], [second, first]):
        with pytest.raises(marketplace_intel.ValidationError, match="latest timestamp"):
            marketplace_intel.reconcile(profile, rows)


def test_tied_campaign_seller_presence_cannot_change_output_by_input_order() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    identified = observation("same", "PS5 disc console", campaign_key="same")
    anonymous = dict(identified)
    anonymous.pop("seller_id")

    for rows in ([identified, anonymous], [anonymous, identified]):
        with pytest.raises(marketplace_intel.ValidationError, match="latest timestamp"):
            marketplace_intel.reconcile(profile, rows)


def test_cross_source_inventory_key_prevents_unit_double_counting() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    reddit = observation(
        "reddit-one",
        "PS5 disc console",
        source="reddit",
        inventory_key="serial-safe-item-a",
        ask_price="350",
    )
    ebay = observation(
        "ebay-one",
        "PS5 disc console",
        source="ebay",
        inventory_key="serial-safe-item-a",
        ask_price="375",
    )

    result = marketplace_intel.reconcile(profile, [reddit, ebay])

    assert result["totals"]["distinct_campaigns"] == 2
    assert result["totals"]["distinct_inventories"] == 1
    assert result["totals"]["physical_units"] == 1
    assert result["totals"]["offered_units"] == 1


def test_conflicting_cross_source_sale_and_offer_becomes_unknown() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    offered = observation(
        "reddit-one",
        "PS5 disc console",
        source="reddit",
        inventory_key="serial-safe-item-a",
        ask_price="350",
    )
    sold = observation(
        "ebay-one",
        "PS5 disc console",
        source="ebay",
        inventory_key="serial-safe-item-a",
        status="sold",
        realized_price="340",
        status_evidence="Native completed listing card displays Sold",
        status_basis="platform_marker",
    )

    result = marketplace_intel.reconcile(profile, [offered, sold])

    assert result["totals"]["publicly_confirmed_sold_units"] == 0
    assert result["totals"]["offered_units"] == 0
    assert result["totals"]["unknown_units"] == 1
    assert result["prices"]["active_asks_by_currency"] == {}
    assert result["prices"]["realized_sales_by_currency"] == {}


def test_weak_sold_signal_conflicting_with_offer_becomes_unknown() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    offered = observation(
        "reddit-one",
        "PS5 disc console",
        source="reddit",
        inventory_key="serial-safe-item-a",
        ask_price="350",
    )
    weak = observation(
        "search-one",
        "PS5 disc console sold",
        source="search",
        inventory_key="serial-safe-item-a",
        retrieval_method="search_index",
        evidence_scope="snippet",
        status="sold",
        status_evidence="Indexed excerpt contains sold",
    )

    result = marketplace_intel.reconcile(profile, [offered, weak])

    assert result["totals"]["unknown_units"] == 1
    assert result["totals"]["offered_units"] == 0
    assert result["totals"]["weak_sold_signal_units"] == 0
    assert result["inventories"][0]["signals"] == ["weak_sold", "offered"]


def test_realized_price_requires_sold_or_completed_status() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    row = observation("one", "PS5 disc console", realized_price="350")

    with pytest.raises(marketplace_intel.ValidationError, match="realized_price"):
        marketplace_intel.reconcile(profile, [row])


def test_removed_listing_remains_unknown() -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    result = marketplace_intel.reconcile(
        profile,
        [observation("gone", "PS5 disc console", status="removed")],
    )

    assert result["totals"]["unknown_units"] == 1
    assert result["totals"]["publicly_confirmed_sold_units"] == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("url", "javascript:alert(1)", "url"),
        ("observed_at", "2026-08-15T12:00:00", "timezone"),
        ("quantity", 0, "quantity"),
        ("currency", "US", "currency"),
        ("ask_price", "free", "ask_price"),
        ("ask_price", "0.001", "ask_price"),
        ("ask_price", "1e999", "ask_price"),
    ],
)
def test_invalid_observations_fail_closed(field: str, value: object, message: str) -> None:
    profile = {"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}
    row = observation("bad", "PS5 disc console", ask_price="350")
    row[field] = value

    with pytest.raises(marketplace_intel.ValidationError, match=message):
        marketplace_intel.reconcile(profile, [row])


def test_cli_reads_jsonl_and_emits_markdown(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    rows_path = tmp_path / "observations.jsonl"
    profile_path.write_text(
        json.dumps({"name": "PS5", "match_any": [["PS5"]], "exclude_any": []}),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(observation("one", "PS5 disc console", ask_price="350")) + "\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "reconcile",
            "--profile",
            str(profile_path),
            "--observations",
            str(rows_path),
            "--format",
            "markdown",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "1 raw matching observations → 1 campaigns → 1 inventories → 1 units" in completed.stdout
    assert "Publicly confirmed sold units | 0" in completed.stdout
