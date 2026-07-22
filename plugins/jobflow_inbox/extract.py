"""URL handling + job-field extraction for the jobflow_inbox plugin."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_URL_RE = re.compile(r"https?://[^\s<>\"']+")

# Tracking params stripped during normalization (prefix match on lowercased key).
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_EXACT = {"gh_src", "ref", "trk", "refid", "trackingid", "src"}


def find_first_url(text: str) -> str | None:
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_EXACT or any(k.startswith(p) for p in _TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking(k)]
    query = urlencode(kept)
    return urlunsplit((scheme, netloc, path, query, ""))


import json
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HermesJobFlow/1.0"
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:([a-z_]+)["\'][^>]+content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class JobFields:
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    salary: Optional[str] = None
    description: Optional[str] = None
    enrichment_status: str = "failed"


def _strip_html(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    return html.unescape(_TAG_RE.sub("", text).strip())


def _iter_jsonld_objects(html: str):
    for block in _JSONLD_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if isinstance(node.get("@graph"), list):
                    stack.extend(node["@graph"])
                yield node


def _first_jobposting(html: str) -> Optional[dict]:
    for obj in _iter_jsonld_objects(html):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(str(x).lower() == "jobposting" for x in types):
            return obj
    return None


def _location_from(node: dict) -> Optional[str]:
    loc = node.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return None
    addr = loc.get("address")
    if isinstance(addr, dict):
        bits = [addr.get("addressLocality"), addr.get("addressRegion"),
                addr.get("addressCountry")]
        bits = [str(b) for b in bits if b]
        return ", ".join(bits) or None
    if isinstance(addr, str):
        return addr
    return None


def _salary_from(node: dict) -> Optional[str]:
    bs = node.get("baseSalary")
    if not isinstance(bs, dict):
        return None
    currency = bs.get("currency") or ""
    val = bs.get("value")
    if isinstance(val, dict):
        lo, hi = val.get("minValue"), val.get("maxValue")
        single = val.get("value")
        unit = val.get("unitText") or ""
        if lo is not None and hi is not None:
            return f"{currency} {lo}-{hi} {unit}".strip()
        if single is not None:
            return f"{currency} {single} {unit}".strip()
    return None


def parse_job_html(page_html: str) -> JobFields:
    node = _first_jobposting(page_html)
    if node:
        org = node.get("hiringOrganization")
        company = org.get("name") if isinstance(org, dict) else (
            org if isinstance(org, str) else None)
        jf = JobFields(
            title=_strip_html(node.get("title")),
            company=_strip_html(company),
            location=_location_from(node),
            salary=_salary_from(node),
            description=_strip_html(node.get("description")),
            enrichment_status="enriched",
        )
        if jf.title:
            return jf

    # Fallback: OpenGraph / <title>.
    og = {k.lower(): v for k, v in _OG_RE.findall(page_html)}
    title = html.unescape(og["title"]) if og.get("title") else None
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
        title = _strip_html(m.group(1)) if m else None
    company = html.unescape(og["site_name"]) if og.get("site_name") else None
    if title:
        return JobFields(title=title, company=company, enrichment_status="partial")
    return JobFields(enrichment_status="failed")


def _default_fetch(url: str) -> str:
    last_err = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except Exception as exc:  # noqa: BLE001 — retried, then surfaced
            last_err = exc
    raise last_err if last_err else RuntimeError("fetch failed")


def extract_job(url: str, *, fetch: Optional[Callable[[str], str]] = None) -> JobFields:
    fetcher = fetch or _default_fetch
    try:
        page_html = fetcher(url)
    except Exception:  # noqa: BLE001 — graceful fallback to URL-only
        return JobFields(enrichment_status="failed")
    return parse_job_html(page_html)
