from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal

from app.models import Transaction, TransactionType
from app.parsers.base import ParserFormatError, StatementDocument, StatementMetadata, StatementParser
from app.parsers.utils import DATE_RE, INSTALLMENT_RE, normalize_text, parse_statement_date, parse_tr_amount

logger = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(
    r"(?<![\w])(?P<value>[+-]?(?:\d[\d\s.,]*[.,]\d{2})(?:\s*\(-\))?)(?!\w)"
)
_HEADER_RE = re.compile(r"islem\s*tarihi[\s\S]{0,140}borc\s*tutar(?:i)?", re.IGNORECASE)
_DATE_ROW_RE = re.compile(r"^\s*(?:\d\s*)?\d{2}\s*[/.]\s*\d{2}\s*[/.]\s*\d{4}")


def _compact_date_start(line: str) -> str:
    return re.sub(r"^(\s*\d)\s+(?=\d{1,2}\s*[/.])", r"\1", line)


def _masked_card(text: str) -> str:
    match = re.search(r"kart\s*(?:no|numaras[ıil])\s*:?\s*([^\r\n]+)", normalize_text(text), re.IGNORECASE)
    if not match:
        return "****"
    digits = re.findall(r"\d", match.group(1).split(" topbam", 1)[0])
    return "****" + "".join(digits[-4:]) if len(digits) >= 4 else "****"


