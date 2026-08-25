#!/usr/bin/env python3
"""Exercise a provisioned, empty Interfaze tenant over its public HTTP API.

The default mode is read-only and never changes the target tenant. The script
never accepts a password value on the command line. Candidate data is loaded
separately with ``python -m server import-candidates`` because it is a private
backend corpus, not tenant or WebUI data. Full mode is destructive release
rehearsal for an explicitly confirmed disposable tenant only.
"""
from __future__ import annotations

import argparse
import json
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OPERATIONAL_COLLECTIONS = (
    "/api/v1/leads",
    "/api/v1/contacts",
    "/api/v1/lead-map/selected-countries",
    "/api/v1/research",
    "/api/v1/research-campaigns",
    "/api/v1/outreach/campaigns",
)
INITIAL_EMPTY_COLLECTIONS = ("/api/v1/products", *OPERATIONAL_COLLECTIONS)
PROTECTED_DEMO_EMAILS = frozenset({"efe@anexa-arelvia.com"})

# The sanitized acceptance corpus: five markets, twenty curated buyer rows, and
# the exclusion cases. It carries the same shape as the real kitchen-appliance
# export and none of its content — no person, no real domain.
FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "server" / "fixtures" / "curated_appliance_buyers.jsonl"
)
# The same assertion an operator files for the real corpus. Kept beside the
# fixture rather than derived from it, because a manifest the test computed from
# the rows would prove nothing about the rows.
ACCEPTANCE_MANIFEST = {
    "purpose": "curated_buyers",
    "asserted_fields": [
        "company_identity", "target_presence",
        "product_sector_relevance", "buyer_membership",
    ],
    "sector_ids": ["household-appliances"],
    "product_terms": [],
    "publisher_label": "Acceptance appliance buyer list",
    "curated_at": 1787616000.0,
    "freshness_unknown": False,
    "curation_note": "Fictional company-only acceptance corpus; no contact columns.",
}
ACCEPTANCE_MARKETS = ("DE", "ES", "FR", "PL", "RO")
# Published contract, restated here so the acceptance gate fails if the engine
# quietly changes it.
RESULT_TARGET_MIN = 5
RESULT_LIMIT = 15


def load_acceptance_corpus() -> tuple[bytes, bytes]:
    """The fixture split into its manifest-bearing and legacy datasets.

    Two datasets because the distinction is the point: a curated buyer list is
    evidence for its rows, and a list nobody made an assertion about is not, so
    the same run has to contain both to prove the boundary holds.
    """
    curated: list[str] = []
    legacy: list[str] = []
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        target = curated if entry["dataset"] == "curated" else legacy
        target.append(json.dumps(entry["row"], ensure_ascii=False))
    return "\n".join(curated).encode(), "\n".join(legacy).encode()


