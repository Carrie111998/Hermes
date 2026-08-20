"""Service-only candidate corpus import and campaign preselection.

Candidate datasets are deliberately not tenant-owned records.  This module is
the sole repository boundary for them: importing a dataset writes only the two
shared candidate tables and later campaign code may select from that corpus.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..db import json_dump, json_load, now
from ..quality import normalize_name


# ISO 3166-1 alpha-2 codes.
# Keeping the stable vocabulary local avoids turning a basic import validation
# into an optional runtime dependency on a country-data package.
ISO_ALPHA_2 = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL
BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV
CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD
GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM
IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK
LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW
MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR
PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS
ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY
UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())


class CandidateImportValidationError(ValueError):
    """The supplied corpus is malformed; no part of it was written."""


class CandidateImportConflict(ValueError):
    """The immutable dataset/version identity or row identity already exists."""


@dataclass(frozen=True)
class CandidateRecord:
    dataset_id: str
    version: str
    source_record_id: str
    company_name: str
    normalized_name: str
    country: str
    domain: str | None
    data: dict[str, Any]


@dataclass(frozen=True)
class CandidateImportReport:
    dataset_id: str
    version: str
    imported: int
    raw_hash: str

    @property
    def record_count(self) -> int:
        return self.imported


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _source_value(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def _as_list(value: Any, field: str, row_number: int) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(";") if item.strip()]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item.strip() for item in value if item.strip()]
    raise CandidateImportValidationError(
        f"row {row_number}: {field} must be a semicolon-separated string or list of strings"
    )


def _normalize_url(value: Any, field: str, row_number: int) -> str | None:
    text = _clean(value)
    if text is None:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CandidateImportValidationError(f"row {row_number}: {field} must be an http or https URL")
    hostname = parsed.hostname.lower().rstrip(".").removeprefix("www.")
    if not hostname:
        raise CandidateImportValidationError(f"row {row_number}: {field} must be an http or https URL")
    return hostname


def _provenance_url(value: Any, row_number: int) -> str | None:
    text = _clean(value)
    if text is None:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CandidateImportValidationError(f"row {row_number}: provenance_url must be an http or https URL")
    return text


def _read_rows(filename: str, content: bytes) -> list[tuple[int, dict[str, Any]]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CandidateImportValidationError("file must be UTF-8 encoded") from exc
    suffix = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    if suffix == "csv":
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise CandidateImportValidationError("CSV must include a header row")
        return [(number, dict(row)) for number, row in enumerate(reader, start=2)]
    if suffix == "json":
        raise CandidateImportValidationError("candidate corpora must use JSON Lines (.jsonl), not .json")
    if suffix != "jsonl":
        raise CandidateImportValidationError("only .csv and JSON Lines (.jsonl) candidate corpora are supported")
    rows: list[tuple[int, dict[str, Any]]] = []
    for row_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise CandidateImportValidationError(f"JSON Lines row {row_number} is blank")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CandidateImportValidationError(f"JSON Lines row {row_number} is not valid JSON") from exc
        if not isinstance(row, dict):
            raise CandidateImportValidationError(f"JSON Lines row {row_number} must be an object")
        rows.append((row_number, row))
    return rows


def _candidate_from_row(source: dict[str, Any], row_number: int) -> CandidateRecord:
    source_record_id = _clean(source.get("source_record_id"))
    company_name = _clean(source.get("company_name"))
    country = _clean(source.get("country"))
    if not source_record_id:
        raise CandidateImportValidationError(f"row {row_number}: source_record_id is required")
    if not company_name:
        raise CandidateImportValidationError(f"row {row_number}: company_name is required")
    country = country.upper() if country else None
    if country not in ISO_ALPHA_2:
        raise CandidateImportValidationError(f"row {row_number}: country must use an ISO alpha-2 code")
    domain = _normalize_url(source.get("domain"), "domain", row_number)
    aliases = _as_list(source.get("aliases"), "aliases", row_number)
    categories = _as_list(_source_value(source, "categories", "category"), "categories", row_number)
    buyer_types = _as_list(_source_value(source, "buyer_types", "buyer_type"), "buyer_types", row_number)
    provenance_url = _provenance_url(source.get("provenance_url"), row_number)
    known = {
        "source_record_id", "company_name", "country", "domain", "aliases", "categories", "category",
        "buyer_types", "buyer_type",
        "provenance_url",
    }
    data = {key: value for key, value in source.items() if key not in known}
    data.update({
        "aliases": aliases,
        "categories": categories,
        "buyer_types": buyer_types,
        "provenance_url": provenance_url,
    })
    return CandidateRecord(
        dataset_id="", version="", source_record_id=source_record_id, company_name=company_name,
        normalized_name=normalize_name(company_name), country=country, domain=domain, data=data,
    )


def _parse_rows(filename: str, content: bytes) -> list[CandidateRecord]:
    rows = _read_rows(filename, content)
    if not rows:
        raise CandidateImportValidationError("candidate corpus is empty")
    candidates = [_candidate_from_row(source, row_number) for row_number, source in rows]
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    seen_domains: set[str] = set()
    duplicate_domains: set[str] = set()
    seen_names: set[tuple[str, str]] = set()
    duplicate_names: set[tuple[str, str]] = set()
    for record in candidates:
        if record.source_record_id in seen_ids:
            duplicate_ids.add(record.source_record_id)
        seen_ids.add(record.source_record_id)
        if record.domain:
            if record.domain in seen_domains:
                duplicate_domains.add(record.domain)
            seen_domains.add(record.domain)
        identity = (record.normalized_name, record.country)
        if identity in seen_names:
            duplicate_names.add(identity)
        seen_names.add(identity)
    if duplicate_ids:
        raise CandidateImportConflict(
            "Duplicate source_record_id in candidate corpus: " + ", ".join(sorted(duplicate_ids))
        )
    if duplicate_domains:
        raise CandidateImportConflict(
            "Duplicate normalized domain in candidate corpus: " + ", ".join(sorted(duplicate_domains))
        )
    if duplicate_names:
        raise CandidateImportConflict(
            "Duplicate normalized company_name and country in candidate corpus: "
            + ", ".join(f"{name} ({country})" for name, country in sorted(duplicate_names))
        )
    return candidates


class CandidateRepository:
    """Repository for shared corpus data, intentionally without a company id."""

    def __init__(self, db):
        self.db = db

    def import_file(self, dataset_id: str, version: str, filename: str, content: bytes) -> CandidateImportReport:
        dataset_id, version = _clean(dataset_id), _clean(version)
        if not dataset_id or not version:
            raise CandidateImportValidationError("dataset_id and version are required")
        candidates = _parse_rows(filename, content)
        digest = hashlib.sha256(content).hexdigest()
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO candidate_datasets("
                    "dataset_id,version,source_filename,raw_hash,imported_at,record_count) "
                    "VALUES(?,?,?,?,?,?)",
                    (dataset_id, version, filename, digest, now(), len(candidates)),
                )
                for record in candidates:
                    conn.execute(
                        "INSERT INTO candidate_records("
                        "dataset_id,version,source_record_id,company_name,normalized_name,country,domain,data) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (dataset_id, version, record.source_record_id, record.company_name, record.normalized_name,
                         record.country, record.domain, json_dump(record.data)),
                    )
        except CandidateImportConflict:
            raise
        except Exception as exc:
            # A concurrent insert can only be a dataset/version or record-key
            # collision.  The transaction rollback preserves atomic rejection.
            message = str(exc).lower()
            if "unique" in message or "duplicate" in message:
                raise CandidateImportConflict("candidate dataset/version is immutable and already exists") from exc
            raise
        return CandidateImportReport(dataset_id, version, len(candidates), digest)

    def select(
        self,
        *,
        countries: list[str],
        product_terms: list[str],
        limit: int,
        exclude: set[tuple[str, str]] | None = None,
    ) -> list[CandidateRecord]:
        """Pick candidates to verify.

        ``exclude`` holds (normalized_name, country) identities the caller has
        already settled — validated recently, or closed. It is passed in rather
        than queried here because this corpus is shared across tenants and has
        no company_id: whose work is already done is a tenant question. The
        filter runs before ``limit`` so a run still gets a full batch of
        unsettled candidates instead of a page mostly spent on skips.
        """
        if limit < 1:
            return []
        skip = exclude or set()
        normalized_countries = {str(value).strip().upper() for value in countries if str(value).strip()}
        invalid_countries = normalized_countries - ISO_ALPHA_2
        if invalid_countries:
            raise CandidateImportValidationError("countries must use ISO alpha-2 codes")
        query = (
            "SELECT dataset_id,version,source_record_id,company_name,normalized_name,country,domain,data "
            "FROM candidate_records"
        )
        params: tuple[Any, ...] = ()
        if normalized_countries:
            placeholders = ",".join("?" for _ in normalized_countries)
            query += f" WHERE country IN ({placeholders})"
            params = tuple(sorted(normalized_countries))
        query += " ORDER BY dataset_id,version,source_record_id"
        terms = [normalize_name(str(value)) for value in product_terms if str(value).strip()]
        results: list[CandidateRecord] = []
        for row in self.db.all(query, params):
            data = json_load(row["data"], {})
            searchable = " ".join([
                row["normalized_name"],
                *[normalize_name(value) for value in data.get("aliases", [])],
                *[normalize_name(value) for value in data.get("categories", [])],
            ])
            if terms and not all(term in searchable for term in terms):
                continue
            if (row["normalized_name"], (row["country"] or "").upper()) in skip:
                continue
            results.append(CandidateRecord(
                dataset_id=row["dataset_id"], version=row["version"], source_record_id=row["source_record_id"],
                company_name=row["company_name"], normalized_name=row["normalized_name"], country=row["country"],
                domain=row["domain"], data=data,
            ))
            if len(results) == limit:
                break
        return results
