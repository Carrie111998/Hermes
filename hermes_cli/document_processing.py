"""Read-only corpus/document processing backed by auxiliary LLM tasks.

The implementation deliberately keeps a narrow core footprint: this is a CLI
workflow, not a model tool. It builds a source registry, performs deterministic
local extraction/chunking, enforces a fail-closed external-routing gate, and only
then optionally calls configured ``auxiliary.document_*`` routes.
"""
from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import mimetypes
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


RIGHTS_STATUSES = {"allowed", "unknown", "restricted", "blocked"}
SENSITIVITIES = {"public", "internal", "sensitive", "highly_sensitive"}
ROUTING_POLICIES = {"local_only", "external_allowed", "metadata_only", "blocked"}
SUPPORTED_TEXT_TYPES = {
    "markdown", "text", "html", "transcript", "pdf", "docx", "pptx", "xlsx", "epub"
}
AUXILIARY_TASKS = {
    "document_summarization",
    "document_merge_draft",
    "document_integrity_check",
    "document_corpus_planner",
}
EXACT_TOKEN_RE = re.compile(
    r"(?P<url>https?://[^\s)\]>'\"]+)"
    r"|(?P<date>\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b)"
    r"|(?P<time>\b\d{1,2}:\d{2}(?::\d{2})?\b)"
    r"|(?P<currency>[€$]\s?\d+(?:[.,]\d+)?)"
    r"|(?P<number>\b\d+(?:[.,]\d+)?\s?(?:%|€|\$|GB|MB|KB|ms|s|min|uur|dagen)?\b)"
    r"|(?P<path>(?:/|~/?|[A-Za-z]:/)[^\s)\]>'\"]+)"
    r"|(?P<wikilink>\[\[[^\]]+\]\])"
)


@dataclass
class SourceEntry:
    source_id: str
    path: str
    source_type: str
    title: str = ""
    origin: str = "unspecified"
    rights_status: str = "unknown"
    sensitivity: str = "internal"
    retention_policy: str = "pilot_derived_90d"
    retention_expires_at: str = ""
    routing_policy: str = "local_only"
    rights_released_by: str = ""
    hash_or_snapshot: str = ""
    size_bytes: int = 0
    created_at: str = ""


@dataclass
class DocumentUnit:
    unit_id: str
    source_id: str
    source_type: str
    location: dict[str, Any]
    extractor: str
    extraction_status: str
    confidence: int | None
    text_sha256: str
    char_count: int
    quote: str
    text_preview: str
    context: dict[str, Any] = field(default_factory=dict)
    integrity_terms: list[str] = field(default_factory=list)


