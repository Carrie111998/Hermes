from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.categorization.rules import categorize, normalize_merchant
from app.config import AppConfig, load_config
from app.database.schema import Database
from app.models import TransactionType

SUPPORTED_BANKS = ("isbank_maximum", "axess", "enpara")
_SEPARATE_TYPES = {
    TransactionType.FEE,
    TransactionType.INTEREST,
    TransactionType.TAX,
    TransactionType.CASH_ADVANCE,
    TransactionType.PAYMENT,
    TransactionType.OTHER,
}


def _month_start(value: str | date) -> date:
    if isinstance(value, date):
        return value.replace(day=1)
    return date.fromisoformat(f"{value}-01")


def _next_month(value: date) -> date:
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def _money(value: Decimal | int | float | None) -> Decimal:
    return Decimal("0") if value is None else Decimal(str(value)).quantize(Decimal("0.01"))


def _positive_cost(value: Decimal) -> Decimal:
    return abs(value)


@dataclass(frozen=True, slots=True)
class CategoryAggregate:
    amount: Decimal = Decimal("0.00")
    transaction_count: int = 0


@dataclass(frozen=True, slots=True)
class TopTransaction:
    transaction_date: date
    amount: Decimal
    bank: str
    category: str
    transaction_type: str


@dataclass(frozen=True, slots=True)
class StatementCompleteness:
    bank: str
    status: str


@dataclass(frozen=True, slots=True)
class MonthlyAnalysis:
    period: str
    total_spending: Decimal
    purchase_total: Decimal
    refund_total: Decimal
    fee_total: Decimal
    interest_total: Decimal
    tax_total: Decimal
    cash_advance_total: Decimal
    by_bank: dict[str, Decimal]
    by_card: dict[str, Decimal]
    by_category: dict[str, Decimal]
    by_subcategory: dict[str, Decimal]
    transaction_count: int
    uncategorized_count: int
    top_transactions: list[TopTransaction] = field(default_factory=list)
    statement_completeness: list[StatementCompleteness] = field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        """Return aggregate data only; no merchant/card descriptions."""
        return {
            "period": self.period,
            "total_spending": str(self.total_spending),
            "purchase_total": str(self.purchase_total),
            "refund_total": str(self.refund_total),
            "fee_total": str(self.fee_total),
            "interest_total": str(self.interest_total),
            "tax_total": str(self.tax_total),
            "cash_advance_total": str(self.cash_advance_total),
            "by_bank": {key: str(value) for key, value in self.by_bank.items()},
            "by_category": {key: str(value) for key, value in self.by_category.items()},
            "by_subcategory": {key: str(value) for key, value in self.by_subcategory.items()},
            "transaction_count": self.transaction_count,
            "uncategorized_count": self.uncategorized_count,
            "statement_completeness": [asdict(item) for item in self.statement_completeness],
        }


@dataclass(frozen=True, slots=True)
class MonthComparison:
    current_month: str
    previous_month: str
    current_total: Decimal
    previous_total: Decimal
    difference: Decimal
    percent_change: Decimal | None
    by_category: dict[str, dict[str, Decimal | None]]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "current_month": self.current_month,
            "previous_month": self.previous_month,
            "current_total": str(self.current_total),
            "previous_total": str(self.previous_total),
            "difference": str(self.difference),
            "percent_change": None if self.percent_change is None else str(self.percent_change),
            "by_category": {
                category: {key: None if value is None else str(value) for key, value in values.items()}
                for category, values in self.by_category.items()
            },
        }


