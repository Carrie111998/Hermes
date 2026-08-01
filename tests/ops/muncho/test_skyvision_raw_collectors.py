from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from ops.muncho.runtime import skyvision_backup_raw as backup
from ops.muncho.runtime import skyvision_discount_codes_raw as discounts
from ops.muncho.runtime import skyvision_seo_raw as seo


def test_backup_collector_preserves_exact_facts_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_rows = "10 42 backup /usr/local/cpanel/bin/backup\n"
    account_rows = "[2026-08-01] skyvisio row\n"
    raw = "\n".join((
        "remote_epoch=1800000000",
        "remote_date=2026-08-01",
        "service_httpd=active",
        "archive_present=1",
        "archive_bytes=12345",
        "process_rows_b64=" + base64.b64encode(process_rows.encode()).decode("ascii"),
        "account_log_rows_b64="
        + base64.b64encode(account_rows.encode()).decode("ascii"),
        "transporter_log_rows_b64=",
        "exclude_exact_rows_b64=",
        "backup_config_rows_b64=",
    ))
    monkeypatch.setattr(backup, "_run_remote", lambda _command: raw)

    packet = backup.collect()

    assert packet["facts"] == {
        "remote_epoch": "1800000000",
        "remote_date": "2026-08-01",
        "service_httpd": "active",
        "archive_present": "1",
        "archive_bytes": "12345",
    }
    assert packet["raw_evidence"]["process_rows"] == [
        "10 42 backup /usr/local/cpanel/bin/backup"
    ]
    assert packet["raw_evidence"]["account_log_rows"] == ["[2026-08-01] skyvisio row"]
    assert packet["semantic_judgment_performed"] is False
    assert packet["delivery_attempted"] is False


def test_discount_collector_returns_database_rows_without_transforming_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "code": "4M1R0YZY",
            "type": "fixed",
            "value": "10.00",
            "max_uses": 20,
            "current_uses": 3,
            "validity": "2026-12-31",
            "order_id": 712,
            "integration_status": "pending",
            "order_other_id": "",
            "promo_total": "-10.00",
            "created": "2026-08-01 05:00:00",
        }
    ]

    def run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        assert argv[-2:] == ["--query", discounts.QUERY]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "rows": rows}),
            stderr="",
        )

    monkeypatch.setattr(discounts.subprocess, "run", run)

    packet = discounts.collect()

    assert packet["requested_codes"] == list(discounts.CODES)
    assert packet["rows"] == rows
    assert packet["row_count"] == len(rows)
    assert packet["semantic_judgment_performed"] is False
    assert packet["delivery_attempted"] is False


def test_seo_collector_preserves_source_windows_and_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        seo,
        "_impersonated_token",
        lambda target, _scope: f"token:{target}",
    )

    def gsc_query(
        token: str,
        site_url: str,
        period: tuple[seo.date, seo.date],
        dimensions: tuple[str, ...] = (),
        *,
        limit: int,
    ) -> list[dict[str, object]]:
        return [
            {
                "token_target": token.removeprefix("token:"),
                "site_url": site_url,
                "start": str(period[0]),
                "end": str(period[1]),
                "dimensions": list(dimensions),
                "limit": limit,
                "clicks": 17,
            }
        ]

    def ga4_report(
        token: str,
        property_id: str,
        period: tuple[seo.date, seo.date],
        dimensions: tuple[str, ...] = (),
        *,
        limit: int,
    ) -> dict[str, object]:
        row = {
            "token_target": token.removeprefix("token:"),
            "property_id": property_id,
            "start": str(period[0]),
            "end": str(period[1]),
            "dimensions": list(dimensions),
            "limit": limit,
            "metricValues": [{"value": "23"}],
        }
        return {
            "dimension_headers": [],
            "metric_headers": [{"name": "activeUsers"}],
            "rows": [row],
            "row_count": 1,
        }

    monkeypatch.setattr(seo, "_gsc_query", gsc_query)
    monkeypatch.setattr(seo, "_ga4_report", ga4_report)

    packet = seo.collect()

    assert len(packet["gsc"]) == len(seo.GSC_PROPERTIES)
    assert len(packet["ga4"]) == len(seo.GA4_PROPERTIES)
    assert packet["gsc"][0]["rows"]["current_7d_query"][0]["clicks"] == 17
    assert packet["ga4"][0]["rows"]["current_7d_sessionDefaultChannelGroup"]["rows"][0][
        "metricValues"
    ] == [{"value": "23"}]
    assert packet["other_sources"] == {
        "ahrefs": {"available": False, "reason_code": "not_packaged"},
        "competitor_feeds": {
            "available": False,
            "reason_code": "not_packaged",
        },
    }
    assert packet["semantic_judgment_performed"] is False
    assert packet["delivery_attempted"] is False
