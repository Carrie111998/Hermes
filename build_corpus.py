#!/usr/bin/env python3
"""Turn the raw customer list into an importable candidate corpus.

The raw file is a contact list: every row carries a person's name, title,
email and phone. `candidate_records` has no company_id and is shared across
tenants, and unknown columns are kept verbatim in its `data` blob, so this
script emits company identity ONLY. Contacts are tenant data and belong in
`contacts` (see STATUS-AND-PLAN.md item 4), never here.

    python build_corpus.py "customer list - KitchenAppliancesCustomerData.csv" corpus.csv
    python -m server import-candidates --dataset-id kitchen-appliances --version 1 --file corpus.csv
"""
from __future__ import annotations

import csv
import hashlib
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.quality import normalize_name

# Hand-written because the raw file uses trade names and misspellings that no
# ISO lookup resolves ("Malasia", "Krgyzistan", "Venezuella"). A country-data
# package would still need every one of these aliases spelled out.
ISO = {
    "afganistan": "AF", "albania": "AL", "algeria": "DZ", "angola": "AO",
    "argentina": "AR", "armenia": "AM", "australia": "AU", "azerbaijan": "AZ",
    "bahrain": "BH", "bangladesh": "BD", "barbados": "BB", "belarus": "BY",
    "belgium": "BE", "belize": "BZ", "benin": "BJ", "bhutan": "BT",
    "bolivia": "BO", "bosnia": "BA", "bosnia-herzegovina": "BA",
    "botswana": "BW", "brazil": "BR", "brunei": "BN", "bulgaria": "BG",
    "burkina faso": "BF", "burundi": "BI", "cambodia": "KH", "cameroon": "CM",
    "canada": "CA", "chile": "CL", "china": "CN", "colombia": "CO",
    "congo": "CG", "costa rica": "CR", "cote d'ivoire": "CI",
    "côte d'ivoire": "CI", "ivory coast": "CI", "croatia": "HR",
    "cyprus": "CY", "czechia": "CZ", "denmark": "DK", "djibuti": "DJ",
    "dominican republic": "DO", "ecuador": "EC", "egypt": "EG",
    "el salvador": "SV", "estonia": "EE", "ethiopa": "ET", "ethiopia": "ET",
    "fiji": "FJ", "finland": "FI", "france": "FR", "gabon": "GA",
    "gambia": "GM", "georgia": "GE", "germany": "DE", "ghana": "GH",
    "greece": "GR", "grenada": "GD", "guatemala": "GT", "guinea": "GN",
    "guyana": "GY", "haiti": "HT", "honduras": "HN", "hong kong": "HK",
    "hungary": "HU", "iceland": "IS", "india": "IN", "indonesia": "ID",
    "iran": "IR", "iraq": "IQ", "ireland": "IE", "jamaica": "JM",
    "jordan": "JO", "kazakhstan": "KZ", "kenya": "KE", "krgyzistan": "KG",
    "kuwait": "KW", "latvia": "LV", "lebanon": "LB", "lesotho": "LS",
    "liberia": "LR", "libya": "LY", "lithuania": "LT", "luxembourg": "LU",
    "macedonia": "MK", "madagascar": "MG", "malasia": "MY", "malawi": "MW",
    "malaysia": "MY", "maldives": "MV", "mali": "ML", "malta": "MT",
    "mauritania": "MR", "mauritius": "MU", "mayotte": "YT", "mexico": "MX",
    "moldova": "MD", "mongolia": "MN", "montenegro": "ME", "morocco": "MA",
    "mozambique": "MZ", "myanmar": "MM", "namibia": "NA", "nepal": "NP",
    "netherlands": "NL", "new caledonia": "NC", "new zealand": "NZ",
    "niger": "NE", "nigeria": "NG", "norway": "NO", "oman": "OM",
    "pakistan": "PK", "panama": "PA", "paraguay": "PY", "peru": "PE",
    "philippines": "PH", "poland": "PL", "portugal": "PT", "qatar": "QA",
    "romania": "RO", "russia": "RU", "rwanda": "RW", "saint lucia": "LC",
    "sao tome": "ST", "saudi arabia": "SA", "senegal": "SN", "serbia": "RS",
    "sierra leone": "SL", "singapore": "SG", "slovenia": "SI", "somalia": "SO",
    "south africa": "ZA", "spain": "ES", "sri lanka": "LK", "sudan": "SD",
    "sweden": "SE", "switzerland": "CH", "syria": "SY", "taiwan": "TW",
    "tajikistan": "TJ", "tanzania": "TZ", "thailand": "TH", "togo": "TG",
    "trinidad & tobago": "TT", "tunisia": "TN", "uganda": "UG",
    "ukraine": "UA", "united arab emirates": "AE", "united kingdom": "GB",
    "uruguay": "UY", "uzbekistan": "UZ", "vanuatu": "VU", "venezuela": "VE",
    "venezuella": "VE", "vietnam": "VN", "yemen": "YE", "zambia": "ZM",
    "zimbabwe": "ZW",
}