def assert_balanced_primary_list(
    *,
    active: list[dict[str, Any]],
    review: list[dict[str, Any]],
    overflow: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> None:
    """The acceptance relationships for one run's customer-visible outcome.

    Shared by the in-process release gate and the HTTP smoke run so a live
    deployment is held to the same statements the test suite is. Every check is
    a relationship between what the run reported and what it persisted; none of
    them assumes a particular corpus.
    """
    qualified = int(metrics.get("qualified_leads", 0) or 0)
    pool = int(metrics.get("strong_fit_pool", 0) or 0)
    if qualified != len(active):
        raise SmokeFailure(
            f"metrics report {qualified} qualified leads but {len(active)} are displayed"
        )
    if qualified > RESULT_LIMIT:
        raise SmokeFailure(f"{qualified} displayed leads exceeds the limit of {RESULT_LIMIT}")
    if pool < qualified:
        raise SmokeFailure(f"strong-fit pool {pool} is smaller than the {qualified} displayed")
    if any(row.get("verdict") != "strong_fit" for row in active):
        raise SmokeFailure("the primary list contains a verdict other than strong_fit")
    if any(not (row.get("selection") or {}).get("displayed") for row in active):
        raise SmokeFailure("the primary list contains an undisplayed result")
    if any(not row.get("evidence") for row in active):
        raise SmokeFailure("a displayed lead has no evidence receipt")
    ranks = [(row.get("selection") or {}).get("display_rank") for row in active]
    if ranks != sorted(rank for rank in ranks if rank is not None) or None in ranks:
        raise SmokeFailure("the primary list is not in saved rank order")
    if any(row.get("lead_id") is not None for row in review):
        raise SmokeFailure("a review candidate was materialized as a lead")
    if any((row.get("selection") or {}).get("displayed") for row in overflow):
        raise SmokeFailure("an overflow strong fit is marked displayed")
    if any(row.get("lead_id") is not None for row in overflow):
        raise SmokeFailure("an overflow strong fit was materialized as a lead")

    counts: dict[str, int] = {}
    for row in active:
        code = str(row.get("country") or "").upper()
        counts[code] = counts.get(code, 0) + 1
    reported = {
        str(key).upper(): int(value)
        for key, value in (metrics.get("leads_by_country") or {}).items()
    }
    if reported and reported != counts:
        raise SmokeFailure(
            f"metrics report {reported} per country but the list shows {counts}"
        )
    # The balance rule, stated directly: nobody takes a fourth while a
    # represented market is still under three and has a candidate waiting.
    waiting = {
        str(row.get("country") or "").upper() for row in overflow
    }
    for country, count in counts.items():
        if count < 4:
            continue
        starved = [
            other for other, other_count in counts.items()
            if other_count < 3 and other in waiting
        ]
        if starved:
            raise SmokeFailure(
                f"{country} took {count} while {sorted(starved)} had unselected candidates"
            )
    shortfall = int(metrics.get("result_shortfall", 0) or 0)
    if shortfall != max(0, RESULT_TARGET_MIN - qualified):
        raise SmokeFailure(f"reported shortfall {shortfall} does not match {qualified} qualified")


class SmokeFailure(RuntimeError):
    """A release-gate assertion failed."""


@dataclass(frozen=True)
class ApiClient:
    base_url: str
    token: str | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        if body is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                parsed = json.loads(raw) if raw else None
                return response.status, parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail: Any = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = raw.decode("utf-8", errors="replace")
            raise SmokeFailure(f"{method} {path} returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SmokeFailure(f"{method} {path} could not connect: {exc.reason}") from exc


def _password(path: Path) -> str:
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SmokeFailure("password file must be a regular file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SmokeFailure("password file must be readable only by its owner (chmod 600)")
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not value:
        raise SmokeFailure("password file is empty")
    return value


def _expect(status: int, expected: int, label: str) -> None:
    if status != expected:
        raise SmokeFailure(f"{label}: expected HTTP {expected}, received {status}")


CAMPAIGN_TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


def _await_campaign(client, campaign_id: str, timeout: float = 900) -> dict:
    """Poll a queued campaign until it settles.

    `/start` queues the run and answers immediately — a campaign is hundreds of
    blocking fetches and cannot own a request. Every client therefore polls, and
    this is the reference for how.
    """
    deadline = time.monotonic() + timeout
    while True:
        status, campaign = client.request("GET", f"/api/v1/research-campaigns/{campaign_id}")
        _expect(status, 200, "campaign poll")
        if campaign.get("status") in CAMPAIGN_TERMINAL:
            return campaign
        if time.monotonic() >= deadline:
            raise SmokeFailure(
                f"research campaign still {campaign.get('status')!r} after {timeout:.0f}s"
            )
        time.sleep(0.2)


def _assert_empty(client: ApiClient, paths: tuple[str, ...], *, phase: str) -> None:
    for path in paths:
        status, value = client.request("GET", path)
        _expect(status, 200, path)
        if value != []:
            raise SmokeFailure(f"{phase}: {path} is not empty")


def _multipart_file(
    field: str,
    filename: str,
    content_type: str,
    content: bytes,
) -> tuple[bytes, str]:
    boundary = f"interfaze-smoke-{uuid.uuid4().hex}"
    body = b"".join((
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ))
    return body, f"multipart/form-data; boundary={boundary}"


def _choose_source(client: ApiClient, requested: str | None) -> str:
    status, catalog = client.request("GET", "/api/v1/data-sources/catalog")
    _expect(status, 200, "source catalog")
    available = [row["source_id"] for row in catalog if row.get("available")]
    if requested:
        if requested not in available:
            raise SmokeFailure(f"research source {requested!r} is not available")
        return requested
    if not available:
        raise SmokeFailure("no configured research verifier is available")
    return available[0]


def _mutation_mode(args: argparse.Namespace) -> bool:
    mode = getattr(args, "mode", "read-only")
    if mode not in {"read-only", "full"}:
        raise SmokeFailure(f"unsupported smoke mode: {mode!r}")
    if mode == "read-only":
        return False
    email = args.email.strip().casefold()
    confirmation = str(getattr(args, "confirm_disposable_tenant", "") or "").strip().casefold()
    if email in PROTECTED_DEMO_EMAILS:
        raise SmokeFailure(
            "full smoke cannot mutate the protected demo tenant; provision a disposable smoke tenant"
        )
    if not confirmation or confirmation != email:
        raise SmokeFailure(
            "full smoke requires the disposable tenant confirmation "
            "(--confirm-disposable-tenant) to exactly match --email"
        )
    return True


def run(args: argparse.Namespace) -> None:
    mutating = _mutation_mode(args)
    anonymous = ApiClient(args.base_url)
    status, session = anonymous.request("POST", "/api/v1/auth/login", payload={
        "email": args.email,
        "password": _password(args.password_file),
    })
    _expect(status, 200, "login")
    client = ApiClient(args.base_url, session["access_token"])

    _assert_empty(client, INITIAL_EMPTY_COLLECTIONS, phase="clean tenant check")
    if not mutating:
        print("clean demo smoke passed (read-only empty-tenant check)")
        return

    catalog = (
        b"product_name,category,aliases\n"
        b"Release gate built-in oven,Ovens,oven;electric oven\n"
    )
    body, content_type = _multipart_file(
        "file", "release-gate-products.csv", "text/csv", catalog,
    )
    status, imported = client.request(
        "POST", "/api/v1/products/import", body=body, content_type=content_type,
    )
    _expect(status, 201, "product import")
    if imported.get("imported") != 1:
        raise SmokeFailure("product import did not create exactly one product")

    # Product and backend-candidate imports must not materialize tenant leads,
    # contacts, country choices, research, campaigns, or outreach on their own.
    _assert_empty(
        client,
        OPERATIONAL_COLLECTIONS,
        phase="after catalog and backend candidate import",
    )
    source_id = _choose_source(client, args.source_id)
    status, campaign = client.request("POST", "/api/v1/research-campaigns", payload={
        "name": "Release gate appliance buyers",
        "seller_countries": ["TR"],
        "target_countries": [
            value.strip().upper() for value in str(args.country).split(",") if value.strip()
        ],
        "sector_ids": ["household-appliances"],
        "buyer_types": ["importer", "distributor", "retailer", "wholesaler"],
        "enabled_source_ids": [source_id],
    })
    _expect(status, 201, "campaign creation")
    campaign_id = campaign["id"]
    status, started = client.request(
        "POST", f"/api/v1/research-campaigns/{campaign_id}/start",
    )
    _expect(status, 202, "campaign start")
    if started.get("status") != "queued":
        raise SmokeFailure(f"campaign start returned {started.get('status')!r}, expected 'queued'")
    settled = _await_campaign(client, campaign_id)
    if settled.get("status") not in {"succeeded", "partial"}:
        raise SmokeFailure(f"research campaign ended as {settled.get('status')!r}")

    _, active = client.request(
        "GET", f"/api/v1/research-campaigns/{campaign_id}/results",
    )
    _, rejected = client.request(
        "GET", f"/api/v1/research-campaigns/{campaign_id}/results?view=rejected",
    )
    _, review = client.request(
        "GET", f"/api/v1/research-campaigns/{campaign_id}/results?view=review",
    )
    _, overflow = client.request(
        "GET", f"/api/v1/research-campaigns/{campaign_id}/results?view=outside_limit",
    )
    _, metric_rows = client.request(
        "GET", f"/api/v1/research-campaigns/{campaign_id}/metrics",
    )
    metrics = next(
        (row for row in (metric_rows or []) if row.get("dimension") == "overall"),
        (metric_rows or [{}])[0],
    )
    assert_balanced_primary_list(
        active=active, review=review, overflow=overflow, metrics=metrics,
    )
    if not active:
        raise SmokeFailure("research produced no active results")
    if any(row.get("verdict") == "reject" for row in active):
        raise SmokeFailure("active results contain a rejected verdict")
    if any(row.get("verdict") != "reject" for row in rejected):
        raise SmokeFailure("rejected results contain a non-rejected verdict")
    if {row["id"] for row in active} & {row["id"] for row in rejected}:
        raise SmokeFailure("active and rejected results overlap")
    for row in active:
        if not row.get("source_ids"):
            raise SmokeFailure(f"active result {row['id']} has no source IDs")
        _, claims = client.request("GET", f"/api/v1/research/results/{row['id']}/claims")
        evidence = [item for claim in claims for item in claim.get("evidence", [])]
        # A receipt is either a public page or an immutable internal dataset
        # reference. Requiring a URL rejected the curated-corpus path outright,
        # which is exactly the evidence this release is about.
        incomplete = any(
            not (
                str(item.get("provenance_url") or "").startswith("https://")
                or str(item.get("source_reference") or "").startswith("dataset:")
            )
            or not item.get("snapshot_id")
            or not item.get("raw_hash")
            for item in evidence
        )
        if not evidence or incomplete:
            raise SmokeFailure(
                f"active result {row['id']} lacks a complete evidence receipt"
            )
    print(
        f"clean demo smoke passed ({len(active)} displayed, {len(review)} review, "
        f"{len(overflow)} not selected, {len(rejected)} rejected)"
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base-url", required=True)
    result.add_argument("--email", required=True)
    result.add_argument("--password-file", required=True, type=Path)
    result.add_argument(
        "--mode",
        choices=("read-only", "full"),
        default="read-only",
        help="read-only by default; full irreversibly mutates a disposable tenant",
    )
    result.add_argument(
        "--confirm-disposable-tenant",
        metavar="EMAIL",
        help="required in full mode and must exactly match --email",
    )
    result.add_argument(
        "--source-id",
        help="configured verifier source; defaults to the first available",
    )
    result.add_argument(
        "--country", default="DE",
        help="comma-separated ISO alpha-2 target markets (default: DE)",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        run(parser().parse_args(argv))
    except (OSError, SmokeFailure, KeyError, TypeError, ValueError) as exc:
        print(f"clean demo smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
