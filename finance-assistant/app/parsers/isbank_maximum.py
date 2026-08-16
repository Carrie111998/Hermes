from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.analysis.fees import detect_fee
from app.models import Transaction, TransactionType
from app.parsers.base import ParserFormatError, StatementDocument, StatementMetadata, StatementParser
from app.parsers.utils import DATE_RE, INSTALLMENT_RE, load_pdf_document, normalize_text, parse_statement_date, parse_tr_amount

logger = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(r"(?<!\w)-?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}(?!\w)")
_TABLE_HEADER_RE = re.compile(r"İŞLEM\s+TARİHİ[\s\S]{0,120}AÇIKLAMA[\s\S]{0,120}TUTAR", re.IGNORECASE)
_STOP_RE = re.compile(r"(?:\*+\s*ÖDEMELERİNİZ|AYLIK TAKSİTLİ|\*+\s*TOPLAM|TOPLAM\s+)", re.IGNORECASE)


class IsbankMaximumParser(StatementParser):
    bank_id = "isbank_maximum"

    def can_parse(self, document: StatementDocument) -> bool:
        text = document.text.casefold()
        anchors = ("isbank.com.tr", "maximum", "hesap özetiniz")
        return all(anchor in text for anchor in anchors)

    def parse_metadata(self, document: StatementDocument) -> StatementMetadata:
        if not self.can_parse(document):
            raise ParserFormatError("Document is not an İş Bankası Maximum statement")
        text = document.text
        cutoff = self._label_date(text, r"Hesap Kesim Tarihi")
        due = self._label_date(text, r"Son Ödeme Tarihi")
        statement_total = self._label_amount(text, r"Hesap Özeti Borcu")
        minimum = self._label_amount(text, r"Ödenmesi Gereken Asgari Tutar")
        card_match = re.search(r"Kart Numarası:\s*([^\r\n]+)", text, re.IGNORECASE)
        card = card_match.group(1).strip() if card_match else "****"
        card_digits = re.sub(r"\D", "", card)
        if len(card_digits) > 4 and "*" not in card:
            raise ParserFormatError("Unmasked card identifier found")
        card = "****" + card_digits[-4:] if len(card_digits) >= 4 else card
        header = _TABLE_HEADER_RE.search(text)
        if not header:
            raise ParserFormatError("Expected transaction table header not found")
        first_date = DATE_RE.search(text, header.end())
        period_start = parse_statement_date(first_date) if first_date else None
        return StatementMetadata(
            period_start=period_start,
            period_end=cutoff,
            due_date=due,
            cutoff_date=cutoff,
            statement_date=cutoff,
            card_identifier=card,
            currency="TRY",
            statement_total=statement_total,
            minimum_payment=minimum,
        )

    def parse_transactions(self, document: StatementDocument, metadata: StatementMetadata) -> list[Transaction]:
        header = _TABLE_HEADER_RE.search(document.text)
        if not header:
            raise ParserFormatError("Expected transaction table header not found")
        lines: list[str] = []
        current: list[str] = []
        for raw_line in document.text[header.end():].splitlines():
            line = " ".join(raw_line.split())
            if not line:
                continue
            if _STOP_RE.search(line):
                break
            if DATE_RE.match(line):
                if current:
                    lines.append(" ".join(current))
                current = [line]
            elif current:
                current.append(line)
        if current:
            lines.append(" ".join(current))
        if not lines:
            raise ParserFormatError("Transaction table found but no transaction rows were parsed")

        transactions: list[Transaction] = []
        failures = 0
        for line in lines:
            try:
                transaction = self._parse_row(line, metadata, document)
            except ParserFormatError:
                failures += 1
                logger.debug("Could not parse a transaction row")
                continue
            transactions.append(transaction)
        logger.info("Parsed %d transactions from statement document", len(transactions))
        if not transactions:
            raise ParserFormatError("Transaction table found but every transaction row failed to parse")
        if failures:
            logger.warning("Skipped %d transaction rows that could not be parsed", failures)
        return transactions

    def _parse_row(self, line: str, metadata: StatementMetadata, document: StatementDocument) -> Transaction:
        date_match = DATE_RE.match(line)
        if not date_match:
            raise ParserFormatError("Transaction row has no date")
        tx_date = parse_statement_date(date_match)
        remainder = line[date_match.end():].strip()
        amount_match = _AMOUNT_RE.search(remainder)
        if not amount_match:
            raise ParserFormatError("Transaction row has no amount")
        amount = parse_tr_amount(amount_match.group())
        description = remainder[:amount_match.start()].strip()
        installment_match = INSTALLMENT_RE.search(remainder)
        current = total = None
        if installment_match:
            current = int(installment_match["first"])
            total = int(installment_match["second"])
        lowered = normalize_text(description)
        fee = detect_fee(description)
        if fee:
            tx_type = fee.transaction_type
        elif any(normalize_text(term) in lowered for term in ("hesaptan aktarım", "ödeme", "ödemesi")):
            tx_type = TransactionType.PAYMENT
        elif normalize_text("nakit avans") in lowered or normalize_text("nakit çek") in lowered:
            tx_type = TransactionType.CASH_ADVANCE
        elif amount < 0:
            tx_type = TransactionType.REFUND
        elif installment_match:
            tx_type = TransactionType.INSTALLMENT
        else:
            tx_type = TransactionType.PURCHASE
        return Transaction(
            bank=self.bank_id,
            card_identifier=metadata.card_identifier,
            statement_id="pending",
            statement_period_start=metadata.period_start,
            statement_period_end=metadata.period_end,
            transaction_date=tx_date,
            merchant_raw=description,
            description_raw=description,
            amount=amount,
            currency=metadata.currency,
            installment_current=current,
            installment_total=total,
            transaction_type=tx_type,
            statement_file=str(document.path),
        )

    @staticmethod
    def _label_date(text: str, label: str) -> date | None:
        match = re.search(label + r"\s*:\s*(\d{2}[/.]\d{2}[/.]\d{4})", text, re.IGNORECASE)
        return parse_statement_date(DATE_RE.search(match.group(1))) if match else None

    @staticmethod
    def _label_amount(text: str, label: str) -> Decimal | None:
        match = re.search(label + r"\s*:\s*\*?\s*(\d[\d.]*,\d{2})\s*TL", text, re.IGNORECASE)
        return parse_tr_amount(match.group(1)) if match else None
