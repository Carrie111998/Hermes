#!/usr/bin/env python3
"""Collect exact read-only discount-code rows; GPT supplies all interpretation."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DB_HELPER = Path(__file__).with_name("skyvision_db_readonly.py")
CODES = (
    "4M1R0YZY",
    "HKJLPJIB",
    "QI3BYFZR",
    "603Z8ZO8",
    "CSBMCAG0",
    "SZUJBEOD",
    "BYA4A7OQ",
    "W2T2H3E1",
    "NEKEST58",
    "BLSN6WBK",
    "8PA840XW",
    "TTA8ZMOT",
    "D93Y5LAR",
    "GMSE38FE",
    "4HUYA5RL",
    "EM9ZUMG3",
    "EPCZNWOJ",
    "PJMRV7SN",
    "MVFXBMTR",
    "JJNINSXE",
)
SQL_CODES = ",".join(f"'{code}'" for code in CODES)
QUERY = f"""
SELECT UPPER(p.code) AS code, p.type, p.value, p.max_uses,
       p.current_uses, p.validity, o.id AS order_id,
       o.integration_status, o.order_other_id, o.promo_total, o.created
FROM skyvisio_wp64.promo_codes p
LEFT JOIN skyvisio_wp64.orders_new o ON UPPER(o.promos) = UPPER(p.code)
WHERE UPPER(p.code) IN ({SQL_CODES})
ORDER BY code, o.created, o.id
LIMIT 100
""".strip()


def collect() -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(DB_HELPER),
            "--db",
            "skyvisio_wp64",
            "--case-id",
            "case:cron:2b8fbfcf9699",
            "--requester",
            "Muncho",
            "--purpose",
            "Exact read-only discount-code source rows for GPT review",
            "--expected-result-shape",
            "aggregate_report",
            "--max-rows",
            "100",
            "--timeout-seconds",
            "60",
            "--sensitivity",
            "normal",
            "--query",
            QUERY,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=90,
        text=True,
    )
    if completed.returncode != 0 or len(completed.stdout) > 512 * 1024:
        raise RuntimeError("discount_codes_raw_query_failed")
    payload = json.loads(completed.stdout)
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if payload.get("ok") is not True or not isinstance(rows, list) or len(rows) > 100:
        raise RuntimeError("discount_codes_raw_result_invalid")
    return {
        "schema": "skyvision-discount-codes-raw.v1",
        "ok": True,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_codes": list(CODES),
        "rows": rows,
        "row_count": len(rows),
        "semantic_judgment_performed": False,
        "delivery_attempted": False,
    }


def main() -> int:
    print(
        json.dumps(collect(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
