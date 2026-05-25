"""Read-only Christopher iLinked reconciliation lookup.

This module is the first TGG-specific reconciliation layer between WhatsApp /
operator-cases facts and the read-only iLinked corpus captured by the
browser-owner crawler.

It intentionally performs no iLinked writes and does not require a live browser
session. The only input source is a local corpus directory:

``CHRISTOPHER_ILINKED_CORPUS_DIR`` or, by default, the newest
``~/pcl/ilinked-corpus/tgg/full-import-*`` directory.

Public API:

``query_ilinked(payload, corpus_dir=None) -> dict``

Payload accepts any of:
- ``jobNo`` / ``job_no`` / ``task_no`` / ``message`` / ``text`` / ``query`` for
  exact iLinked task/job number lookup.
- ``address`` / ``unit`` / ``message`` / ``text`` / ``query`` for block+unit
  fuzzy matching.

Return shape:

```
{
  "ok": True,
  "query": {"raw": "...", "jobNo": "...", "block": "...", "unit": "..."},
  "confidence": "exact" | "high_similarity" | "low" | "no_match",
  "matches": [
    {
      "confidence": "...",
      "score": 0.0-1.0,
      "reasons": ["..."],
      "entry": {
        "taskNo": "PG/JOB/2605/0334",
        "jobNo": "PG/JOB/2605/0334",
        "taskType": "Job",
        "description": "...",
        "location": "...",
        "status": "...",
        "subStatus": "...",
        "leaf": "...",
        "sourceFile": "tree/leaf-....json"
      }
    }
  ],
  "meta": {"adapter": "tools.tgg_ilinked_lookup", "corpus_dir": "..."}
}
```
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_LIMIT = 5
JOB_NO_RE = re.compile(r"\b[A-Z]{2,4}/[A-Z][A-Z0-9_-]*/\d{4}/\d{3,5}\b", re.I)
BLOCK_RE = re.compile(r"\b(?:BLK|BLOCK)\s*([0-9]{1,4}[A-Z]?)\b", re.I)
UNIT_HASH_RE = re.compile(r"#\s*([0-9]{1,2})\s*[-–]\s*([0-9]{1,5}[A-Z]?)", re.I)
UNIT_PLAIN_RE = re.compile(
    r"\b(?:UNIT\s*)?([0-9]{1,2})\s*[-–]\s*([0-9]{2,5}[A-Z]?)\b",
    re.I,
)
POSTAL_RE = re.compile(r"\bS(?:INGAPORE)?\s*\(?\s*(\d{6})\s*\)?", re.I)


@dataclass(frozen=True)
class IlinkedEntry:
    """Canonical row extracted from an iLinked grid corpus."""

    taskNo: str
    jobNo: str | None
    taskType: str
    description: str
    location: str
    createdDate: str
    createdBy: str
    subStatus: str
    status: str
    leaf: str
    pageArg: str
    sourceFile: str
    block: str | None
    unit: str | None
    postalCode: str | None


@dataclass(frozen=True)
class CorpusIndex:
    corpus_dir: str
    entries: tuple[IlinkedEntry, ...]
    by_task_no: Mapping[str, IlinkedEntry]


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("payload must be a JSON object")
    return parsed


def _first(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_task_no(value: str | None) -> str:
    """Canonical task/job number form used for exact matching."""

    return re.sub(r"\s+", "", str(value or "").strip().upper())


def extract_job_no(text: str | None) -> str:
    """Return the first iLinked-style task/job number in text, if present."""

    match = JOB_NO_RE.search(str(text or "").upper())
    return normalize_task_no(match.group(0)) if match else ""


def normalize_token_text(value: str | None) -> str:
    """Normalize address-ish text for deterministic fuzzy scoring."""

    text = str(value or "").upper()
    text = text.replace("AVENUE", "AVE").replace("STREET", "ST")
    text = re.sub(r"[^A-Z0-9#-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_block(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).upper().strip()
    value = re.sub(r"^(?:BLK|BLOCK)\s*", "", value)
    value = re.sub(r"[^0-9A-Z]", "", value)
    return value or None


def normalize_unit(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).upper().strip()
    value = value.replace("#", "")
    value = value.replace("–", "-")
    value = re.sub(r"\s+", "", value)
    match = UNIT_PLAIN_RE.search(value)
    if match:
        floor, number = match.groups()
        return f"{int(floor):02d}-{number.upper()}"
    return value or None


def extract_block(text: str | None) -> str | None:
    match = BLOCK_RE.search(str(text or ""))
    if match:
        return normalize_block(match.group(1))
    # Fallback for iLinked descriptions such as "Incident Location 223A SUMANG".
    match = re.search(r"\b(?:LOCATION:?\s*)?([0-9]{2,4}[A-Z]?)\s+[A-Z]", str(text or ""), re.I)
    if match:
        return normalize_block(match.group(1))
    return None


def extract_unit(text: str | None) -> str | None:
    raw = str(text or "")
    match = UNIT_HASH_RE.search(raw) or UNIT_PLAIN_RE.search(raw)
    if not match:
        return None
    floor, number = match.groups()
    return f"{int(floor):02d}-{number.upper()}"


def extract_postal(text: str | None) -> str | None:
    match = POSTAL_RE.search(str(text or ""))
    return match.group(1) if match else None


def latest_corpus_dir() -> str:
    env_dir = os.getenv("CHRISTOPHER_ILINKED_CORPUS_DIR", "").strip()
    if env_dir:
        return str(Path(env_dir).expanduser())
    pattern = str(Path("~/pcl/ilinked-corpus/tgg/full-import-*").expanduser())
    candidates = [Path(p) for p in glob.glob(pattern)]
    if not candidates:
        raise FileNotFoundError(
            "No iLinked corpus found. Set CHRISTOPHER_ILINKED_CORPUS_DIR or create "
            "~/pcl/ilinked-corpus/tgg/full-import-*."
        )
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


def _cell_text(row: Mapping[str, Any], header_map: Mapping[str, int], *headers: str) -> str:
    cells = row.get("cells")
    if not isinstance(cells, list):
        return ""
    for header in headers:
        idx = header_map.get(header.lower())
        if idx is None or idx >= len(cells):
            continue
        cell = cells[idx]
        if isinstance(cell, Mapping):
            value = cell.get("text")
            if value is not None:
                return str(value).strip()
    return ""


def _entry_from_row(root: Path, path: Path, payload: Mapping[str, Any], row: Mapping[str, Any]) -> IlinkedEntry | None:
    grid = payload.get("grid")
    if not isinstance(grid, Mapping) or not grid.get("ok", False):
        return None
    headers = grid.get("headers")
    if not isinstance(headers, list):
        return None
    header_map = {str(header).strip().lower(): idx for idx, header in enumerate(headers)}
    task_no = _cell_text(row, header_map, "Task Number")
    if not task_no:
        return None
    task_type = _cell_text(row, header_map, "Task Type")
    description = _cell_text(row, header_map, "Description")
    location = _cell_text(row, header_map, "Location")
    combined = " ".join(part for part in (location, description) if part)
    leaf = payload.get("leaf")
    leaf_text = ""
    if isinstance(leaf, Mapping):
        leaf_text = str(leaf.get("text") or "")
    task_no_norm = normalize_task_no(task_no)
    job_no = task_no_norm if "/JOB/" in task_no_norm else None
    return IlinkedEntry(
        taskNo=task_no_norm,
        jobNo=job_no,
        taskType=task_type,
        description=description,
        location=location,
        createdDate=_cell_text(row, header_map, "Created Date"),
        createdBy=_cell_text(row, header_map, "Created By"),
        subStatus=_cell_text(row, header_map, "Sub Status"),
        status=_cell_text(row, header_map, "Status"),
        leaf=leaf_text,
        pageArg=str(payload.get("pageArg") or ""),
        sourceFile=str(path.relative_to(root)),
        block=extract_block(combined),
        unit=extract_unit(combined),
        postalCode=extract_postal(combined),
    )


def load_corpus(corpus_dir: str | None = None) -> CorpusIndex:
    """Load and normalize a crawler corpus from disk.

    ``corpus_dir=None`` means "resolve the newest corpus now". The cached helper
    is keyed by the resolved path, not by ``None``, so a long-running runtime can
    pick up a newer crawl on the next query instead of pinning the first corpus
    it ever saw.
    """

    root = Path(corpus_dir or latest_corpus_dir()).expanduser().resolve()
    return _load_corpus_by_path(str(root))


@lru_cache(maxsize=8)
def _load_corpus_by_path(root_path: str) -> CorpusIndex:
    root = Path(root_path)
    if not root.exists():
        raise FileNotFoundError(f"iLinked corpus does not exist: {root}")
    entries: dict[str, IlinkedEntry] = {}
    for path in sorted(root.glob("tree/*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        grid = payload.get("grid")
        rows = grid.get("rows") if isinstance(grid, Mapping) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            entry = _entry_from_row(root, path, payload, row)
            if entry is not None and entry.taskNo not in entries:
                entries[entry.taskNo] = entry
    ordered = tuple(entries.values())
    return CorpusIndex(
        corpus_dir=str(root),
        entries=ordered,
        by_task_no={entry.taskNo: entry for entry in ordered},
    )


def _entry_payload(entry: IlinkedEntry) -> dict[str, Any]:
    return asdict(entry)


def _confidence(score: float, exact: bool = False) -> str:
    if exact:
        return "exact"
    if score >= 0.78:
        return "high_similarity"
    if score >= 0.42:
        return "low"
    return "no_match"


def _match_record(entry: IlinkedEntry, *, score: float, reasons: list[str], exact: bool = False) -> dict[str, Any]:
    return {
        "confidence": _confidence(score, exact=exact),
        "score": round(score, 4),
        "reasons": reasons,
        "entry": _entry_payload(entry),
    }


def exact_match(job_no: str, index: CorpusIndex) -> dict[str, Any] | None:
    normalized = normalize_task_no(job_no)
    if not normalized:
        return None
    entry = index.by_task_no.get(normalized)
    if entry is None:
        return None
    return _match_record(entry, score=1.0, reasons=["task_no_exact"], exact=True)


def _address_score(raw_query: str, block: str | None, unit: str | None, entry: IlinkedEntry) -> tuple[float, list[str]]:
    query_norm = normalize_token_text(raw_query)
    entry_norm = normalize_token_text(" ".join([entry.location, entry.description]))
    score = 0.0
    reasons: list[str] = []

    if block and entry.block == block:
        score += 0.42
        reasons.append("block_match")
    elif block and entry.block:
        score -= 0.08

    if unit and entry.unit == unit:
        score += 0.42
        reasons.append("unit_match")
    elif unit and entry.unit:
        score -= 0.08

    if block and unit and entry.block == block and entry.unit == unit:
        score += 0.12
        reasons.append("block_unit_pair_match")

    postal = extract_postal(raw_query)
    if postal and entry.postalCode == postal:
        score += 0.12
        reasons.append("postal_match")

    similarity = SequenceMatcher(None, query_norm, entry_norm).ratio() if query_norm and entry_norm else 0.0
    if similarity >= 0.55:
        reasons.append("address_text_similarity")
    # Text similarity is a support signal; block/unit are the stronger keys.
    score += min(similarity * 0.35, 0.35)
    return max(0.0, min(score, 1.0)), reasons


def fuzzy_address_match(
    raw_query: str,
    index: CorpusIndex,
    *,
    limit: int = DEFAULT_LIMIT,
    block: str | None = None,
    unit: str | None = None,
) -> list[dict[str, Any]]:
    """Rank corpus entries by block/unit and address similarity."""

    block = normalize_block(block) or extract_block(raw_query)
    unit = normalize_unit(unit) or extract_unit(raw_query)
    ranked: list[dict[str, Any]] = []
    for entry in index.entries:
        score, reasons = _address_score(raw_query, block, unit, entry)
        # Candidates below the ``low`` floor are not useful as matches. Returning
        # them makes the top-level confidence say ``no_match`` while still
        # carrying rows, which is ambiguous for downstream gap detection.
        if score < 0.42:
            continue
        ranked.append(_match_record(entry, score=score, reasons=reasons))
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[: max(1, limit)]


def query_ilinked(payload: Mapping[str, Any], corpus_dir: str | None = None) -> dict[str, Any]:
    """Query the read-only iLinked corpus for an exact or fuzzy match."""

    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    index = load_corpus(corpus_dir)
    raw = _first(payload, "message", "text", "query", "address", "unit")
    job_no = _first(payload, "jobNo", "job_no", "job", "task_no") or extract_job_no(raw)
    limit_raw = _first(payload, "limit")
    try:
        limit = int(limit_raw) if limit_raw else DEFAULT_LIMIT
    except ValueError:
        limit = DEFAULT_LIMIT
    block = normalize_block(_first(payload, "block", "blk"))
    unit = normalize_unit(_first(payload, "unit"))
    matches: list[dict[str, Any]] = []

    if job_no:
        match = exact_match(job_no, index)
        if match:
            matches = [match]
        elif raw or block or unit:
            matches = fuzzy_address_match(raw or job_no, index, limit=limit, block=block, unit=unit)
    elif raw or block or unit:
        query_text = raw or " ".join(part for part in (block, unit) if part)
        matches = fuzzy_address_match(query_text, index, limit=limit, block=block, unit=unit)

    top_confidence = matches[0]["confidence"] if matches else "no_match"
    return {
        "ok": True,
        "query": {
            "raw": raw,
            "jobNo": normalize_task_no(job_no) if job_no else None,
            "block": block or extract_block(raw),
            "unit": unit or extract_unit(raw),
        },
        "confidence": top_confidence,
        "matches": matches,
        "meta": {
            "adapter": "tools.tgg_ilinked_lookup",
            "corpus_dir": index.corpus_dir,
            "indexed_entries": len(index.entries),
            "read_only": True,
        },
    }


def main() -> int:
    try:
        payload = _read_payload()
        result = query_ilinked(payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "CHRISTOPHER_ILINKED_LOOKUP_ADAPTER_ERROR",
                        "message": str(exc),
                    },
                    "meta": {
                        "adapter": "tools.tgg_ilinked_lookup",
                        "read_only": True,
                    },
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
