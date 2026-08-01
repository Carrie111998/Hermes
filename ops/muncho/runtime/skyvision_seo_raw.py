#!/usr/bin/env python3
"""Bounded keyless GSC/GA4 source rows with no SEO classification."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote


METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
GSC_TARGET = "muncho-gsc-reporter@adventico-ai-platform.iam.gserviceaccount.com"
GA4_TARGET = "muncho-analytics-reporter@adventico-ai-platform.iam.gserviceaccount.com"
GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GA4_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
GSC_BASE = "https://www.googleapis.com/webmasters/v3"
GA4_BASE = "https://analyticsdata.googleapis.com/v1beta"
GSC_PROPERTIES = (
    {"name": "SkyVision", "site_url": "sc-domain:skyvision.bg"},
    {"name": "Adventico", "site_url": "https://adventico.com/"},
)
GA4_PROPERTIES = (
    {"name": "SkyVision", "property_id": "321015614"},
    {"name": "Adventico", "property_id": "530092873"},
)
GA4_METRICS = (
    "activeUsers",
    "sessions",
    "engagedSessions",
    "engagementRate",
    "averageSessionDuration",
    "screenPageViews",
    "keyEvents",
    "totalRevenue",
)
MAX_OUTPUT_BYTES = 900 * 1024


def _request_json(
    url: str,
    *,
    token: str | None = None,
    payload: object | None = None,
    metadata: bool = False,
) -> tuple[int, dict[str, object]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if metadata:
        headers["Metadata-Flavor"] = "Google"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            value = json.load(response)
            return response.status, value if isinstance(value, dict) else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096).decode("utf-8", errors="replace")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = {"error_code": "remote_non_json_error"}
        return exc.code, value if isinstance(value, dict) else {}


def _impersonated_token(target: str, scope: str) -> str:
    status, source = _request_json(METADATA_TOKEN_URL, metadata=True)
    source_token = source.get("access_token")
    if status != 200 or not isinstance(source_token, str) or not source_token:
        raise RuntimeError("seo_raw_metadata_token_failed")
    status, projected = _request_json(
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{target}:generateAccessToken",
        token=source_token,
        payload={"scope": [scope], "lifetime": "3600s"},
    )
    token = projected.get("accessToken")
    if status != 200 or not isinstance(token, str) or not token:
        raise RuntimeError("seo_raw_impersonation_failed")
    return token


def _windows(today: date) -> dict[str, tuple[date, date]]:
    end = today - timedelta(days=1)
    return {
        "current_7d": (end - timedelta(days=6), end),
        "previous_7d": (end - timedelta(days=13), end - timedelta(days=7)),
        "current_28d": (end - timedelta(days=27), end),
        "previous_28d": (end - timedelta(days=55), end - timedelta(days=28)),
    }


def _gsc_query(
    token: str,
    site_url: str,
    period: tuple[date, date],
    dimensions: tuple[str, ...] = (),
    *,
    limit: int,
) -> list[dict[str, object]]:
    body: dict[str, object] = {
        "startDate": str(period[0]),
        "endDate": str(period[1]),
        "dataState": "final",
        "rowLimit": limit,
    }
    if dimensions:
        body["dimensions"] = list(dimensions)
    status, value = _request_json(
        f"{GSC_BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query",
        token=token,
        payload=body,
    )
    rows = value.get("rows", [])
    if status != 200 or not isinstance(rows, list) or len(rows) > limit:
        raise RuntimeError("seo_raw_gsc_query_failed")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _ga4_report(
    token: str,
    property_id: str,
    period: tuple[date, date],
    dimensions: tuple[str, ...] = (),
    *,
    limit: int,
) -> dict[str, object]:
    body: dict[str, object] = {
        "dateRanges": [{"startDate": str(period[0]), "endDate": str(period[1])}],
        "metrics": [{"name": name} for name in GA4_METRICS],
        "limit": str(limit),
    }
    if dimensions:
        body["dimensions"] = [{"name": name} for name in dimensions]
    status, value = _request_json(
        f"{GA4_BASE}/properties/{property_id}:runReport",
        token=token,
        payload=body,
    )
    rows = value.get("rows", [])
    if status != 200 or not isinstance(rows, list) or len(rows) > limit:
        raise RuntimeError("seo_raw_ga4_query_failed")
    return {
        "dimension_headers": value.get("dimensionHeaders", []),
        "metric_headers": value.get("metricHeaders", []),
        "rows": rows,
        "row_count": len(rows),
    }


def collect() -> dict[str, object]:
    windows = _windows(date.today())
    rendered_windows = {
        name: {"start_date": str(period[0]), "end_date": str(period[1])}
        for name, period in windows.items()
    }
    gsc_token = _impersonated_token(GSC_TARGET, GSC_SCOPE)
    ga4_token = _impersonated_token(GA4_TARGET, GA4_SCOPE)
    gsc: list[dict[str, object]] = []
    for prop in GSC_PROPERTIES:
        totals = {
            name: _gsc_query(gsc_token, prop["site_url"], period, limit=1)
            for name, period in windows.items()
        }
        rows = {
            f"{period_name}_{dimension}": _gsc_query(
                gsc_token,
                prop["site_url"],
                windows[period_name],
                (dimension,),
                limit=250,
            )
            for period_name in ("current_7d", "previous_7d")
            for dimension in ("query", "page")
        }
        rows["current_7d_device"] = _gsc_query(
            gsc_token, prop["site_url"], windows["current_7d"], ("device",), limit=20
        )
        rows["current_7d_country"] = _gsc_query(
            gsc_token, prop["site_url"], windows["current_7d"], ("country",), limit=50
        )
        rows["current_7d_query_page"] = _gsc_query(
            gsc_token,
            prop["site_url"],
            windows["current_7d"],
            ("query", "page"),
            limit=250,
        )
        gsc.append({"property": dict(prop), "totals": totals, "rows": rows})
    ga4: list[dict[str, object]] = []
    for prop in GA4_PROPERTIES:
        totals = {
            name: _ga4_report(ga4_token, prop["property_id"], period, limit=1)
            for name, period in windows.items()
        }
        rows = {
            f"{period_name}_{dimension}": _ga4_report(
                ga4_token,
                prop["property_id"],
                windows[period_name],
                (dimension,),
                limit=100,
            )
            for period_name in ("current_7d", "previous_7d")
            for dimension in (
                "sessionDefaultChannelGroup",
                "sessionSourceMedium",
                "landingPagePlusQueryString",
                "deviceCategory",
                "country",
            )
        }
        ga4.append({"property": dict(prop), "totals": totals, "rows": rows})
    return {
        "schema": "skyvision-seo-raw-sources.v1",
        "ok": True,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "windows": rendered_windows,
        "gsc": gsc,
        "ga4": ga4,
        "other_sources": {
            "ahrefs": {"available": False, "reason_code": "not_packaged"},
            "competitor_feeds": {"available": False, "reason_code": "not_packaged"},
        },
        "semantic_judgment_performed": False,
        "delivery_attempted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("probe", "collect"))
    args = parser.parse_args(argv)
    if args.action == "probe":
        _impersonated_token(GSC_TARGET, GSC_SCOPE)
        _impersonated_token(GA4_TARGET, GA4_SCOPE)
        output: dict[str, object] = {"schema": "skyvision-seo-raw-probe.v1", "ok": True}
    else:
        output = collect()
    raw = json.dumps(
        output, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(raw) > MAX_OUTPUT_BYTES:
        raise RuntimeError("seo_raw_output_oversized")
    sys.stdout.buffer.write(raw + b"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