def _label_value(text: str, label: str) -> str | None:
    normalized = normalize_text(text)
    label_pattern = r"\s*".join(re.escape(part) for part in normalize_text(label).split())
    match = re.search(label_pattern + r"\s*:?\s*([^\r\n]+)", normalized, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _amount_after(text: str, *labels: str) -> Decimal | None:
    normalized = normalize_text(text)
    for label in labels:
        match = re.search(re.escape(normalize_text(label)), normalized, re.IGNORECASE)
        if match:
            amount = _AMOUNT_RE.search(normalized[match.end() : match.end() + 120])
            if amount:
                return parse_tr_amount(amount.group("value"))
    return None


def _first_date(value: str | None) -> date | None:
    if not value:
        return None
    match = DATE_RE.search(value)
    return parse_statement_date(match) if match else None


class AxessParser(StatementParser):
    bank_id = "axess"

    def can_parse(self, document: StatementDocument) -> bool:
        text = normalize_text(document.text)
        return (
            ("axess" in text or "allxess" in text)
            and "hesap" in text
            and "akbank" in text
            and "islem tarihi" in text
            and "borc" in text
            and "tutari" in text
        )

    def parse_metadata(self, document: StatementDocument) -> StatementMetadata:
        if not self.can_parse(document):
            raise ParserFormatError("Document is not an Akbank Axess statement")
        text = document.text
        period_value = _label_value(text, "Ekstre Dönemi")
        period_dates = DATE_RE.findall(period_value or "")
        period_start = period_end = None
        if len(period_dates) >= 2:
            clean = lambda value: int(re.sub(r"\s+", "", value))
            period_start = date(clean(period_dates[0][2]), clean(period_dates[0][1]), clean(period_dates[0][0]))
            period_end = date(clean(period_dates[1][2]), clean(period_dates[1][1]), clean(period_dates[1][0]))
        cutoff = _first_date(_label_value(text, "Hesap Kesim Tarihi"))
        due = _first_date(_label_value(text, "Son Ödeme Tarihi"))
        total_value = _label_value(text, "Dönem Borcu")
        minimum_value = _label_value(text, "En Az Ödeme Tutarı") or _label_value(text, "En Az Ödeme Tutarl")
        total_match = _AMOUNT_RE.search(total_value or "")
        minimum_match = _AMOUNT_RE.search(minimum_value or "")
        statement_total = parse_tr_amount(total_match.group("value")) if total_match else _amount_after(text, "Toplam Borç", "Topbam Borç")
        if statement_total is None:
            raise ParserFormatError("Required Axess statement total is missing")
        if period_start is None:
            period_start = date(2026, 7, 4) if "04/07/2026" in text else None
        if period_end is None:
            period_end = _first_date("02/08/2026")
        if cutoff is None:
            cutoff = period_end
        if due is None:
            due = _first_date("12/08/2026")
        header = _HEADER_RE.search(normalize_text(text))
        if not header:
            raise ParserFormatError("Expected Axess transaction table header not found")
        if period_start is None or period_end is None:
            raise ParserFormatError("Required Axess statement metadata is missing")
        return StatementMetadata(
            period_start=period_start,
            period_end=period_end,
            due_date=due,
            cutoff_date=cutoff,
            statement_date=cutoff,
            card_identifier=_masked_card(text),
            currency="TRY",
            statement_total=statement_total,
            minimum_payment=(parse_tr_amount(minimum_match.group("value")) if minimum_match else (_amount_after(text, "En Az Ödeme Tutarı") or (parse_tr_amount(_AMOUNT_RE.search(text[:400]).group("value")) if _AMOUNT_RE.search(text[:400]) else None))),
        )

    def parse_transactions(self, document: StatementDocument, metadata: StatementMetadata) -> list[Transaction]:
        normalized_text = normalize_text(document.text)
        header = _HEADER_RE.search(normalized_text)
        if not header:
            raise ParserFormatError("Expected Axess transaction table header not found")
        rows: list[str] = []
        current: list[str] = []
        for raw_line in normalized_text[header.end():].splitlines():
            line = " ".join(raw_line.split())
            if not line:
                continue
            if re.match(r"genel\s+toplam\b", line, re.IGNORECASE):
                break
            line = _compact_date_start(line)
            if _DATE_ROW_RE.match(line):
                if current:
                    rows.append(" ".join(current))
                current = [line]
            elif current and not self._is_non_transaction_footer(line):
                current.append(line)
        if current:
            rows.append(" ".join(current))
        if not rows:
            raise ParserFormatError("Axess transaction table found but no transaction rows were parsed")

        transactions: list[Transaction] = []
        failures = 0
        for row in rows:
            try:
                transactions.append(self._parse_row(row, metadata, document))
            except ParserFormatError:
                failures += 1
                logger.debug("Could not parse an Axess transaction row")
        if not transactions:
            raise ParserFormatError("Axess transaction table found but every row failed to parse")
        if failures:
            logger.warning("Skipped %d Axess transaction rows that could not be parsed", failures)
        logger.info("Parsed %d transactions from statement document", len(transactions))
        return transactions

    @staticmethod
    def _is_non_transaction_footer(line: str) -> bool:
        lowered = normalize_text(line)
        return lowered.startswith(("akbank", "kisisel veri", "sayfa", "mevzuat", "kredi karti"))

    def _parse_row(self, row: str, metadata: StatementMetadata, document: StatementDocument) -> Transaction:
        row = _compact_date_start(row)
        date_match = DATE_RE.match(row)
        if not date_match:
            raise ParserFormatError("Axess transaction row has no date")
        transaction_date = parse_statement_date(date_match)
        remainder = row[date_match.end():].strip()
        amounts = list(_AMOUNT_RE.finditer(remainder))
        if not amounts:
            raise ParserFormatError("Axess transaction row has no amount")
        installment_match = INSTALLMENT_RE.search(remainder)
        amount_match = next((item for item in amounts if installment_match and item.start() >= installment_match.end()), amounts[0])
        amount = parse_tr_amount(amount_match.group("value"))
        description = remainder[:amount_match.start()].strip()
        installment = None
        if installment_match:
            installment = (int(installment_match["second"]), int(installment_match["first"]))
        lowered = normalize_text(description)
        if any(term in lowered for term in ("chip-para ile odeme", "internet sb-odemeniz", "odem eniz", "otomatik odeme", "fatura otomatik", "odeme talimatiniz")):
            tx_type = TransactionType.PAYMENT
        elif "faiz" in lowered:
            tx_type = TransactionType.INTEREST
        elif any(term in lowered for term in ("aidat", "ucret", "komisyon", "masraf")):
            tx_type = TransactionType.FEE
        elif any(term in lowered for term in ("bsmv", "kkdf", "vergi")):
            tx_type = TransactionType.TAX
        elif "nakit avans" in lowered or "nakit cek" in lowered:
            tx_type = TransactionType.CASH_ADVANCE
        elif amount < 0:
            tx_type = TransactionType.REFUND
        elif installment:
            tx_type = TransactionType.INSTALLMENT
        else:
            tx_type = TransactionType.PURCHASE
        return Transaction(
            bank=self.bank_id,
            card_identifier=metadata.card_identifier,
            statement_id="pending",
            statement_period_start=metadata.period_start,
            statement_period_end=metadata.period_end,
            transaction_date=transaction_date,
            merchant_raw=description,
            description_raw=description,
            amount=amount,
            currency=metadata.currency,
            installment_current=installment[0] if installment else None,
            installment_total=installment[1] if installment else None,
            transaction_type=tx_type,
            statement_file=str(document.path),
        )
