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
        "target_countries": [args.country.upper()],
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
        incomplete = any(
            not item.get("provenance_url", "").startswith("https://")
            or not item.get("snapshot_id")
            or not item.get("raw_hash")
            for item in evidence
        )
        if not evidence or incomplete:
            raise SmokeFailure(
                f"active result {row['id']} lacks complete HTTPS evidence metadata"
            )
    print(f"clean demo smoke passed ({len(active)} active, {len(rejected)} rejected)")


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
    result.add_argument("--country", default="DE", help="ISO alpha-2 target country (default: DE)")
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
