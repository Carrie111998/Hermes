from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from uuid import uuid4


class TransactionType(StrEnum):
    PURCHASE = "PURCHASE"
    REFUND = "REFUND"
    PAYMENT = "PAYMENT"
    CASH_ADVANCE = "CASH_ADVANCE"
    FEE = "FEE"
    INTEREST = "INTEREST"
    TAX = "TAX"
    INSTALLMENT = "INSTALLMENT"
    OTHER = "OTHER"


class IngestionSource(StrEnum):
    MANUAL = "MANUAL"
    DIRECTORY = "DIRECTORY"
    GMAIL = "GMAIL"


class IngestionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    SUCCESS_WITH_WARNINGS = "SUCCESS_WITH_WARNINGS"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    UNSUPPORTED = "UNSUPPORTED"
    FORMAT_ERROR = "FORMAT_ERROR"
    FAILED = "FAILED"


@dataclass(slots=True)
class Statement:
    bank: str
    message_id: str
    attachment_sha256: str
    file_path: str
    statement_period_start: date | None = None
    statement_period_end: date | None = None
    due_date: date | None = None
    cutoff_date: date | None = None
    statement_date: date | None = None
    card_identifier: str = "****"
    currency: str = "TRY"
    statement_total: Decimal | None = None
    minimum_payment: Decimal | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if len(self.attachment_sha256) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in self.attachment_sha256):
            raise ValueError("attachment_sha256 must be a SHA-256 hex digest")


@dataclass(slots=True)
class Transaction:
    bank: str
    card_identifier: str
    statement_id: str
    statement_period_start: date | None
    statement_period_end: date | None
    transaction_date: date
    merchant_raw: str
    amount: Decimal
    currency: str = "TRY"
    posting_date: date | None = None
    merchant_normalized: str | None = None
    description_raw: str | None = None
    installment_current: int | None = None
    installment_total: int | None = None
    transaction_type: TransactionType = TransactionType.PURCHASE
    category: str = "Diğer"
    subcategory: str | None = None
    category_source: str = "rule"
    confidence: float | None = None
    statement_file: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        digits = "".join(ch for ch in self.card_identifier if ch.isdigit())
        if len(digits) > 4 and "*" not in self.card_identifier:
            raise ValueError("card_identifier must be masked; store at most last four digits")
        if not self.merchant_normalized:
            self.merchant_normalized = self.merchant_raw.strip().upper()
        if not 0 <= (self.confidence if self.confidence is not None else 1) <= 1:
            raise ValueError("confidence must be between 0 and 1")

    @property
    def fingerprint(self) -> str:
        raw = "|".join(
            [
                self.bank,
                self.card_identifier,
                self.statement_period_start.isoformat() if self.statement_period_start else "",
                self.statement_period_end.isoformat() if self.statement_period_end else "",
                self.transaction_date.isoformat(),
                self.merchant_raw,
                f"{self.amount:.2f}",
                self.transaction_type.value,
            ]
        )
        return sha256(raw.encode("utf-8")).hexdigest()
