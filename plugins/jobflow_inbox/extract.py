"""URL handling + job-field extraction for the jobflow_inbox plugin."""

from __future__ import annotations

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
