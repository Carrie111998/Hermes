from datetime import date
from decimal import Decimal

import duckdb
import pytest

from app.analysis.service import AnalysisService
from app.database.schema import initialize_database
from app.models import Statement, Transaction, TransactionType


def make_service(tmp_path):
    db = initialize_database(tmp_path / "finance.duckdb")
    statement = Statement(
        bank="enpara", message_id="m", attachment_sha256="a" * 64, file_path="x",
        statement_period_start=date(2026, 7, 5), statement_period_end=date(2026, 8, 4),
        statement_date=date(2026, 8, 4), card_identifier="****1234",
    )
    assert db.insert_statement(statement)
    db.log_processing(
        source="MANUAL", sha256=statement.attachment_sha256, bank=statement.bank,
        status="SUCCESS", transaction_count=8, inserted_count=8,
        file_path=statement.file_path,
    )
    def add(day, merchant, amount, kind, bank="enpara", card="****1234", desc=""):
        tx = Transaction(
            bank=bank, card_identifier=card, statement_id=statement.id,
            statement_period_start=statement.statement_period_start,
            statement_period_end=statement.statement_period_end, transaction_date=date.fromisoformat(day),
            merchant_raw=merchant, description_raw=desc, amount=Decimal(amount), transaction_type=kind,
        )
        assert db.insert_transaction(tx)
    add("2026-08-01", "MIGROS TICARET", "100", TransactionType.PURCHASE)
    add("2026-08-02", "UNKNOWN SHOP", "50", TransactionType.INSTALLMENT)
    add("2026-08-03", "MIGROS TICARET", "20", TransactionType.REFUND)
    add("2026-08-04", "CARD PAYMENT", "400", TransactionType.PAYMENT)
    add("2026-08-05", "BANK FEE", "5", TransactionType.FEE)
    add("2026-08-06", "INTEREST", "3", TransactionType.INTEREST)
    add("2026-08-07", "TAX", "2", TransactionType.TAX)
    add("2026-08-08", "FUEL SHELL", "30", TransactionType.PURCHASE, bank="axess", card="****9876")
    add("2026-07-31", "MIGROS", "999", TransactionType.PURCHASE)
    return AnalysisService(db)


def test_analysis_from_path_is_read_only_and_does_not_create_missing_database(tmp_path):
    missing = tmp_path / "missing" / "finance.duckdb"
    with pytest.raises(FileNotFoundError):
        AnalysisService.from_path(missing, tmp_path)

    database_path = tmp_path / "finance.duckdb"
    database = initialize_database(database_path)
    database.close()
    service = AnalysisService.from_path(database_path, tmp_path)
    with pytest.raises(duckdb.Error):
        service.database.connection.execute("CREATE TABLE should_not_exist(value INTEGER)")
    service.database.close()


def test_transaction_type_totals_and_aggregates(tmp_path):
    analysis = make_service(tmp_path).analyze("2026-08")
    assert analysis.purchase_total == Decimal("180.00")
    assert analysis.refund_total == Decimal("-20.00")
    assert analysis.total_spending == Decimal("160.00")
    assert analysis.fee_total == Decimal("5.00")
    assert analysis.interest_total == Decimal("3.00")
    assert analysis.tax_total == Decimal("2.00")
    assert analysis.by_bank == {"axess": Decimal("30.00"), "enpara": Decimal("130.00")}
    assert analysis.by_category["Market"] == Decimal("80.00")
    assert analysis.uncategorized_count == 1
    assert analysis.transaction_count == 8


def test_calendar_month_boundaries_and_comparison_zero(tmp_path):
    service = make_service(tmp_path)
    august = service.analyze("2026-08")
    assert august.transaction_count == 8
    comparison = service.compare_months("2026-08", "2026-09")
    assert comparison.previous_total == 0
    assert comparison.percent_change is None
    assert comparison.by_category["Market"]["percent_change"] is None


def test_manual_merchant_rule_precedes_deterministic_rule(tmp_path):
    service = make_service(tmp_path)
    service.set_merchant_rule("UNKNOWN SHOP", "Market", "Supermarket")
    result = service.analyze("2026-08")
    assert result.uncategorized_count == 0
    assert result.by_subcategory["Supermarket"] == Decimal("50.00")


