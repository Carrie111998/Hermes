"""Market preferences per company (UI-selected; replaces the old matrix).

Reads company-packs/<company>/market-preferences.yaml. Three ISO-code lists:
  target_markets       — prioritized; the lead map defaults here
  no_outreach_markets  — never contacted on any channel (research still allowed)
  no_research_markets  — never scanned or researched for leads

Exclusion-based: any market not listed is allowed. In the SaaS deployment
these come from the DB (sales-preferences + lead-map selection); this file
loader is what the standalone/demo path uses.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .run_types import PACKS

_LISTS = ("target_markets", "no_outreach_markets", "no_research_markets")


def _parse_flow_lists(text: str) -> dict[str, list[str]]:
    """Minimal parser for `key: [A, B, C]` lines (no pyyaml dependency).
    Used only for the demo pack format we control."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^(\w+):\s*\[(.*)\]\s*$", line)
        if m and m.group(1) in _LISTS:
            items = [x.strip().upper() for x in m.group(2).split(",") if x.strip()]
            out[m.group(1)] = items
    return out


@lru_cache(maxsize=32)
def load(company: str) -> dict[str, set[str]]:
    path = PACKS / company / "market-preferences.yaml"
    if not path.exists():
        return {k: set() for k in _LISTS}
    text = path.read_text()
    try:
        import yaml  # optional; falls back to the flow-list parser
        data = yaml.safe_load(text) or {}
        parsed = {k: [str(x).upper() for x in (data.get(k) or [])] for k in _LISTS}
    except Exception:
        parsed = _parse_flow_lists(text)
    return {k: set(parsed.get(k, [])) for k in _LISTS}


def target_markets(company: str) -> set[str]:
    return load(company)["target_markets"]


def no_outreach_markets(company: str) -> set[str]:
    return load(company)["no_outreach_markets"]


def no_research_markets(company: str) -> set[str]:
    return load(company)["no_research_markets"]
