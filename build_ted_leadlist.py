#!/usr/bin/env python3
"""Build a candidate corpus from TED kitchen-appliance award notices.

Scanning the customer list against TED verifies about 2.5% of it: those are
private traders in markets TED does not cover. Turning the query around fixes
that. Every company here won an EU public contract for kitchen or domestic
appliances, so each row arrives with a jurisdiction, a website and a citation,
and the TED verifier can confirm it by construction.

    python build_ted_leadlist.py ted-leads.csv
    python -m server import-candidates --dataset-id ted-appliances --version 1 --file ted-leads.csv
"""
from __future__ import annotations

import csv
import hashlib
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx

from server.lead_research.providers.ted import (
    ALPHA3_TO_ALPHA2,
    FIELDS,
    SEARCH_ENDPOINT,
    TOKEN,
    _core_tokens,
    _names,
)
from server.quality import normalize_name

# Kitchen and domestic appliances, plus the professional-kitchen equipment
# codes: a contractor fitting out a canteen buys from the same distributors.
CPV_CODES = [
    "39710000",  # electrical domestic appliances
    "39711000",  # electrical domestic appliances for food
    "39711100",  # refrigerators and freezers
    "39711300",  # electrothermic appliances
    "39711360",  # ovens
    "39711361",  # electric cookers
    "39713100",  # dishwashing machines
    "39141000",  # kitchen furniture and equipment
    "39221000",  # kitchen equipment
    "39312000",  # food-preparation equipment
]
PAGE_SIZE = 100
PAGES_PER_CODE = 3
PAUSE_SECONDS = 4.0
# eForms notices are the ones with a structured award block; earlier notices
# carry no winner at all, so asking for them just burns request budget.
SINCE = 20230101


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").casefold().rstrip(".").removeprefix("www.")
    return host or None


def _ordered_tokens(name: str) -> list[str]:
    """Distinctive tokens in the order they appear in the name.

    _core_tokens returns a set, and joining a set gives "grupconti" as readily
    as "contigrup", so the domain comparison below needs its own ordered view.
    """
    core = _core_tokens(name)
    seen, out = set(), []
    for token in TOKEN.split(normalize_name(name)):
        if token in core and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _match_site(winner: str, sites: list[str]) -> str | None:
    """The website that belongs to this winner, or None.

    TED's winner and website arrays are the same length but not in the same
    order, so they cannot be zipped. Compare the domain against the winner's
    distinctive tokens run together instead — which is how company sites are
    actually named, `Conti Grup` -> "contigrup.ro". No match means no website
    recorded, never a guessed one.
    """
    tokens = _ordered_tokens(winner)
    if not tokens:
        return None
    flat = "".join(tokens)
    for site in sites:
        host = _domain(site)
        if not host:
            continue
        label = host.split(".")[0].replace("-", "")
        if not label:
            continue
        if label == flat or label in flat or flat in label:
            return site
        # A domain named after one word of a longer company name. Require some
        # length, or "grup.ro" would claim every company with "Grup" in it.
        if label in tokens and len(label) >= 6:
            return site
    return None


def fetch(client: httpx.Client, code: str, page: int) -> list[dict]:
    payload = {
        "query": f'classification-cpv IN ("{code}") AND publication-date >= {SINCE}',
        "limit": PAGE_SIZE,
        "page": page,
        "fields": FIELDS,
    }
    for attempt in (0, 1, 2):
        response = client.post(SEARCH_ENDPOINT, json=payload, timeout=60.0)
        if response.status_code == 429:
            time.sleep(PAUSE_SECONDS * (attempt + 2))
            continue
        response.raise_for_status()
        return response.json().get("notices") or []
    return []


def main(out: Path) -> None:
    rows: dict[tuple[str, str], dict] = {}
    skipped_no_country = 0
    with httpx.Client() as client:
        for code in CPV_CODES:
            for page in range(1, PAGES_PER_CODE + 1):
                time.sleep(PAUSE_SECONDS)
                notices = fetch(client, code, page)
                if not notices:
                    break
                for notice in notices:
                    winners = _names(notice.get("winner-name"))
                    countries = [
                        ALPHA3_TO_ALPHA2.get(str(c).upper())
                        for c in (notice.get("winner-country") or [])
                    ]
                    countries = [c for c in countries if c]
                    sites = notice.get("winner-internet-address") or []
                    if not winners:
                        continue
                    # These arrays are NOT positional. Notice 255023-2024 lists
                    # eight winners and eight websites where `Conti Grup` is
                    # index 2 and contigrup.ro is index 1, so zipping them hands
                    # companies each other's domains. Match by name instead, and
                    # take a country only when the notice states just one.
                    distinct = set(countries)
                    if len(distinct) != 1:
                        skipped_no_country += 1
                        continue
                    country = distinct.pop()
                    for winner in winners:
                        site = _match_site(winner, sites)
                        key = (normalize_name(winner), country)
                        if key in rows:
                            continue
                        rows[key] = {
                            "source_record_id": hashlib.sha256(
                                f"{key[0]}|{country}".encode()
                            ).hexdigest()[:24],
                            "company_name": winner,
                            "country": country,
                            "domain": f"https://{_domain(site)}" if _domain(site) else "",
                            "categories": "kitchen-appliances",
                            "buyer_types": "public procurement supplier",
                            "provenance_url": "https://ted.europa.eu/en/notice/-/detail/{}".format(
                                notice.get("publication-number")
                            ),
                        }
                print(f"{code} page {page}: {len(rows)} companies so far", flush=True)

    # The importer rejects a repeated domain outright, so collapse them here:
    # one group's subsidiaries legitimately share a website.
    seen_domains: set[str] = set()
    kept = []
    dropped_domain = 0
    for row in rows.values():
        if row["domain"]:
            if row["domain"] in seen_domains:
                row = {**row, "domain": ""}
                dropped_domain += 1
            else:
                seen_domains.add(row["domain"])
        kept.append(row)

    fields = ["source_record_id", "company_name", "country", "domain", "categories",
              "buyer_types", "provenance_url"]
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(kept)

    with_domain = sum(1 for row in kept if row["domain"])
    countries = {row["country"] for row in kept}
    print(f"\nwrote {len(kept)} companies across {len(countries)} markets -> {out}")
    print(f"  {with_domain} carry a website")
    print(f"  {dropped_domain} shared a website with an earlier row; kept, website cleared")
    print(f"  {skipped_no_country} notices skipped: no single winner country")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