def test_statement_completeness_and_safe_uncategorized_output(tmp_path):
    service = make_service(tmp_path)
    statuses = {item.bank: item.status for item in service.statement_completeness("2026-08")}
    assert statuses == {"isbank_maximum": "WAITING_FOR_STATEMENT", "axess": "WAITING_FOR_STATEMENT", "enpara": "PRESENT"}
    unknown = service.get_uncategorized_transactions("2026-08")
    assert len(unknown) == 1
    assert set(unknown[0]) == {"transaction_date", "amount", "bank"}


def test_statement_completeness_detects_empty_and_partial_imports(tmp_path):
    service = make_service(tmp_path)
    db = service.database
    db.connection.execute("DELETE FROM processing_log")
    db.connection.execute("DELETE FROM transactions")
    db.log_processing(
        source="MANUAL", sha256="a" * 64, bank="enpara", status="SUCCESS",
        transaction_count=0, inserted_count=0, file_path="x",
    )
    statuses = {item.bank: item.status for item in service.statement_completeness("2026-08")}
    assert statuses["enpara"] == "WAITING_FOR_TRANSACTIONS"

    db.log_processing(
        source="MANUAL", sha256="a" * 64, bank="enpara", status="SUCCESS",
        transaction_count=8, inserted_count=2, file_path="x",
    )
    statuses = {item.bank: item.status for item in service.statement_completeness("2026-08")}
    assert statuses["enpara"] == "PARTIAL"


def test_statement_with_transactions_but_without_success_audit_is_legacy_unverified(tmp_path):
    service = make_service(tmp_path)
    db = service.database
    db.connection.execute("DELETE FROM processing_log")
    statuses = {item.bank: item.status for item in service.statement_completeness("2026-08")}
    assert statuses["enpara"] == "LEGACY_UNVERIFIED"


def test_duplicate_only_audit_with_transactions_is_legacy_unverified(tmp_path):
    service = make_service(tmp_path)
    db = service.database
    statement_hash = "a" * 64
    db.connection.execute("DELETE FROM processing_log")
    db.log_processing(
        source="GMAIL", sha256=statement_hash, bank="enpara", status="SKIPPED_DUPLICATE",
        transaction_count=0, inserted_count=0, file_path="x",
    )
    statuses = {item.bank: item.status for item in service.statement_completeness("2026-08")}
    assert statuses["enpara"] == "LEGACY_UNVERIFIED"


def test_public_analysis_does_not_expose_card_identifiers(tmp_path):
    public = make_service(tmp_path).analyze("2026-08").to_public_dict()
    assert "by_card" not in public
    assert "top_transactions" not in public


def test_top_n_must_not_be_negative(tmp_path):
    try:
        make_service(tmp_path).analyze("2026-08", top_n=-1)
    except ValueError as exc:
        assert "top_n" in str(exc)
    else:
        raise AssertionError("negative top_n must fail")


def test_cash_advance_and_other_are_separate_from_spending(tmp_path):
    service = make_service(tmp_path)
    db = service.database
    statement_id = db.connection.execute("SELECT id FROM statements LIMIT 1").fetchone()[0]
    tx = Transaction(
        bank="enpara", card_identifier="****1234", statement_id=statement_id,
        statement_period_start=date(2026, 7, 5), statement_period_end=date(2026, 8, 4),
        transaction_date=date(2026, 8, 9), merchant_raw="CASH", amount=Decimal("40"),
        transaction_type=TransactionType.CASH_ADVANCE,
    )
    assert db.insert_transaction(tx)
    tx = Transaction(
        bank="enpara", card_identifier="****1234", statement_id=statement_id,
        statement_period_start=date(2026, 7, 5), statement_period_end=date(2026, 8, 4),
        transaction_date=date(2026, 8, 10), merchant_raw="OTHER", amount=Decimal("60"),
        transaction_type=TransactionType.OTHER,
    )
    assert db.insert_transaction(tx)
    analysis = service.analyze("2026-08")
    assert analysis.total_spending == Decimal("160.00")
    assert analysis.cash_advance_total == Decimal("40.00")


def test_trend_supports_three_six_twelve_months(tmp_path):
    service = make_service(tmp_path)
    assert len(service.trend("2026-08", 3)) == 3
    assert len(service.trend("2026-08", 6)) == 6
    assert len(service.trend("2026-08", 12)) == 12
