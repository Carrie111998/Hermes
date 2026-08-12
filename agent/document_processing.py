"""Local document → Markdown processing.

Everything here runs on the machine that holds the file. No uploaded content
is ever sent to a hosted parser; ``anydoc`` (the ``firecrawl-anydoc`` wheel) is
an in-process Rust extension, and the optional advanced PDF path is a local
model too.

Callers get a :class:`DocumentProcessingResult` with a stable
:class:`ProcessingDisposition` and a stable ``reason_code``. Those codes are the
contract every surface (server coordinator, profile store, context references)
maps to product-safe copy — never render ``diagnostic`` to a customer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "DocumentProcessingResult",
    "ProcessingDisposition",
    "READABLE_TEXT_EXTENSIONS",
    "SUPPORTED_DOCUMENT_EXTENSIONS",
    "is_processable_document",
    "process_document",
]

# Formats anydoc parses from bytes. Kept explicit rather than probing the
# library so the upload gate stays a pure string check with no import cost.
ANYDOC_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".csv",
        ".rtf",
        ".odt",
        ".ods",
        ".odp",
        ".epub",
    }
)

# Already agent-readable: decode and hand straight to the model.
READABLE_TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".text",
        ".json",
        ".jsonl",
        ".ndjson",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".log",
        ".tsv",
        ".xml",
        ".html",
        ".htm",
    }
)

SUPPORTED_DOCUMENT_EXTENSIONS = ANYDOC_EXTENSIONS | READABLE_TEXT_EXTENSIONS

# Signature-less formats anydoc cannot sniff — it needs the extension.
_EXTENSION_ONLY_FORMATS = {".csv"}

# Content types that identify a document when the filename has no useful
# extension (browser uploads of "blob", messaging attachments, etc.).
_CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/epub+zip": ".epub",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/html": ".html",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
}

_MAX_DIAGNOSTIC_CHARS = 300


class ProcessingDisposition(str, Enum):
    CONVERTED = "converted"
    PASSTHROUGH = "passthrough"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"


@dataclass(frozen=True)
class DocumentProcessingResult:
    disposition: ProcessingDisposition
    markdown: str | None = None
    source_format: str | None = None
    reason_code: str | None = None
    diagnostic: str | None = None
    used_fallback: bool = False

    @property
    def ok(self) -> bool:
        return self.disposition in (
            ProcessingDisposition.CONVERTED,
            ProcessingDisposition.PASSTHROUGH,
        )


def _extension_for(filename: str, content_type: str = "") -> str:
    suffix = os.path.splitext(filename)[1].lower()
    if suffix in SUPPORTED_DOCUMENT_EXTENSIONS:
        return suffix
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXTENSIONS.get(base, suffix)


def is_processable_document(filename: str, content_type: str = "") -> bool:
    """True when this upload has a Markdown form worth producing."""
    return _extension_for(filename, content_type) in SUPPORTED_DOCUMENT_EXTENSIONS


def _anydoc_to_markdown(data: bytes, fmt: str | None) -> str:
    """Seam for tests. Real conversion happens entirely in-process."""
    import anydoc

    if fmt is None:
        return anydoc.to_markdown_bytes(data)
    return anydoc.to_markdown_bytes(data, fmt)


def _advanced_pdf_markdown(path: Path) -> str:
    """Optional local OCR-grade PDF path.

    ``marker`` is a heavyweight optional dependency (torch + models). It is
    never auto-installed — an upload or worker run that reaches here without it
    surfaces ``advanced_processing_unavailable`` and stops.
    """
    from marker.converters.pdf import PdfConverter  # type: ignore[import-not-found]
    from marker.models import create_model_dict  # type: ignore[import-not-found]
    from marker.output import text_from_rendered  # type: ignore[import-not-found]

    converter = PdfConverter(artifact_dict=create_model_dict())
    text, _, _ = text_from_rendered(converter(str(path)))
    return text


def _sanitize(exc: BaseException, payload: bytes | None = None) -> str:
    """Bound and de-identify an exception message.

    Parser errors quote the offending bytes, so any word of the message that
    also appears in the document is dropped before the string reaches logs,
    database rows, or admin JSON. Short words are kept — they carry the
    diagnosis ("at", "byte") and are too generic to identify content.
    """
    haystack = ""
    if payload:
        haystack = payload.decode("utf-8", "replace")

    words = str(exc).split()
    if haystack:
        words = ["[…]" if len(w) >= 4 and w in haystack else w for w in words]
    text = " ".join(words)

    if len(text) > _MAX_DIAGNOSTIC_CHARS:
        text = text[:_MAX_DIAGNOSTIC_CHARS] + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _reason_for(exc: BaseException) -> str:
    """Map an anydoc failure to a stable semantic category."""
    try:
        import anydoc
    except ModuleNotFoundError:
        return "processor_unavailable"

    if isinstance(exc, anydoc.EncryptedError):
        return "encrypted"
    if isinstance(exc, anydoc.ResourceLimitError):
        return "resource_limit"
    if isinstance(exc, (anydoc.MalformedError, anydoc.MissingPartError)):
        return "malformed"
    if isinstance(exc, anydoc.UnsupportedError):
        return "unsupported_format"
    return "conversion_failed"


def process_document(
    path: Path | None = None,
    *,
    data: bytes | None = None,
    filename: str = "",
    use_fallback: bool = True,
) -> DocumentProcessingResult:
    """Produce agent-readable Markdown for one document.

    Exactly one of ``path`` or ``data`` must be given. ``filename`` supplies the
    format hint when reading from ``data``; with ``path`` it defaults to the
    file's own name.
    """
    if (path is None) == (data is None):
        raise ValueError("process_document requires exactly one of path or data")

    if path is not None:
        path = Path(path)
        filename = filename or path.name

    extension = _extension_for(filename)

    if extension in READABLE_TEXT_EXTENSIONS:
        try:
            raw = path.read_bytes() if path is not None else data
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return DocumentProcessingResult(
                ProcessingDisposition.FAILED,
                source_format=extension.lstrip("."),
                reason_code="undecodable_text",
                diagnostic=_sanitize(exc, raw),
            )
        except OSError as exc:
            return DocumentProcessingResult(
                ProcessingDisposition.FAILED,
                source_format=extension.lstrip("."),
                reason_code="unreadable_source",
                diagnostic=_sanitize(exc),
            )
        return DocumentProcessingResult(
            ProcessingDisposition.PASSTHROUGH,
            markdown=text,
            source_format=extension.lstrip("."),
        )

    source_format = extension.lstrip(".") or None
    # Signature-bearing formats auto-detect; CSV and friends must be named.
    fmt_hint = source_format if extension in _EXTENSION_ONLY_FORMATS else None

    try:
        payload = path.read_bytes() if path is not None else data
    except OSError as exc:
        return DocumentProcessingResult(
            ProcessingDisposition.FAILED,
            source_format=source_format,
            reason_code="unreadable_source",
            diagnostic=_sanitize(exc),
        )

    primary_error: BaseException | None = None
    markdown = ""
    try:
        markdown = _anydoc_to_markdown(payload, fmt_hint)
    except BaseException as exc:  # noqa: BLE001 — categorized below
        primary_error = exc

    if primary_error is None and markdown.strip():
        return DocumentProcessingResult(
            ProcessingDisposition.CONVERTED,
            markdown=markdown,
            source_format=source_format,
        )

    # Blank output counts as a primary failure: a scanned PDF converts
    # "successfully" into nothing at all.
    needs_advanced = extension == ".pdf"
    if not (needs_advanced and use_fallback):
        if primary_error is None:
            return DocumentProcessingResult(
                ProcessingDisposition.NEEDS_ATTENTION,
                source_format=source_format,
                reason_code="no_extractable_text",
            )
        return DocumentProcessingResult(
            ProcessingDisposition.FAILED,
            source_format=source_format,
            reason_code=_reason_for(primary_error),
            diagnostic=_sanitize(primary_error, payload),
        )

    return _run_advanced_pdf(path, payload, source_format, primary_error)


def _run_advanced_pdf(
    path: Path | None,
    payload: bytes,
    source_format: str | None,
    primary_error: BaseException | None,
) -> DocumentProcessingResult:
    """Second PDF attempt through the optional local model."""
    import tempfile

    temporary: Path | None = None
    try:
        if path is None:
            handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            try:
                handle.write(payload)
            finally:
                handle.close()
            temporary = Path(handle.name)
            target = temporary
        else:
            target = path

        try:
            recovered = _advanced_pdf_markdown(target)
        except ModuleNotFoundError as exc:
            return DocumentProcessingResult(
                ProcessingDisposition.NEEDS_ATTENTION,
                source_format=source_format,
                reason_code="advanced_processing_unavailable",
                diagnostic=_sanitize(exc),
            )
        except BaseException as exc:  # noqa: BLE001 — categorized below
            return DocumentProcessingResult(
                ProcessingDisposition.FAILED,
                source_format=source_format,
                reason_code=(
                    _reason_for(primary_error)
                    if primary_error is not None
                    else "advanced_processing_failed"
                ),
                diagnostic=_sanitize(exc, payload),
                used_fallback=True,
            )

        if not (recovered or "").strip():
            return DocumentProcessingResult(
                ProcessingDisposition.NEEDS_ATTENTION,
                source_format=source_format,
                reason_code="no_extractable_text",
                used_fallback=True,
            )

        return DocumentProcessingResult(
            ProcessingDisposition.CONVERTED,
            markdown=recovered,
            source_format=source_format,
            used_fallback=True,
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
