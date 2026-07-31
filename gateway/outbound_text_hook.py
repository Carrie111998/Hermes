"""Outbound text-file normalization hook.

The hook preserves the source file and writes a delivery-only copy under the
active Hermes cache. Human-facing text formats get a UTF-8 BOM so Telegram,
Windows Notepad, Excel and other encoding-guessing viewers reliably recognize
Cyrillic. Machine-facing text formats stay plain UTF-8 because a BOM can break
strict parsers and executable scripts.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Formats users commonly open directly in editors/spreadsheets. A UTF-8 BOM is
# intentional here because it fixes Cyrillic autodetection in Telegram viewers,
# Windows Notepad and Excel.
_BOM_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log",
}

# Structured/code formats are text too, but a BOM may violate a parser or a
# shebang. They are transcoded to UTF-8 without adding a BOM.
_PLAIN_UTF8_EXTENSIONS = {
    ".html", ".htm", ".xml", ".css", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".sql", ".py", ".js", ".jsx",
    ".ts", ".tsx", ".sh", ".bash", ".zsh", ".ps1",
}
_TEXT_EXTENSIONS = _BOM_EXTENSIONS | _PLAIN_UTF8_EXTENSIONS
_MAX_TEXT_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class PreparedTextFile:
    path: str
    file_name: str
    changed: bool
    source_encoding: Optional[str] = None
    output_encoding: Optional[str] = None


def _safe_filename(name: str, fallback: str) -> str:
    """Return a Telegram-safe Unicode filename while preserving Cyrillic."""
    name = unicodedata.normalize("NFC", (name or fallback)).strip()
    name = re.sub(r"[\\/\x00-\x1f\x7f]+", "_", name)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^\w.()\-]+", "_", name, flags=re.UNICODE)
    name = re.sub(r"_+", "_", name).strip("._")
    if not name:
        name = fallback
    # Telegram's filename metadata should stay compact and leave room for the
    # extension. Unicode slicing is sufficient because python-telegram-bot will
    # encode the final metadata as UTF-8.
    if len(name) > 180:
        suffix = Path(name).suffix[:20]
        name = name[: 180 - len(suffix)].rstrip("._") + suffix
    return name


def _looks_textual(text: str) -> bool:
    if "\x00" in text:
        return False
    if not text:
        return True
    controls = sum(ord(ch) < 32 and ch not in "\t\r\n\f" for ch in text)
    return controls / max(len(text), 1) < 0.01


def _decode_text(raw: bytes) -> tuple[str, str] | None:
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            return None
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        try:
            return raw.decode("utf-32"), "utf-32"
        except UnicodeDecodeError:
            return None
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            return None
    try:
        text = raw.decode("utf-8")
        return (text, "utf-8") if _looks_textual(text) else None
    except UnicodeDecodeError:
        pass

    # Legacy Russian exports are commonly CP1251, CP866 or KOI8-R. Prefer the
    # candidate with the strongest Cyrillic signal and reject control-heavy
    # decodes. Latin-1 is deliberately not a fallback because it decodes any
    # binary byte stream and would silently corrupt non-text files.
    candidates: list[tuple[int, str, str]] = []
    for encoding in ("cp1251", "cp866", "koi8-r"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if not _looks_textual(text):
            continue
        cyrillic = sum("\u0400" <= ch <= "\u052f" for ch in text)
        printable = sum(ch.isprintable() or ch in "\t\r\n" for ch in text)
        candidates.append((cyrillic * 4 + printable, encoding, text))
    if not candidates:
        return None
    _, encoding, text = max(candidates, key=lambda item: item[0])
    return text, encoding


def prepare_outbound_text_file(
    file_path: str,
    file_name: Optional[str] = None,
) -> PreparedTextFile:
    """Prepare a delivery-only UTF-8 copy when *file_path* is textual.

    Unknown/binary/oversized files are returned unchanged. The source file is
    never modified. Failures are fail-open: delivery continues with the original
    path and a normalized filename, while the exception is logged.
    """
    source = Path(file_path).expanduser()
    fallback_name = source.name or "document.txt"
    display_name = _safe_filename(file_name or fallback_name, fallback_name)
    extension = Path(display_name).suffix.lower() or source.suffix.lower()

    if extension not in _TEXT_EXTENSIONS:
        return PreparedTextFile(str(source), display_name, False)

    try:
        size = source.stat().st_size
        if size > _MAX_TEXT_BYTES:
            logger.warning("Outbound text hook skipped oversized file: %s bytes", size)
            return PreparedTextFile(str(source), display_name, False)
        raw = source.read_bytes()
        decoded = _decode_text(raw)
        if decoded is None:
            logger.warning("Outbound text hook skipped non-text payload with text extension")
            return PreparedTextFile(str(source), display_name, False)
        text, source_encoding = decoded
        want_bom = extension in _BOM_EXTENSIONS
        output_encoding = "utf-8-sig" if want_bom else "utf-8"
        payload = text.encode(output_encoding)

        digest = hashlib.sha256(
            str(source.resolve()).encode("utf-8", "surrogatepass") + b"\0" + raw
        ).hexdigest()[:12]
        target_dir = get_hermes_home() / "cache" / "outbound-text" / digest
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / display_name
        target.write_bytes(payload)
        # Verify the exact output contract before handing it to an adapter.
        verify_codec = "utf-8-sig" if want_bom else "utf-8"
        if target.read_bytes().decode(verify_codec) != text:
            raise UnicodeError("outbound text verification mismatch")
        return PreparedTextFile(
            str(target), display_name, str(target) != str(source),
            source_encoding=source_encoding,
            output_encoding=output_encoding,
        )
    except Exception as exc:
        logger.warning("Outbound text hook failed; using original file: %s", exc)
        return PreparedTextFile(str(source), display_name, False)
