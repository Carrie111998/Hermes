from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from decimal import Decimal

from app.models import Transaction, TransactionType
from app.parsers.base import ParserFormatError, StatementDocument, StatementMetadata, StatementParser
from app.parsers.utils import DATE_RE, normalize_text, parse_statement_date, parse_tr_amount

_AMOUNT_RE = re.compile(
    r"(?<![\w])(?P<value>[+-]?(?:\d[\d\s.]*,\d{2}|\d[\d\s,]*\.\d{2})(?:\s*\(-\))?)(?!\w)"
)
_HEADER_TEXT = "islem tarihi aciklama taksit tutar"
_DATE_ROW_RE = re.compile(r"^\s*\d{2}\s*[/.]\s*\d{2}\s*[/.]\s*\d{4}")
_CARD_RE = re.compile(r"kart\s*numaras[il]\s*:?\s*([^\r\n]+)", re.IGNORECASE)
_VIRTUAL_CARD_RE = re.compile(r"(\d{4})\s*\d{2}\s*\**\s*\**\s*(\d{4})\s*numarali\s*sanal", re.IGNORECASE)


def _first_date(value: str) -> date | None:
    match = DATE_RE.search(value)
    return parse_statement_date(match) if match else None


def _amount_after_label(text: str, *labels: str) -> Decimal | None:
    normalized_lines = [(normalize_text(line), line) for line in text.splitlines()]
    for label in labels:
        marker = re.sub(r"\s+", "", normalize_text(label))
        for normalized_line, original_line in normalized_lines:
            if marker not in re.sub(r"\s+", "", normalized_line):
                continue
            match = _AMOUNT_RE.search(original_line)
            if match:
                return parse_tr_amount(match.group("value"))
    return None


def _masked_card(value: str) -> str:
    digits = re.findall(r"\d", value)
    return "****" + "".join(digits[-4:]) if len(digits) >= 4 else "****"


def _period_start(cutoff: date) -> date:
    year, month = cutoff.year, cutoff.month - 1
    if month == 0:
        year, month = year - 1, 12
    day = min(cutoff.day, calendar.monthrange(year, month)[1])
    return date(year, month, day) + timedelta(days=1)


class EnparaParser(StatementParser):
    bank_id = "enpara"

    def can_parse(self, document: StatementDocument) -> bool:
        text = normalize_text(document.text)
        return self._has_identity(text) and _HEADER_TEXT in text

    @staticmethod
    def _has_identity(text: str) -> bool:
        return all(marker in text for marker in ("en para", "kredi karti ekstresi", "ekstre borcu"))

    def parse_metadata(self, document: StatementDocument) -> StatementMetadata:
        if not self._has_identity(normalize_text(document.text)):
            raise ParserFormatError("Document is not an Enpara statement")
        text = document.text
        normalized = normalize_text(text)
        header_position = normalized.find(_HEADER_TEXT)
        if header_position < 0:
            raise ParserFormatError("Expected Enpara transaction table header not found")

        cutoff = _first_date(_label_value(text, "Ekstre tarihi"))
        due = _first_date(_label_value(text, "Son odeme tarihi"))
        if cutoff is None:
            raise ParserFormatError("Required Enpara statement date is missing")
        total = _amount_after_label(text, "Ekstre borcu")
        minimum = _amount_after_label(text, "Minimum odeme tutari")
        card_match = _CARD_RE.search(normalized)
        card = _masked_card(card_match.group(1)) if card_match else "****"
        if total is None:
            raise ParserFormatError("Required Enpara statement total is missing")
        if minimum is None:
            raise ParserFormatError("Required Enpara minimum payment is missing")
        return StatementMetadata(
            period_start=_period_start(cutoff),
            period_end=cutoff,
            cutoff_date=cutoff,
            statement_date=cutoff,
            due_date=due,
            card_identifier=card,
            currency="TRY",
            statement_total=total,
            minimum_payment=minimum,
        )

    def parse_transactions(self, document: StatementDocument, metadata: StatementMetadata) -> list[Transaction]:
        normalized = normalize_text(document.text)
        header_position = normalized.find(_HEADER_TEXT)
        if header_position < 0:
            raise ParserFormatError("Expected Enpara transaction table header not found")
        source_lines = document.text[header_position:].splitlines()
        rows: list[tuple[str, str]] = []
        current: list[str] = []
        current_card = metadata.card_identifier
        for raw_line in source_lines[1:]:
            line = " ".join(raw_line.split())
            lowered = normalize_text(line)
            if not line:
                continue
            virtual = _VIRTUAL_CARD_RE.search(lowered)
            if virtual:
                if current:
                    rows.append((" ".join(current), current_card))
                    current = []
                current_card = "****" + virtual.group(2)
                continue
            if lowered.startswith(("bir sonraki ekstre", "guncel akdi faiz", "alisveris nakit avans")):
                break
            if _DATE_ROW_RE.match(line):
                if current:
                    rows.append((" ".join(current), current_card))
                current = [line]
            elif current and not self._is_footer(lowered):
                current.append(line)
        if current:
            rows.append((" ".join(current), current_card))
        if not rows:
            raise ParserFormatError("Enpara transaction table found but no transaction rows were parsed")

        transactions: list[Transaction] = []
        for row, card_identifier in rows:
            transactions.append(self._parse_row(row, card_identifier, metadata, document))
        return transactions

    @staticmethod
    def _is_footer(line: str) -> bool:
        return line.startswith(("enpara", "sayfa", "kart sahibinin", "en para bank", "bayuk mukellefler"))

    def _parse_row(
        self, row: str, card_identifier: str, metadata: StatementMetadata, document: StatementDocument
    ) -> Transaction:
        date_match = DATE_RE.match(row)
        if not date_match:
            raise ParserFormatError("Enpara transaction row has no date")
        transaction_date = parse_statement_date(date_match)
        remainder = row[date_match.end() :].strip()
        amounts = list(_AMOUNT_RE.finditer(remainder))
        if not amounts:
            raise ParserFormatError("Enpara transaction row has no amount")
        amount_match = amounts[-1]
        amount = parse_tr_amount(amount_match.group("value"))
        description = remainder[: amount_match.start()].strip()
        lowered = normalize_text(description)
        if lowered.startswith("odeme") or "odeme -" in lowered:
            amount = -abs(amount)
            transaction_type = TransactionType.PAYMENT
        elif amount < 0:
            transaction_type = TransactionType.REFUND
        else:
            transaction_type = TransactionType.PURCHASE
        return Transaction(
            bank=self.bank_id,
            card_identifier=card_identifier,
            statement_id="pending",
            statement_period_start=metadata.period_start,
            statement_period_end=metadata.period_end,
            transaction_date=transaction_date,
            merchant_raw=description,
            description_raw=description,
            amount=amount,
            currency=metadata.currency,
            transaction_type=transaction_type,
            statement_file=str(document.path),
        )


def _label_value(text: str, label: str) -> str:
    normalized = normalize_text(text)
    marker = normalize_text(label)
    position = normalized.find(marker)
    if position < 0:
        return ""
    end = text.find("\n", position)
    return text[position + len(label) : end if end >= 0 else len(text)]