# Dropped on purpose. Kosovo's XK is user-assigned, not ISO 3166-1, so the
# importer's ISO_ALPHA_2 set rejects it; the rest are regions, not countries.
UNMAPPED = {"kosovo", "caribbean", "middle asia", "west indies"}


def main(source: Path, out: Path) -> None:
    rows, dropped = {}, Counter()
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("Company Name") or "").strip()
            country = (row.get("Country") or "").strip().casefold()
            if not name:
                dropped["blank company name"] += 1
                continue
            if country in UNMAPPED:
                dropped[f"no alpha-2 code: {country}"] += 1
                continue
            code = ISO.get(country)
            if not code:
                dropped[f"UNKNOWN COUNTRY: {country}"] += 1
                continue
            # The importer raises on a repeated (normalized_name, country)
            # rather than deduping, so collapse them here. Same company, same
            # market, different contact person — that is the whole duplicate set.
            key = (normalize_name(name), code)
            if key in rows:
                dropped["duplicate company+country"] += 1
                continue
            rows[key] = {
                "source_record_id": hashlib.sha256(
                    f"{key[0]}|{code}".encode()
                ).hexdigest()[:24],
                "company_name": name,
                "country": code,
                "categories": "kitchen-appliances",
            }

    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["source_record_id", "company_name", "country", "categories"]
        )
        writer.writeheader()
        writer.writerows(rows.values())

    print(f"kept {len(rows)} -> {out}")
    for reason, count in sorted(dropped.items()):
        print(f"  dropped {count}: {reason}")
    unknown = [r for r in dropped if r.startswith("UNKNOWN")]
    if unknown:
        raise SystemExit(f"unmapped country names, add them to ISO: {unknown}")


def self_check() -> None:
    """python build_corpus.py --self-check"""
    import tempfile

    raw = (
        "Country,Company Name,Name,Title,Primary Email\n"
        "Malasia,Acme Sdn Bhd,Jane Roe,Buyer,jane@acme.example\n"
        "Malaysia,ACME SDN BHD,John Doe,Head,john@acme.example\n"   # dupe after normalize
        "Kosovo,Pristina Trading,A B,Buyer,ab@x.example\n"          # XK is not ISO 3166-1
        "Germany,,C D,Buyer,cd@x.example\n"                         # blank company
        "Venezuella,Caracas Import,E F,Buyer,ef@x.example\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        source, out = Path(tmp) / "in.csv", Path(tmp) / "out.csv"
        source.write_text(raw, encoding="utf-8")
        main(source, out)
        rows = list(csv.DictReader(out.open(encoding="utf-8")))

    assert len(rows) == 2, rows
    assert {r["country"] for r in rows} == {"MY", "VE"}, rows
    # The whole point of this script: no person survives the trip.
    out_text = ",".join(sorted(rows[0]) + [v for r in rows for v in r.values()])
    for leaked in ("Jane", "Roe", "@acme.example", "Buyer", "Title"):
        assert leaked not in out_text, f"PII leaked: {leaked}"
    # Same input must give the same ids, or a re-import stops being idempotent.
    assert rows[0]["source_record_id"] == rows[0]["source_record_id"]
    assert all(len(r["source_record_id"]) == 24 for r in rows), rows
    print("self-check ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-check"]:
        self_check()
    else:
        main(Path(sys.argv[1]), Path(sys.argv[2]))
