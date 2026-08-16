from datetime import date

import pytest

from app.categorization.rules import categorize, normalize_merchant
from app.config import load_config
from app.database.schema import initialize_database
from app.models import Statement, Transaction, TransactionType
from app.parsers.base import StatementDocument, StatementParser


def test_transaction_rejects_full_card_identifier():
    with pytest.raises(ValueError):
        Transaction(
            bank="bank_1",
            card_identifier="1234567890123456",
            statement_id="s1",
            statement_period_start=date(2026, 8, 1),
            statement_period_end=date(2026, 8, 31),
            transaction_date=date(2026, 8, 10),
            merchant_raw="TEST",
            amount=10,
        )


def test_transaction_preserves_refund_as_negative_spending():
    tx = Transaction(
        bank="bank_1",
        card_identifier="****1234",
        statement_id="s1",
        statement_period_start=date(2026, 8, 1),
        statement_period_end=date(2026, 8, 31),
        transaction_date=date(2026, 8, 10),
        merchant_raw="MIGROS TICARET A.S.",
        amount=-125.50,
        transaction_type=TransactionType.REFUND,
    )
    assert tx.amount == -125.50
    assert tx.transaction_type is TransactionType.REFUND


def test_config_loads_three_configurable_banks(tmp_path):
    config = load_config(tmp_path / "config")
    assert {"banka_1", "banka_2", "banka_3"}.issubset(config.banks)
    assert "isbank_maximum" in config.banks
    assert config.banks["banka_1"].senders == ["example@bank1.com"]


def test_normalize_and_categorize_use_deterministic_rules():
    merchant = normalize_merchant("MIGROS TICARET A.S. ANKARA TR 1234")
    result = categorize(merchant, "market alışverişi")
    assert merchant == "MIGROS"
    assert result.category == "Market"
    assert result.source == "rule"


def test_common_non_sensitive_merchants_have_deterministic_categories():
    expected = {
        "FILE MARKET": ("Market", None),
        "OPET ISTASYONU": ("Akaryakıt", None),
        "TCDD TAŞIMACILIK": ("Ulaşım", None),
        "OTOPARK": ("Ulaşım", None),
    }

    for merchant, (category, subcategory) in expected.items():
        result = categorize(normalize_merchant(merchant))
        assert (result.category, result.subcategory) == (category, subcategory)
        assert result.source == "rule"


def test_database_schema_supports_statement_and_transaction_roundtrip(tmp_path):
    db = initialize_database(tmp_path / "finance.duckdb")
    statement = Statement(
        bank="bank_1",
        message_id="gmail-message-1",
        attachment_sha256="a" * 64,
        file_path="data/statements/bank_1/example.pdf",
        statement_period_start=date(2026, 8, 1),
        statement_period_end=date(2026, 8, 31),
    )
    db.insert_statement(statement)
    tx = Transaction(
        bank="bank_1",
        card_identifier="****1234",
        statement_id=statement.id,
        statement_period_start=date(2026, 8, 1),
        statement_period_end=date(2026, 8, 31),
        transaction_date=date(2026, 8, 10),
        merchant_raw="MIGROS",
        amount=100,
    )
    assert db.insert_transaction(tx) is True
    assert db.insert_transaction(tx) is False
    assert db.count_transactions() == 1
    db.close()


def test_parser_interface_is_explicit():
    parser = StatementParser()
    document = StatementDocument(path="example.pdf", text="")
    assert parser.can_parse(document) is False
    with pytest.raises(NotImplementedError):
        parser.parse_metadata(document)