class AnalysisService:
    """Read-only calendar-month analysis over persisted transactions."""

    def __init__(self, database: Database, config: AppConfig | None = None):
        self.database = database
        self.config = config or load_config(Path(__file__).resolve().parents[2] / "config")

    @classmethod
    def from_path(cls, path: str | Path, config_dir: str | Path | None = None) -> "AnalysisService":
        db = Database(path, read_only=True)
        config = load_config(config_dir or Path(__file__).resolve().parents[2] / "config")
        return cls(db, config)

    def _rows(self, start: date, end: date) -> list[tuple[Any, ...]]:
        return self.database.connection.execute(
            """SELECT bank, card_identifier, transaction_date, merchant_raw, merchant_normalized,
                      description_raw, amount, transaction_type, category, subcategory
               FROM transactions
               WHERE transaction_date >= ? AND transaction_date < ?
               ORDER BY transaction_date, id""", [start, end]
        ).fetchall()

    def _categorized(self, row: tuple[Any, ...]) -> tuple[dict[str, Any], Any]:
        bank, card, tx_date, raw, persisted_normalized, description, amount, tx_type, old_category, old_subcategory = row
        normalized = normalize_merchant(raw)
        manual = self.database.connection.execute(
            "SELECT category, subcategory FROM merchant_rules WHERE merchant_normalized = ? LIMIT 1",
            [normalized],
        ).fetchone()
        if manual:
            result = type("Result", (), {"category": manual[0], "subcategory": manual[1], "source": "manual", "confidence": 1.0})()
        else:
            result = categorize(normalized, description or "")
            if result.category == "Diğer" and result.confidence < 1:
                result = type("Result", (), {"category": result.category, "subcategory": result.subcategory, "source": "unknown", "confidence": result.confidence})()
        return {
            "bank": bank, "card": card, "transaction_date": tx_date, "amount": _money(amount),
            "type": TransactionType(str(tx_type)), "normalized": normalized,
        }, result

    @staticmethod
    def _spending_amount(amount: Decimal, tx_type: TransactionType) -> Decimal:
        if tx_type in {TransactionType.PURCHASE, TransactionType.INSTALLMENT}:
            return _positive_cost(amount)
        if tx_type is TransactionType.REFUND:
            return -_positive_cost(amount)
        return Decimal("0.00")

    def analyze(self, month: str | date, *, top_n: int = 10) -> MonthlyAnalysis:
        if top_n < 0:
            raise ValueError("top_n must be non-negative")
        start = _month_start(month)
        end = _next_month(start)
        rows = [self._categorized(row) for row in self._rows(start, end)]
        by_bank: dict[str, Decimal] = {}
        by_card: dict[str, Decimal] = {}
        by_category: dict[str, Decimal] = {}
        by_subcategory: dict[str, Decimal] = {}
        purchase = refund = fee = interest = tax = cash_advance = Decimal("0.00")
        uncategorized = 0
        ranked: list[TopTransaction] = []
        for item, result in rows:
            tx_type = item["type"]
            amount = item["amount"]
            if tx_type is TransactionType.PURCHASE:
                purchase += _positive_cost(amount)
            elif tx_type is TransactionType.INSTALLMENT:
                purchase += _positive_cost(amount)
            elif tx_type is TransactionType.REFUND:
                refund -= _positive_cost(amount)
            elif tx_type is TransactionType.FEE:
                fee += _positive_cost(amount)
            elif tx_type is TransactionType.INTEREST:
                interest += _positive_cost(amount)
            elif tx_type is TransactionType.TAX:
                tax += _positive_cost(amount)
            elif tx_type is TransactionType.CASH_ADVANCE:
                cash_advance += _positive_cost(amount)
            spending = self._spending_amount(amount, tx_type)
            if spending:
                by_bank[item["bank"]] = by_bank.get(item["bank"], Decimal("0.00")) + spending
                by_card[item["card"]] = by_card.get(item["card"], Decimal("0.00")) + spending
                by_category[result.category] = by_category.get(result.category, Decimal("0.00")) + spending
                if result.subcategory:
                    by_subcategory[result.subcategory] = by_subcategory.get(result.subcategory, Decimal("0.00")) + spending
                ranked.append(TopTransaction(item["transaction_date"], spending, item["bank"], result.category, tx_type.value))
            if spending and result.source == "unknown":
                uncategorized += 1
        ranked.sort(key=lambda value: value.amount, reverse=True)
        return MonthlyAnalysis(
            period=start.strftime("%Y-%m"), total_spending=purchase + refund,
            purchase_total=purchase, refund_total=refund, fee_total=fee,
            interest_total=interest, tax_total=tax, cash_advance_total=cash_advance,
            by_bank=dict(sorted(by_bank.items())), by_card=dict(sorted(by_card.items())),
            by_category=dict(sorted(by_category.items())), by_subcategory=dict(sorted(by_subcategory.items())),
            transaction_count=len(rows), uncategorized_count=uncategorized,
            top_transactions=ranked[:top_n], statement_completeness=self.statement_completeness(start),
        )

    def statement_completeness(self, month: str | date) -> list[StatementCompleteness]:
        start = _month_start(month)
        end = _next_month(start)
        rows = self.database.connection.execute(
            """SELECT bank, id, attachment_sha256 FROM statements
               WHERE (statement_date >= ? AND statement_date < ?)
                  OR (statement_period_end >= ? AND statement_period_end < ?)
                  OR (statement_period_start < ? AND statement_period_end >= ?)""",
            [start, end, start, end, end, start],
        ).fetchall()
        by_bank: dict[str, list[str]] = {}
        for bank, statement_id, attachment_sha256 in rows:
            transaction_count = int(self.database.connection.execute(
                "SELECT count(*) FROM transactions WHERE statement_id = ?", [statement_id]
            ).fetchone()[0])
            logs = self.database.connection.execute(
                """SELECT status, transaction_count FROM processing_log
                   WHERE sha256 = ? AND stage = 'ingestion'""", [attachment_sha256]
            ).fetchall()
            expected = max((int(log[1] or 0) for log in logs), default=0)
            successful = any(str(log[0]) in {"SUCCESS", "SUCCESS_WITH_WARNINGS"} for log in logs)
            if transaction_count == 0 and expected == 0:
                status = "WAITING_FOR_TRANSACTIONS"
            elif successful and expected > 0:
                status = "PRESENT" if transaction_count >= expected else "PARTIAL"
            elif transaction_count > 0:
                # Existing transactions are authoritative, but without a
                # successful ingestion event their import cannot be audited.
                status = "LEGACY_UNVERIFIED"
            else:
                status = "PARTIAL"
            by_bank.setdefault(str(bank), []).append(status)

        result = []
        for bank in SUPPORTED_BANKS:
            statuses = by_bank.get(bank)
            if not statuses:
                status = "WAITING_FOR_STATEMENT"
            elif "PARTIAL" in statuses:
                status = "PARTIAL"
            elif "LEGACY_UNVERIFIED" in statuses:
                status = "LEGACY_UNVERIFIED"
            elif "WAITING_FOR_TRANSACTIONS" in statuses:
                status = "WAITING_FOR_TRANSACTIONS"
            else:
                status = "PRESENT"
            result.append(StatementCompleteness(bank, status))
        return result

    def compare_months(self, current_month: str | date, previous_month: str | date) -> MonthComparison:
        current = self.analyze(current_month)
        previous = self.analyze(previous_month)
        difference = current.total_spending - previous.total_spending
        percent = None if previous.total_spending == 0 else (difference / previous.total_spending * 100).quantize(Decimal("0.1"))
        categories = set(current.by_category) | set(previous.by_category)
        by_category = {}
        for category in sorted(categories):
            old = previous.by_category.get(category, Decimal("0.00"))
            new = current.by_category.get(category, Decimal("0.00"))
            change = None if old == 0 else ((new - old) / old * 100).quantize(Decimal("0.1"))
            by_category[category] = {"previous": old, "current": new, "difference": new - old, "percent_change": change}
        return MonthComparison(current.period, previous.period, current.total_spending, previous.total_spending, difference, percent, by_category)

    def trend(self, end_month: str | date, months: int) -> list[MonthlyAnalysis]:
        if months not in {3, 6, 12}:
            raise ValueError("months must be one of 3, 6, or 12")
        end = _month_start(end_month)
        values = []
        year, month = end.year, end.month
        for offset in range(months - 1, -1, -1):
            index = year * 12 + month - 1 - offset
            values.append(self.analyze(f"{index // 12:04d}-{index % 12 + 1:02d}"))
        return values

    def audit(self) -> dict[str, Any]:
        """Return non-sensitive counts across the database for a pre-dashboard audit."""
        rows = self._rows(date.min, date.max)
        type_counts: dict[str, int] = {}
        categorized = 0
        uncategorized = 0
        known_rule = 0
        normalized_count = 0
        for row in rows:
            tx_type = row[7]
            type_counts[str(tx_type)] = type_counts.get(str(tx_type), 0) + 1
            item, result = self._categorized(row)
            if item["normalized"] != row[4]:
                normalized_count += 1
            if result.category == "Diğer" and result.confidence < 1:
                uncategorized += 1
            else:
                categorized += 1
                if result.source in {"manual", "rule"}:
                    known_rule += 1
        manual_count = self.database.connection.execute("SELECT count(*) FROM merchant_rules").fetchone()[0]
        return {
            "transaction_type_counts": dict(sorted(type_counts.items())),
            "categorized_count": categorized,
            "uncategorized_count": uncategorized,
            "known_merchant_rule_count": known_rule,
            "manual_rule_count": int(manual_count),
            "normalized_merchant_count": normalized_count,
        }

    def get_uncategorized_transactions(self, month: str | date | None = None) -> list[dict[str, Any]]:
        if month is None:
            start, end = date.min, date.max
        else:
            start = _month_start(month)
            end = _next_month(start)
        result = []
        for row in self._rows(start, end):
            item, category = self._categorized(row)
            if self._spending_amount(item["amount"], item["type"]) and category.source == "unknown":
                result.append({"transaction_date": item["transaction_date"], "amount": item["amount"], "bank": item["bank"]})
        return result


class MerchantRuleService:
    """Writable persistence boundary for explicit merchant overrides."""

    def __init__(self, database: Database):
        self.database = database

    def set_merchant_rule(self, merchant: str, category: str, subcategory: str | None = None) -> None:
        normalized = normalize_merchant(merchant)
        self.database.connection.execute(
            """INSERT INTO merchant_rules (merchant_normalized, category, subcategory, source, created_at)
               VALUES (?, ?, ?, 'manual', ?)
               ON CONFLICT (merchant_normalized) DO UPDATE SET category = excluded.category,
                 subcategory = excluded.subcategory, source = 'manual', created_at = excluded.created_at""",
            [normalized, category, subcategory, datetime.now(timezone.utc)],
        )
