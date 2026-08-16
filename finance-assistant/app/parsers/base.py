from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from decimal import Decimal

from app.models import Transaction


@dataclass(frozen=True, slots=True)
class StatementDocument:
    path: Path | str
    text: str
    bank_hint: str | None = None


class ParserFormatError(ValueError):
    """Raised when a document no longer matches the known bank format."""


@dataclass(frozen=True, slots=True)
class StatementMetadata:
    period_start: date | None = None
    period_end: date | None = None
    due_date: date | None = None
    cutoff_date: date | None = None
    card_identifier: str = "****"
    statement_date: date | None = None
    currency: str = "TRY"
    statement_total: Decimal | None = None
    minimum_payment: Decimal | None = None


class StatementParser:
    bank_id: str = "base"

    def can_parse(self, document: StatementDocument) -> bool:
        return False

    def parse_metadata(self, document: StatementDocument) -> StatementMetadata:
        raise NotImplementedError

    def parse_transactions(self, document: StatementDocument, metadata: StatementMetadata) -> list[Transaction]:
        raise NotImplementedError


class ParserRegistry:
    def __init__(self, parsers: list[StatementParser] | None = None):
        self.parsers = parsers or []

    def register(self, parser: StatementParser) -> None:
        self.parsers.append(parser)

    def find(self, document: StatementDocument) -> StatementParser | None:
        for parser in self.parsers:
            if parser.can_parse(document):
                return parser
        return None
