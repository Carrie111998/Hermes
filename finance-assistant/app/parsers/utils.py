from __future__ import annotations

import re
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.parsers.base import ParserFormatError, StatementDocument

DATE_RE = re.compile(r"(?P<day>\d\s*\d)\s*[/.]\s*(?P<month>\d\s*\d)\s*[/.]\s*(?P<year>\d\s*\d\s*\d\s*\d)")
INSTALLMENT_RE = re.compile(r"(?P<first>\d{1,2})\s*/\s*(?P<second>\d{1,2})\s*\.?\s*taks(?:it(?:i|isi)?|id(?:i|isi))", re.IGNORECASE)


def parse_tr_amount(value: str) -> Decimal:
    """Parse common Turkish and bank-export amount formats."""
    raw = value.strip().replace(" ", "")
    negative = raw.endswith("-") or raw.endswith("(-)") or raw.startswith("-")
    raw = raw.removesuffix("(-)").removesuffix("-").removeprefix("+").removeprefix("-")
    if "," in raw and "." in raw:
        # 1.234,56 and OCR's 1,234.56 are both seen in statements.
        if raw.rfind(",") > raw.rfind("."):
            normalized = raw.replace(".", "").replace(",", ".")
        else:
            normalized = raw.replace(",", "")
    elif "," in raw:
        normalized = raw.replace(".", "").replace(",", ".")
    else:
        normalized = raw
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ParserFormatError(f"Invalid statement amount: {value}") from exc
    return -amount if negative else amount


def parse_statement_date(match: re.Match[str]) -> date:
    return date(*(int(re.sub(r"\s+", "", match[group])) for group in ("year", "month", "day")))


def normalize_text(value: str) -> str:
    return (value.casefold().replace("\u0307", "").replace("ı", "i").replace("İ", "i").replace("ç", "c").replace("ş", "s").replace("ğ", "g").replace("ö", "o").replace("ü", "u"))


def parse_installment(value: str, *, first_is_total: bool = False) -> tuple[int, int] | None:
    match = INSTALLMENT_RE.search(value)
    if not match:
        return None
    first, second = int(match["first"]), int(match["second"])
    return (second, first) if first_is_total else (first, second)


def _needs_ocr(text: str) -> bool:
    compact = text.replace(" ", "")
    return not compact or "(cid:" in compact or ("Hesap" not in text and "Hesap" not in text.casefold())


def _ocr_pdf(pdf_path: Path) -> str:
    try:
        import pypdfium2 as pdfium
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise ParserFormatError("PDF text layer is not usable and local OCR dependencies are unavailable") from exc

    ocr = RapidOCR()
    pdf = pdfium.PdfDocument(str(pdf_path))
    pages: list[str] = []
    for page in pdf:
        image = page.render(scale=3.0).to_pil()
        with tempfile.NamedTemporaryFile(suffix=".png") as rendered:
            image.save(rendered.name)
            rendered.flush()
            result, _ = ocr(rendered.name)
        rows: list[tuple[float, float, str]] = []
        for box, text, _score in result or []:
            x = sum(point[0] for point in box) / 4
            y = sum(point[1] for point in box) / 4
            rows.append((y, x, text))
        rows.sort()
        lines: list[list[object]] = []
        for y, x, text in rows:
            if not lines or abs(y - lines[-1][0]) > 12:
                lines.append([y, [(x, text)]])
            else:
                lines[-1][1].append((x, text))
        pages.append("\n".join(" ".join(text for _x, text in sorted(items, key=lambda item: item[0])) for _y, items in lines))
    return "\n".join(pages)


def load_pdf_document(path: str | Path) -> StatementDocument:
    import pypdf

    pdf_path = Path(path)
    reader = pypdf.PdfReader(str(pdf_path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if _needs_ocr(text):
        text = _ocr_pdf(pdf_path)
    if not text.strip():
        raise ParserFormatError("PDF has no usable text layer or OCR output")
    return StatementDocument(pdf_path, text)
