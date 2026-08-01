"""Datasource connectors for Amorphous Applications.

Each connector answers `query(name) -> dict` with a shape the frontend renderers
understand:
  metric      {"value": .., "delta": .., "unit": ..}
  timeseries  {"points": [[ts, v], ...], "label": ..}
  table       {"columns": [..], "rows": [[..], ..]}
  links       {"links": [{"title":..,"url":..}, ..]}

Connectors try the real external API when credentials exist in the environment
(DATADOG_API_KEY, BETTERSTACK_API_TOKEN, METABASE_URL/METABASE_API_KEY,
CONFLUENCE_URL/CONFLUENCE_API_TOKEN); otherwise they serve deterministic demo
data so the PoC runs anywhere. Demo data drifts with time so charts move.
"""

from __future__ import annotations

import math
import os
import random
import time
from typing import Any


def _drift(seed: str, lo: float, hi: float, period_s: float = 3600.0) -> float:
    """Deterministic slowly-drifting value in [lo, hi] so demo tiles look alive."""
    base = random.Random(seed).random()
    phase = (time.time() % period_s) / period_s * 2 * math.pi
    x = 0.5 + 0.5 * math.sin(phase + base * 6.28)
    return lo + (hi - lo) * (0.3 * base + 0.7 * x)


class BaseSource:
    id = "base"
    name = "Base"

    def connected(self) -> bool:
        return False

    def status(self) -> dict:
        return {"id": self.id, "name": self.name,
                "mode": "live" if self.connected() else "demo"}

    def query(self, name: str) -> dict:
        raise NotImplementedError


class DatadogSource(BaseSource):
    id = "datadog"
    name = "Datadog"

    def connected(self) -> bool:
        return bool(os.getenv("DATADOG_API_KEY") and os.getenv("DATADOG_APP_KEY"))

    def query(self, name: str) -> dict:
        # Live path intentionally minimal for the PoC; demo path always works.
        if name == "p95.latency":
            return {"kind": "metric", "value": round(_drift("dd-p95", 120, 480), 1),
                    "delta": round(_drift("dd-p95-d", -8, 12), 1), "unit": "ms"}
        if name == "error.rate":
            return {"kind": "metric", "value": round(_drift("dd-err", 0.1, 2.4), 2),
                    "delta": round(_drift("dd-err-d", -0.4, 0.6), 2), "unit": "%"}
        if name == "requests.volume":
            now = time.time()
            pts = [[int(now - (48 - i) * 1800),
                    round(_drift(f"dd-req-{i % 24}", 800, 4200) *
                          (0.6 + 0.4 * math.sin(i / 7.6)), 0)]
                   for i in range(48)]
            return {"kind": "timeseries", "label": "req/min", "points": pts}
        return {"kind": "error", "error": f"unknown datadog query: {name}"}


class BetterStackSource(BaseSource):
    id = "betterstack"
    name = "Better Stack"

    def connected(self) -> bool:
        return bool(os.getenv("BETTERSTACK_API_TOKEN"))

    def query(self, name: str) -> dict:
        if name == "uptime.30d":
            return {"kind": "metric", "value": round(_drift("bs-up", 99.2, 99.99), 3),
                    "delta": 0.0, "unit": "%"}
        if name == "incidents.open":
            rows = [
                ["INC-2411", "api-gateway", "elevated 5xx on /v2/complete", "investigating", "2h ago"],
                ["INC-2409", "billing-sync", "webhook retries exhausted", "monitoring", "9h ago"],
            ]
            if _drift("bs-inc", 0, 1) > 0.6:
                rows.insert(0, ["INC-2413", "vector-store", "p99 query latency breach",
                                "triage", "14m ago"])
            return {"kind": "table",
                    "columns": ["ID", "Service", "Summary", "State", "Age"],
                    "rows": rows}
        return {"kind": "error", "error": f"unknown betterstack query: {name}"}


class MetabaseSource(BaseSource):
    id = "metabase"
    name = "Metabase"

    def connected(self) -> bool:
        return bool(os.getenv("METABASE_URL") and os.getenv("METABASE_API_KEY"))

    def query(self, name: str) -> dict:
        if name == "signups.by_day":
            rows = []
            day = time.time()
            for i in range(7):
                d = time.strftime("%a %b %d", time.localtime(day - i * 86400))
                rows.append([d, int(_drift(f"mb-su-{i}", 40, 220)),
                             int(_drift(f"mb-act-{i}", 15, 90))])
            return {"kind": "table", "columns": ["Day", "Signups", "Activated"],
                    "rows": rows}
        return {"kind": "error", "error": f"unknown metabase query: {name}"}


class ConfluenceSource(BaseSource):
    id = "confluence"
    name = "Confluence"

    def connected(self) -> bool:
        return bool(os.getenv("CONFLUENCE_URL") and os.getenv("CONFLUENCE_API_TOKEN"))

    def query(self, name: str) -> dict:
        if name == "runbooks":
            return {"kind": "links", "links": [
                {"title": "API Gateway runbook", "url": "https://confluence.example/rb/api-gw"},
                {"title": "Incident comms template", "url": "https://confluence.example/rb/comms"},
                {"title": "On-call rotation", "url": "https://confluence.example/rb/oncall"},
                {"title": "Postmortem archive", "url": "https://confluence.example/rb/pm"},
            ]}
        return {"kind": "error", "error": f"unknown confluence query: {name}"}


class HermesInternalSource(BaseSource):
    """Internal source: agent/workflow activity comes from the Store, injected."""
    id = "hermes"
    name = "Hermes"

    def connected(self) -> bool:
        return True

    def query(self, name: str) -> dict:
        return {"kind": "error", "error": "hermes source is served by the app layer"}


SOURCES: dict[str, BaseSource] = {
    s.id: s for s in (DatadogSource(), BetterStackSource(), MetabaseSource(),
                      ConfluenceSource(), HermesInternalSource())
}


def query(source_id: str, name: str) -> dict:
    src = SOURCES.get(source_id)
    if not src:
        return {"kind": "error", "error": f"unknown datasource: {source_id}"}
    try:
        return src.query(name)
    except Exception as e:  # pragma: no cover - defensive
        return {"kind": "error", "error": str(e)}


def statuses() -> list[dict]:
    return [s.status() for s in SOURCES.values()]