class _HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        if tag in {"p", "br", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "td"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        if tag in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "td"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return _normalize_ws("".join(self._parts))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8", errors="replace"))


def _normalize_ws(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    collapsed: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                collapsed.append("")
            blank = True
        else:
            collapsed.append(line)
            blank = False
    return "\n".join(collapsed).strip()


def _read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def infer_source_type(path: str, explicit: str = "") -> str:
    if explicit:
        return explicit.strip().lower()
    suffix = Path(path).suffix.lower()
    suffix_map = {
        ".md": "markdown",
        ".markdown": "markdown",
        ".mdown": "markdown",
        ".html": "html",
        ".htm": "html",
        ".xhtml": "html",
        ".srt": "transcript",
        ".vtt": "transcript",
        ".transcript": "transcript",
        ".pdf": "pdf",
        ".docx": "docx",
        ".pptx": "pptx",
        ".xlsx": "xlsx",
        ".epub": "epub",
    }
    if suffix in suffix_map:
        return suffix_map[suffix]
    guessed, _ = mimetypes.guess_type(path)
    if guessed == "text/html":
        return "html"
    if guessed and guessed.startswith("text/"):
        return "text"
    return "text"


def _stable_source_id(path: str, used: set[str]) -> str:
    p = Path(path)
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", p.stem).strip("-").lower() or "source"
    digest = hashlib.sha1(str(p).encode("utf-8", errors="replace")).hexdigest()[:8]
    base = f"{stem}-{digest}"
    source_id = base
    i = 2
    while source_id in used:
        source_id = f"{base}-{i}"
        i += 1
    used.add(source_id)
    return source_id


def _validate_choice(value: str, allowed: set[str], field_name: str) -> str:
    value = (value or "").strip().lower()
    if value not in allowed:
        raise ValueError(f"{field_name} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _load_manifest(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("sources", [])
    if not isinstance(raw, list):
        raise ValueError("manifest must be a JSON list or an object with a 'sources' list")
    return [dict(item) for item in raw if isinstance(item, dict)]


def build_source_registry(
    inputs: Iterable[str],
    *,
    manifest_path: str | None = None,
    rights_status: str = "unknown",
    sensitivity: str = "internal",
    routing_policy: str = "local_only",
    origin: str = "unspecified",
) -> list[SourceEntry]:
    manifest_entries = _load_manifest(manifest_path)
    raw_entries: list[dict[str, Any]] = []
    raw_entries.extend(manifest_entries)
    raw_entries.extend({"path": p} for p in inputs)
    if not raw_entries:
        raise ValueError("provide at least one input path or a manifest with sources")

    used_ids: set[str] = set()
    registry: list[SourceEntry] = []
    for item in raw_entries:
        path = str(item.get("path") or item.get("uri_or_path") or "").strip()
        if not path:
            raise ValueError("every source needs a path or uri_or_path")
        p = Path(path).expanduser()
        entry_rights = _validate_choice(str(item.get("rights_status") or rights_status), RIGHTS_STATUSES, "rights_status")
        entry_sensitivity = _validate_choice(str(item.get("sensitivity") or sensitivity), SENSITIVITIES, "sensitivity")
        entry_routing = _validate_choice(str(item.get("routing_policy") or routing_policy), ROUTING_POLICIES, "routing_policy")
        source_type = infer_source_type(path, str(item.get("source_type") or ""))
        if source_type not in SUPPORTED_TEXT_TYPES:
            source_type = str(source_type or "text")
        explicit_source_id = str(item.get("source_id") or "").strip()
        if explicit_source_id:
            source_id = explicit_source_id
            if source_id in used_ids:
                source_id = _stable_source_id(f"{p}-{len(used_ids)}", used_ids)
            else:
                used_ids.add(source_id)
        else:
            source_id = _stable_source_id(str(p), used_ids)
        title = str(item.get("title") or p.stem or source_id)
        data = p.read_bytes() if p.exists() and p.is_file() else b""
        registry.append(
            SourceEntry(
                source_id=source_id,
                path=str(p),
                source_type=source_type,
                title=title,
                origin=str(item.get("origin") or origin),
                rights_status=entry_rights,
                sensitivity=entry_sensitivity,
                retention_policy=str(item.get("retention_policy") or "pilot_derived_90d"),
                retention_expires_at=str(item.get("retention_expires_at") or ""),
                routing_policy=entry_routing,
                rights_released_by=str(item.get("rights_released_by") or ""),
                hash_or_snapshot=str(item.get("hash_or_snapshot") or (_sha256_bytes(data) if data else "")),
                size_bytes=len(data),
                created_at=_utc_now(),
            )
        )
    return registry


def _quality_status(text: str, has_location: bool = True) -> tuple[str, int | None]:
    stripped = text.strip()
    if not stripped:
        return "failed", 0
    if not has_location:
        return "uncertain", 60
    if len(stripped) < 40:
        return "partial", 75
    return "ok", 95


def _first_quote(text: str, limit: int = 180) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return text.strip()[:limit]


def _integrity_terms(text: str, limit: int = 50) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in EXACT_TOKEN_RE.finditer(text):
        token = match.group(0).strip().rstrip(".,;")
        if token and token not in seen:
            seen.add(token)
            out.append(token)
            if len(out) >= limit:
                break
    return out


def _chunk_text(text: str, *, chunk_chars: int) -> list[tuple[str, int, int]]:
    lines = text.splitlines()
    chunks: list[tuple[str, int, int]] = []
    buf: list[str] = []
    start_line = 1
    current = 0
    for idx, line in enumerate(lines, start=1):
        extra = len(line) + 1
        if buf and current + extra > chunk_chars:
            chunks.append(("\n".join(buf).strip(), start_line, idx - 1))
            buf = []
            start_line = idx
            current = 0
        buf.append(line)
        current += extra
    if buf:
        chunks.append(("\n".join(buf).strip(), start_line, len(lines) or 1))
    return [(c, s, e) for c, s, e in chunks if c]


def _html_to_text(raw: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    return parser.text()


def _xml_text_nodes(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except Exception:
        return [_normalize_ws(re.sub(r"<[^>]+>", " ", xml_text))]
    values: list[str] = []
    for elem in root.iter():
        local = elem.tag.rsplit("}", 1)[-1]
        if local in {"t", "instrText"} and elem.text:
            values.append(elem.text)
        elif local in {"tab"}:
            values.append("\t")
        elif local in {"br", "p", "tr"}:
            values.append("\n")
    return values


def _extract_markdown_or_text(source: SourceEntry, *, chunk_chars: int) -> list[DocumentUnit]:
    text = _normalize_ws(_read_text_file(Path(source.path)))
    return _units_from_text(source, text, extractor=f"stdlib-{source.source_type}", chunk_chars=chunk_chars)


def _extract_html(source: SourceEntry, *, chunk_chars: int) -> list[DocumentUnit]:
    return _units_from_text(source, _html_to_text(_read_text_file(Path(source.path))), extractor="stdlib-htmlparser", chunk_chars=chunk_chars)


def _extract_transcript(source: SourceEntry, *, chunk_chars: int) -> list[DocumentUnit]:
    raw = _read_text_file(Path(source.path))
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.isdigit() or "-->" in stripped or stripped.upper().startswith("WEBVTT"):
            continue
        lines.append(stripped)
    text = _normalize_ws("\n".join(lines))
    return _units_from_text(source, text, extractor="stdlib-transcript", chunk_chars=chunk_chars)


def _zip_failed_unit(source: SourceEntry, extractor: str, error: str) -> list[DocumentUnit]:
    return [
        DocumentUnit(
            unit_id=f"{source.source_id}:unsupported",
            source_id=source.source_id,
            source_type=source.source_type,
            location={"path": source.path},
            extractor=extractor,
            extraction_status="failed",
            confidence=0,
            text_sha256="",
            char_count=0,
            quote="",
            text_preview=error,
            context={"error": error},
            integrity_terms=[],
        )
    ]


def _extract_docx(source: SourceEntry, *, chunk_chars: int) -> list[DocumentUnit]:
    try:
        with zipfile.ZipFile(source.path) as zf:
            xml_text = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception as exc:
        return _zip_failed_unit(source, "stdlib-docx", f"DOCX extraction failed: {exc}")
    text = _normalize_ws("".join(_xml_text_nodes(xml_text)))
    return _units_from_text(source, text, extractor="stdlib-docx", chunk_chars=chunk_chars)


def _extract_pptx(source: SourceEntry, *, chunk_chars: int) -> list[DocumentUnit]:
    try:
        with zipfile.ZipFile(source.path) as zf:
            slide_names = sorted(n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
            units: list[DocumentUnit] = []
            for idx, name in enumerate(slide_names, start=1):
                xml_text = zf.read(name).decode("utf-8", errors="replace")
                text = _normalize_ws("".join(_xml_text_nodes(xml_text)))
                units.extend(
                    _units_from_text(
                        source,
                        text,
                        extractor="stdlib-pptx",
                        chunk_chars=chunk_chars,
                        location_base={"slide": idx, "object": name, "path": source.path},
                    )
                )
            return units or _units_from_text(source, "", extractor="stdlib-pptx", chunk_chars=chunk_chars)
    except Exception as exc:
        return _zip_failed_unit(source, "stdlib-pptx", f"PPTX extraction failed: {exc}")


def _extract_epub(source: SourceEntry, *, chunk_chars: int) -> list[DocumentUnit]:
    try:
        with zipfile.ZipFile(source.path) as zf:
            names = sorted(
                n for n in zf.namelist()
                if n.lower().endswith((".xhtml", ".html", ".htm")) and not n.endswith("/")
            )
            units: list[DocumentUnit] = []
            for idx, name in enumerate(names, start=1):
                raw = zf.read(name).decode("utf-8", errors="replace")
                text = _html_to_text(raw)
                units.extend(
                    _units_from_text(
                        source,
                        text,
                        extractor="stdlib-epub",
                        chunk_chars=chunk_chars,
                        location_base={"chapter": name, "chapter_index": idx, "path": source.path},
                    )
                )
            return units or _units_from_text(source, "", extractor="stdlib-epub", chunk_chars=chunk_chars)
    except Exception as exc:
        return _zip_failed_unit(source, "stdlib-epub", f"EPUB extraction failed: {exc}")


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        raw = zf.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
    except KeyError:
        return []
    try:
        root = ET.fromstring(raw.encode("utf-8"))
    except Exception:
        return []
    strings: list[str] = []
    for si in root.iter():
        if si.tag.rsplit("}", 1)[-1] != "si":
            continue
        parts = [e.text or "" for e in si.iter() if e.tag.rsplit("}", 1)[-1] == "t"]
        strings.append("".join(parts))
    return strings


def _extract_xlsx(source: SourceEntry, *, chunk_chars: int) -> list[DocumentUnit]:
    try:
        with zipfile.ZipFile(source.path) as zf:
            shared = _xlsx_shared_strings(zf)
            sheets = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
            units: list[DocumentUnit] = []
            for sheet_index, name in enumerate(sheets, start=1):
                raw = zf.read(name).decode("utf-8", errors="replace")
                try:
                    root = ET.fromstring(raw.encode("utf-8"))
                except Exception:
                    text = _normalize_ws(re.sub(r"<[^>]+>", " ", raw))
                else:
                    cells: list[str] = []
                    for cell in root.iter():
                        if cell.tag.rsplit("}", 1)[-1] != "c":
                            continue
                        ref = cell.attrib.get("r", "?")
                        cell_type = cell.attrib.get("t", "")
                        formula = ""
                        value = ""
                        for child in cell:
                            local = child.tag.rsplit("}", 1)[-1]
                            if local == "f" and child.text:
                                formula = child.text
                            if local == "v" and child.text:
                                value = child.text
                        if cell_type == "s" and value.isdigit() and int(value) < len(shared):
                            value = shared[int(value)]
                        rendered = f"{ref}={value}"
                        if formula:
                            rendered += f" (formula={formula})"
                        cells.append(rendered)
                    text = _normalize_ws("\n".join(cells))
                units.extend(
                    _units_from_text(
                        source,
                        text,
                        extractor="stdlib-xlsx",
                        chunk_chars=chunk_chars,
                        location_base={"sheet": name, "sheet_index": sheet_index, "path": source.path},
                    )
                )
            return units or _units_from_text(source, "", extractor="stdlib-xlsx", chunk_chars=chunk_chars)
    except Exception as exc:
        return _zip_failed_unit(source, "stdlib-xlsx", f"XLSX extraction failed: {exc}")


def _extract_pdf(source: SourceEntry, *, chunk_chars: int) -> list[DocumentUnit]:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return _zip_failed_unit(source, "pymupdf-missing", "PDF extraction requires optional PyMuPDF/fitz; no external processing performed.")
    doc = fitz.open(source.path)
    units: list[DocumentUnit] = []
    for page_index, page in enumerate(doc, start=1):
        text = _normalize_ws(page.get_text("text") or "")
        units.extend(
            _units_from_text(
                source,
                text,
                extractor="pymupdf",
                chunk_chars=chunk_chars,
                location_base={"page": page_index, "path": source.path},
            )
        )
    return units or _units_from_text(source, "", extractor="pymupdf", chunk_chars=chunk_chars)


def _units_from_text(
    source: SourceEntry,
    text: str,
    *,
    extractor: str,
    chunk_chars: int,
    location_base: Mapping[str, Any] | None = None,
) -> list[DocumentUnit]:
    status, confidence = _quality_status(text)
    chunks = _chunk_text(text, chunk_chars=max(500, chunk_chars)) or [("", 1, 1)]
    units: list[DocumentUnit] = []
    for idx, (chunk, line_start, line_end) in enumerate(chunks, start=1):
        unit_status, unit_conf = _quality_status(chunk)
        if status == "failed":
            unit_status, unit_conf = status, confidence
        location = dict(location_base or {})
        location.update({"path": source.path, "line_start": line_start, "line_end": line_end, "chunk_index": idx})
        units.append(
            DocumentUnit(
                unit_id=f"{source.source_id}:chunk-{idx}",
                source_id=source.source_id,
                source_type=source.source_type,
                location=location,
                extractor=extractor,
                extraction_status=unit_status,
                confidence=unit_conf,
                text_sha256=_sha256_text(chunk),
                char_count=len(chunk),
                quote=_first_quote(chunk),
                text_preview=chunk,
                context={},
                integrity_terms=_integrity_terms(chunk),
            )
        )
    return units


def extract_sources(registry: Iterable[SourceEntry], *, chunk_chars: int = 6000) -> list[DocumentUnit]:
    units: list[DocumentUnit] = []
    for source in registry:
        p = Path(source.path)
        if not p.exists() or not p.is_file():
            units.append(
                DocumentUnit(
                    unit_id=f"{source.source_id}:missing",
                    source_id=source.source_id,
                    source_type=source.source_type,
                    location={"path": source.path},
                    extractor="none",
                    extraction_status="failed",
                    confidence=0,
                    text_sha256="",
                    char_count=0,
                    quote="",
                    text_preview="",
                    context={"error": "source file does not exist or is not a regular file"},
                    integrity_terms=[],
                )
            )
            continue
        if source.source_type == "html":
            units.extend(_extract_html(source, chunk_chars=chunk_chars))
        elif source.source_type == "transcript":
            units.extend(_extract_transcript(source, chunk_chars=chunk_chars))
        elif source.source_type == "pdf":
            units.extend(_extract_pdf(source, chunk_chars=chunk_chars))
        elif source.source_type == "docx":
            units.extend(_extract_docx(source, chunk_chars=chunk_chars))
        elif source.source_type == "pptx":
            units.extend(_extract_pptx(source, chunk_chars=chunk_chars))
        elif source.source_type == "xlsx":
            units.extend(_extract_xlsx(source, chunk_chars=chunk_chars))
        elif source.source_type == "epub":
            units.extend(_extract_epub(source, chunk_chars=chunk_chars))
        else:
            units.extend(_extract_markdown_or_text(source, chunk_chars=chunk_chars))
    return units


def _external_block_reason(source: SourceEntry) -> str | None:
    if source.routing_policy == "blocked":
        return "routing_policy=blocked"
    if source.routing_policy == "metadata_only":
        return "routing_policy=metadata_only forbids sending extracted text"
    if source.routing_policy != "external_allowed":
        return f"routing_policy={source.routing_policy}"
    if source.rights_status != "allowed":
        return f"rights_status={source.rights_status}"
    if source.sensitivity in {"sensitive", "highly_sensitive"}:
        return f"sensitivity={source.sensitivity}"
    return None


def external_processing_gate(registry: Iterable[SourceEntry]) -> tuple[bool, list[dict[str, str]]]:
    blocks = []
    for source in registry:
        reason = _external_block_reason(source)
        if reason:
            blocks.append({"source_id": source.source_id, "reason": reason})
    return not blocks, blocks


def metadata_planning_gate(registry: Iterable[SourceEntry]) -> tuple[bool, list[dict[str, str]]]:
    blocks = []
    for source in registry:
        if source.routing_policy == "blocked":
            blocks.append({"source_id": source.source_id, "reason": "routing_policy=blocked"})
    return not blocks, blocks


def _compact_payload(registry: list[SourceEntry], units: list[DocumentUnit], *, max_chars: int, include_text: bool = True) -> str:
    unit_payload = []
    budget = max_chars
    for unit in units:
        item = {
            "unit_id": unit.unit_id,
            "source_id": unit.source_id,
            "source_type": unit.source_type,
            "location": unit.location,
            "extractor": unit.extractor,
            "extraction_status": unit.extraction_status,
            "confidence": unit.confidence,
            "quote": unit.quote,
            "integrity_terms": unit.integrity_terms[:20],
        }
        if include_text:
            item["text"] = unit.text_preview
        text = json.dumps(item, ensure_ascii=False)
        if len(text) > budget:
            break
        unit_payload.append(item)
        budget -= len(text)
    payload = {
        "sources": [asdict(s) for s in registry],
        "units": unit_payload,
        "truncated_for_model": len(unit_payload) < len(units),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _instructions_for_task(task: str) -> str:
    if task == "document_merge_draft":
        return (
            "You draft a proposal-only merge of a read-only document corpus. Return strict JSON only. "
            "Do not write final notes. Include draft_markdown and change_manifest with kept_claims, "
            "changed_claims, deleted_claims, conflicts, uncertainties, and source anchors for every claim. "
            "Every material claim must include source_id, unit_id, location, and exact_quote. Preserve exact "
            "numbers, dates, paths, URLs, commands, formulas, and code tokens."
        )
    if task == "document_integrity_check":
        return (
            "You review a document proposal for source-grounding only. Return strict JSON only. "
            "Flag unsupported, paraphrased, missing, or conflicting claims. Include claim, status, "
            "reason, source_id, unit_id, location, and exact_quote for each reviewed material claim."
        )
    if task == "document_corpus_planner":
        return (
            "You plan a safe read-only corpus-processing run from metadata only. Return strict JSON only. "
            "Include adapter_plan, risk_register, suggested_routing, retention_notes, and next_steps. "
            "Do not infer private content that is not present in the metadata."
        )
    return (
        "You summarize a read-only document corpus. Return strict JSON only. "
        "Do not invent claims. Every material claim must include source_id, unit_id, "
        "location, and an exact_quote copied from the provided text. Preserve exact "
        "numbers, dates, paths, URLs, commands, formulas, and code tokens. Include: "
        "source_summaries, material_claims, conflicts, uncertainties, recommended_next_steps."
    )


def _run_auxiliary_task(
    task: str,
    registry: list[SourceEntry],
    units: list[DocumentUnit],
    *,
    max_payload_chars: int,
    payload_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if task not in AUXILIARY_TASKS:
        raise ValueError(f"unsupported auxiliary task: {task}")
    if task == "document_corpus_planner":
        ok, blocks = metadata_planning_gate(registry)
        include_text = False
    else:
        ok, blocks = external_processing_gate(registry)
        include_text = True
    if not ok:
        return {"status": "skipped", "reason": "external gate blocked", "blocks": blocks, "task": task}
    if include_text and any(u.extraction_status == "failed" for u in units):
        return {"status": "skipped", "reason": "one or more extractions failed; fail-closed before external model call", "task": task}

    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    if payload_override is None:
        user_payload = _compact_payload(registry, units, max_chars=max_payload_chars, include_text=include_text)
    else:
        user_payload = json.dumps(payload_override, ensure_ascii=False, indent=2)
    response = call_llm(
        task=task,
        messages=[
            {"role": "system", "content": _instructions_for_task(task)},
            {"role": "user", "content": user_payload},
        ],
        max_tokens=3500 if task != "document_merge_draft" else 5000,
        temperature=0.1,
        timeout=300 if task in {"document_merge_draft", "document_integrity_check"} else 180,
    )
    text = (extract_content_or_reasoning(response) or "").strip()
    try:
        parsed = json.loads(_strip_json_fence(text))
    except Exception:
        parsed = {"raw_text": text}
    return {"status": "ok", "task": task, "result": parsed}


def _strip_json_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return text


def _local_summary(registry: list[SourceEntry], units: list[DocumentUnit]) -> dict[str, Any]:
    by_source: dict[str, list[DocumentUnit]] = {}
    for unit in units:
        by_source.setdefault(unit.source_id, []).append(unit)
    source_summaries = []
    for source in registry:
        src_units = by_source.get(source.source_id, [])
        source_summaries.append(
            {
                "source_id": source.source_id,
                "title": source.title,
                "source_type": source.source_type,
                "path": source.path,
                "unit_count": len(src_units),
                "char_count": sum(u.char_count for u in src_units),
                "statuses": sorted({u.extraction_status for u in src_units}),
                "quotes": [u.quote for u in src_units[:5] if u.quote],
                "integrity_terms": sorted({t for u in src_units for t in u.integrity_terms})[:50],
            }
        )
    return {"source_summaries": source_summaries}


def _empty_write_boundary() -> dict[str, bool | str]:
    return {
        "source_artifacts_mutated": False,
        "final_output_written": False,
        "output_is_proposal_only": True,
        "boundary": "auxiliary/document CLI emits reports/proposals only; callers perform any final write separately after validation",
    }


def _material_claims(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, dict):
        claims = obj.get("material_claims")
        if isinstance(claims, list):
            return [c for c in claims if isinstance(c, dict)]
        out: list[dict[str, Any]] = []
        for value in obj.values():
            out.extend(_material_claims(value))
        return out
    if isinstance(obj, list):
        out: list[dict[str, Any]] = []
        for item in obj:
            out.extend(_material_claims(item))
        return out
    return []


def deterministic_integrity_check(report: Mapping[str, Any]) -> dict[str, Any]:
    units = report.get("units") or []
    unit_text = {
        str(unit.get("unit_id")): str(unit.get("text_preview") or "")
        for unit in units if isinstance(unit, dict)
    }
    aux = report.get("auxiliary") or {}
    claims = _material_claims(aux.get("result") if isinstance(aux, dict) else aux)
    failed: list[dict[str, Any]] = []
    checked = 0
    for idx, claim in enumerate(claims):
        checked += 1
        unit_id = str(claim.get("unit_id") or "")
        quote = str(claim.get("exact_quote") or "")
        if not unit_id or unit_id not in unit_text:
            failed.append({"index": idx, "claim": claim.get("claim"), "reason": "missing_or_unknown_unit_id"})
            continue
        if not quote:
            failed.append({"index": idx, "claim": claim.get("claim"), "unit_id": unit_id, "reason": "missing_exact_quote"})
            continue
        if quote not in unit_text[unit_id]:
            failed.append({"index": idx, "claim": claim.get("claim"), "unit_id": unit_id, "exact_quote": quote, "reason": "exact_quote_not_found_in_unit_text"})
    return {
        "status": "failed" if failed else "ok",
        "checked_claims": checked,
        "failed_claims": failed,
    }


def _source_from_mapping(item: Mapping[str, Any]) -> SourceEntry:
    data = {field.name: item.get(field.name) for field in SourceEntry.__dataclass_fields__.values()}
    return SourceEntry(**data)  # type: ignore[arg-type]


def _unit_from_mapping(item: Mapping[str, Any]) -> DocumentUnit:
    data = {field.name: item.get(field.name) for field in DocumentUnit.__dataclass_fields__.values()}
    return DocumentUnit(**data)  # type: ignore[arg-type]


def run_integrity_check_report(
    report: Mapping[str, Any],
    *,
    use_auxiliary: bool = False,
    max_payload_chars: int = 60000,
) -> dict[str, Any]:
    """Run deterministic and optional auxiliary integrity review over a report.

    The deterministic layer is fully local. The auxiliary layer is gated by the
    original source registry, so a private/local-only report cannot accidentally
    send extracted text to ``document_integrity_check``.
    """
    deterministic = deterministic_integrity_check(report)
    registry = [_source_from_mapping(s) for s in report.get("registry", []) if isinstance(s, Mapping)]
    units = [_unit_from_mapping(u) for u in report.get("units", []) if isinstance(u, Mapping)]
    result: dict[str, Any] = {
        "schema_version": "document-integrity-check-v1",
        "created_at": _utc_now(),
        "mode": "read_only",
        "deterministic": deterministic,
        "auxiliary": {"status": "not_requested", "task": "document_integrity_check"},
        "write_boundary": _empty_write_boundary(),
    }
    if use_auxiliary:
        if not registry:
            result["auxiliary"] = {"status": "skipped", "task": "document_integrity_check", "reason": "report has no source registry"}
        else:
            result["auxiliary"] = _run_auxiliary_task(
                "document_integrity_check",
                registry,
                units,
                max_payload_chars=max_payload_chars,
                payload_override={"report": report, "deterministic_integrity_check": deterministic},
            )
    return result


def run_document_pilot(
    inputs: Iterable[str],
    *,
    manifest_path: str | None = None,
    rights_status: str = "unknown",
    sensitivity: str = "internal",
    routing_policy: str = "local_only",
    origin: str = "unspecified",
    use_auxiliary: bool = False,
    auxiliary_task: str = "document_summarization",
    chunk_chars: int = 6000,
    max_payload_chars: int = 60000,
) -> dict[str, Any]:
    registry = build_source_registry(
        inputs,
        manifest_path=manifest_path,
        rights_status=rights_status,
        sensitivity=sensitivity,
        routing_policy=routing_policy,
        origin=origin,
    )
    units = extract_sources(registry, chunk_chars=chunk_chars)
    gate_ok, gate_blocks = external_processing_gate(registry)
    result: dict[str, Any] = {
        "schema_version": "document-pilot-v1",
        "created_at": _utc_now(),
        "mode": "read_only",
        "registry": [asdict(s) for s in registry],
        "units": [asdict(u) for u in units],
        "external_gate": {"allowed": gate_ok, "blocks": gate_blocks},
        "local_summary": _local_summary(registry, units),
        "auxiliary": {"status": "not_requested", "task": auxiliary_task},
        "write_boundary": _empty_write_boundary(),
    }
    if use_auxiliary:
        result["auxiliary"] = _run_auxiliary_task(auxiliary_task, registry, units, max_payload_chars=max_payload_chars)
        if auxiliary_task in {"document_summarization", "document_merge_draft"}:
            result["integrity_check"] = deterministic_integrity_check(result)
    return result


def run_corpus_plan(
    inputs: Iterable[str],
    *,
    manifest_path: str | None = None,
    rights_status: str = "unknown",
    sensitivity: str = "internal",
    routing_policy: str = "metadata_only",
    origin: str = "unspecified",
    use_auxiliary: bool = False,
    max_payload_chars: int = 40000,
) -> dict[str, Any]:
    registry = build_source_registry(
        inputs,
        manifest_path=manifest_path,
        rights_status=rights_status,
        sensitivity=sensitivity,
        routing_policy=routing_policy,
        origin=origin,
    )
    ok, blocks = metadata_planning_gate(registry)
    result: dict[str, Any] = {
        "schema_version": "document-plan-v1",
        "created_at": _utc_now(),
        "mode": "metadata_only",
        "registry": [asdict(s) for s in registry],
        "external_gate": {"allowed": ok, "blocks": blocks},
        "auxiliary": {"status": "not_requested", "task": "document_corpus_planner"},
        "write_boundary": _empty_write_boundary(),
    }
    if use_auxiliary:
        result["auxiliary"] = _run_auxiliary_task("document_corpus_planner", registry, [], max_payload_chars=max_payload_chars)
    return result


def render_markdown_report(result: Mapping[str, Any]) -> str:
    title = "Document processing pilot report" if result.get("schema_version") != "document-plan-v1" else "Document corpus plan report"
    lines = [
        f"# {title}",
        "",
        f"Created: {result.get('created_at', '')}",
        f"Mode: {result.get('mode', 'read_only')}",
        "",
        "## External processing gate",
        "",
        f"Allowed: {result.get('external_gate', {}).get('allowed')}",
    ]
    blocks = result.get("external_gate", {}).get("blocks") or []
    if blocks:
        lines.append("")
        for block in blocks:
            lines.append(f"- {block.get('source_id')}: {block.get('reason')}")
    lines.extend(["", "## Sources", ""])
    for source in result.get("registry", []):
        lines.append(f"- `{source.get('source_id')}` — {source.get('title')} ({source.get('source_type')})")
        lines.append(f"  - path: `{source.get('path')}`")
        lines.append(f"  - rights/sensitivity/routing: {source.get('rights_status')} / {source.get('sensitivity')} / {source.get('routing_policy')}")
    summaries = result.get("local_summary", {}).get("source_summaries", [])
    if summaries:
        lines.extend(["", "## Local summaries", ""])
        for summary in summaries:
            lines.append(f"### {summary.get('source_id')} — {summary.get('title')}")
            lines.append(f"- units: {summary.get('unit_count')}; chars: {summary.get('char_count')}; statuses: {', '.join(summary.get('statuses') or [])}")
            quotes = summary.get("quotes") or []
            if quotes:
                lines.append("- sample quotes:")
                for quote in quotes:
                    lines.append(f"  - “{quote}”")
            terms = summary.get("integrity_terms") or []
            if terms:
                lines.append(f"- exact tokens: {', '.join('`' + t + '`' for t in terms[:20])}")
            lines.append("")
    if result.get("integrity_check"):
        check = result.get("integrity_check") or {}
        lines.extend(["", "## Deterministic integrity check", ""])
        lines.append(f"Status: {check.get('status')}; checked claims: {check.get('checked_claims')}")
        for failure in check.get("failed_claims") or []:
            lines.append(f"- {failure.get('reason')}: {failure.get('claim')}")
    lines.extend(["", "## Write boundary", ""])
    boundary = result.get("write_boundary") or {}
    lines.append(f"Proposal only: {boundary.get('output_is_proposal_only')}")
    lines.append(f"Source artifacts mutated: {boundary.get('source_artifacts_mutated')}")
    lines.extend(["", "## Auxiliary result", ""])
    aux = result.get("auxiliary") or {}
    lines.append(f"Status: {aux.get('status')}")
    if aux.get("task"):
        lines.append(f"Task: {aux.get('task')}")
    if aux.get("reason"):
        lines.append(f"Reason: {aux.get('reason')}")
    if aux.get("result"):
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(aux.get("result"), ensure_ascii=False, indent=2))
        lines.append("```")
    return "\n".join(lines).rstrip() + "\n"


def render_integrity_markdown(check: Mapping[str, Any]) -> str:
    deterministic_obj = check.get("deterministic")
    deterministic: Mapping[str, Any] = deterministic_obj if isinstance(deterministic_obj, Mapping) else check
    lines = [
        "# Document integrity check",
        "",
        f"Status: {deterministic.get('status')}",
        f"Checked claims: {deterministic.get('checked_claims')}",
    ]
    failed = deterministic.get("failed_claims") or []
    if failed:
        lines.extend(["", "## Failed claims"])
        for item in failed:
            lines.append(f"- {item.get('reason')}: {item.get('claim')}")
    aux = check.get("auxiliary") if isinstance(check.get("auxiliary"), Mapping) else None
    if aux:
        lines.extend(["", "## Auxiliary result", "", f"Status: {aux.get('status')}"])
        if aux.get("task"):
            lines.append(f"Task: {aux.get('task')}")
        if aux.get("reason"):
            lines.append(f"Reason: {aux.get('reason')}")
    return "\n".join(lines).rstrip() + "\n"


def _write_or_print(content: str, output: str | None) -> None:
    if output:
        Path(output).expanduser().write_text(content, encoding="utf-8")
        print(f"Wrote {output}")
    else:
        print(content, end="")


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("inputs", nargs="*", help="Input files (Markdown/text/HTML/transcript/PDF/DOCX/PPTX/XLSX/EPUB where supported)")
    parser.add_argument("--manifest", help="JSON list or {sources: [...]} with source metadata")
    parser.add_argument("--output", "-o", help="Write report to this path instead of stdout")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    parser.add_argument("--use-auxiliary", action="store_true", help="Call the configured auxiliary document task if the gate allows it")
    parser.add_argument("--rights-status", choices=sorted(RIGHTS_STATUSES), default="unknown")
    parser.add_argument("--sensitivity", choices=sorted(SENSITIVITIES), default="internal")
    parser.add_argument("--routing-policy", choices=sorted(ROUTING_POLICIES), default="local_only")
    parser.add_argument("--origin", default="unspecified")
    parser.add_argument("--chunk-chars", type=int, default=6000)
    parser.add_argument("--max-payload-chars", type=int, default=60000)


def register_documents_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "documents",
        aliases=["docs"],
        help="Read-only document/corpus processing pilot",
        description="Build a source registry, extract local text with anchors, and optionally call auxiliary.document_* tasks after a fail-closed rights/privacy gate.",
    )
    child = parser.add_subparsers(dest="documents_command")
    summarize = child.add_parser("summarize", aliases=["pilot"], help="Run the read-only summarization pilot")
    _add_source_args(summarize)
    merge = child.add_parser("merge-draft", aliases=["merge"], help="Create a proposal-only merge draft")
    _add_source_args(merge)
    plan = child.add_parser("plan", help="Plan a metadata-only corpus processing run")
    _add_source_args(plan)
    plan.set_defaults(routing_policy="metadata_only")
    integrity = child.add_parser("integrity-check", aliases=["check"], help="Run deterministic checks over a JSON report/proposal")
    integrity.add_argument("report", help="JSON report produced by hermes documents")
    integrity.add_argument("--output", "-o", help="Write check result to this path instead of stdout")
    integrity.add_argument("--format", choices=["json", "markdown"], default="markdown")
    integrity.add_argument("--use-auxiliary", action="store_true", help="Also call auxiliary.document_integrity_check if the report's source gate allows it")
    integrity.add_argument("--max-payload-chars", type=int, default=60000)
    parser.set_defaults(func=cmd_documents)


def _emit_report(result: Mapping[str, Any], fmt: str, output: str | None) -> None:
    if fmt == "json":
        content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    else:
        content = render_markdown_report(result)
    _write_or_print(content, output)


def cmd_documents(args: argparse.Namespace) -> int:
    sub = getattr(args, "documents_command", None)
    if sub in {"summarize", "pilot", "merge-draft", "merge"}:
        task = "document_merge_draft" if sub in {"merge-draft", "merge"} else "document_summarization"
        result = run_document_pilot(
            getattr(args, "inputs", []) or [],
            manifest_path=getattr(args, "manifest", None),
            rights_status=getattr(args, "rights_status", "unknown"),
            sensitivity=getattr(args, "sensitivity", "internal"),
            routing_policy=getattr(args, "routing_policy", "local_only"),
            origin=getattr(args, "origin", "unspecified"),
            use_auxiliary=bool(getattr(args, "use_auxiliary", False)),
            auxiliary_task=task,
            chunk_chars=int(getattr(args, "chunk_chars", 6000) or 6000),
            max_payload_chars=int(getattr(args, "max_payload_chars", 60000) or 60000),
        )
        _emit_report(result, getattr(args, "format", "markdown"), getattr(args, "output", None))
        return 0
    if sub == "plan":
        result = run_corpus_plan(
            getattr(args, "inputs", []) or [],
            manifest_path=getattr(args, "manifest", None),
            rights_status=getattr(args, "rights_status", "unknown"),
            sensitivity=getattr(args, "sensitivity", "internal"),
            routing_policy=getattr(args, "routing_policy", "metadata_only"),
            origin=getattr(args, "origin", "unspecified"),
            use_auxiliary=bool(getattr(args, "use_auxiliary", False)),
            max_payload_chars=int(getattr(args, "max_payload_chars", 40000) or 40000),
        )
        _emit_report(result, getattr(args, "format", "markdown"), getattr(args, "output", None))
        return 0
    if sub in {"integrity-check", "check"}:
        report = json.loads(Path(getattr(args, "report")).read_text(encoding="utf-8"))
        check = run_integrity_check_report(
            report,
            use_auxiliary=bool(getattr(args, "use_auxiliary", False)),
            max_payload_chars=int(getattr(args, "max_payload_chars", 60000) or 60000),
        )
        if getattr(args, "format", "markdown") == "json":
            content = json.dumps(check, ensure_ascii=False, indent=2) + "\n"
        else:
            content = render_integrity_markdown(check)
        _write_or_print(content, getattr(args, "output", None))
        return 0
    print("Usage: hermes documents {summarize|merge-draft|plan|integrity-check} [options]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="python -m hermes_cli.document_processing")
    sub = p.add_subparsers(dest="command")
    register_documents_parser(sub)
    ns = p.parse_args()
    if getattr(ns, "func", None):
        raise SystemExit(ns.func(ns))
    p.print_help()
    raise SystemExit(2)
