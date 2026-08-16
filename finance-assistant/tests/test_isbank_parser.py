from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import TransactionType
from app.database import initialize_database
from app.models import Statement, Transaction
from app.parsers.base import ParserFormatError, StatementDocument
from app.parsers.isbank_maximum import IsbankMaximumParser


HEADER = """isbank.com.tr/0 850 724 0 724 maximum.com.tr
MAXIMUM VISA Klasik Hesap Özetiniz
Hesap Kesim Tarihi: 04.08.2026
Son Ödeme Tarihi: 14.08.2026
Hesap Özeti Borcu: 12.713,84 TL
Ödenmesi Gereken Asgari Tutar: *5.085,54 TL
Kart Numarası: 4543********4187
    İŞLEM TARİHİ AÇIKLAMA TUTAR TAKSİT BİLGİSİ MAXİPUAN
"""


def document(body: str) -> StatementDocument:
    return StatementDocument(Path("statement.pdf"), HEADER + body)


def parse(body: str):
    parser = IsbankMaximumParser()
    doc = document(body)
    metadata = parser.parse_metadata(doc)
    return metadata, parser.parse_transactions(doc, metadata)


def test_can_parse_uses_bank_anchors_not_filename():
    parser = IsbankMaximumParser()
    assert parser.can_parse(document("04/07/2026 MARKET 45,00"))
    assert not parser.can_parse(StatementDocument(Path("maximum.pdf"), "Kredi kartı özeti"))


def test_statement_metadata():
    metadata, _ = parse("04/07/2026 MARKET 45,00\n")
    assert metadata.period_start == date(2026, 7, 4)
    assert metadata.period_end == date(2026, 8, 4)
    assert metadata.statement_date == date(2026, 8, 4)
    assert metadata.due_date == date(2026, 8, 14)
    assert metadata.card_identifier == "****4187"
    assert metadata.currency == "TRY"
    assert metadata.statement_total == Decimal("12713.84")
    assert metadata.minimum_payment == Decimal("5085.54")


def test_single_transaction_and_turkish_amount():
    _, transactions = parse("04/07/2026 TCDD TAŞIMACILIK A.Ş ANKARA TR 1.234,56\n")
    assert len(transactions) == 1
    assert transactions[0].transaction_date == date(2026, 7, 4)
    assert transactions[0].merchant_raw == "TCDD TAŞIMACILIK A.Ş ANKARA TR"
    assert transactions[0].amount == Decimal("1234.56")
    assert transactions[0].transaction_type == TransactionType.PURCHASE


def test_multiple_transactions_and_multiline_description():
    _, transactions = parse(
        "06/07/2026 MARKET A ANKARA TR 45,00\n"
        "07/07/2026 UZUN MAĞAZA AÇIKLAMASI\n"
        "ANKARA TR 850,25\n"
    )
    assert len(transactions) == 2
    assert transactions[1].description_raw.endswith("ANKARA TR")
    assert transactions[1].amount == Decimal("850.25")


def test_refund_payment_fee_interest_and_tax_types():
    _, transactions = parse(
        "21/07/2026 WWW.TRENDYOL.COM ISTANBUL TR -236,10\n"
        "13/07/2026 HESAPTAN AKTARIM ÖDEME -19.399,19\n"
        "01/08/2026 KART AİDATI 150,00\n"
        "02/08/2026 AKDİ FAİZ 100,00\n"
        "03/08/2026 BSMV 5,00\n"
        "04/08/2026 KKDF 2,00\n"
    )
    assert [tx.transaction_type for tx in transactions] == [
        TransactionType.REFUND,
        TransactionType.PAYMENT,
        TransactionType.FEE,
        TransactionType.INTEREST,
        TransactionType.TAX,
        TransactionType.TAX,
    ]


def test_hesaptan_aktarim_is_payment_even_when_amount_is_negative():
    _, transactions = parse("13/07/2026 4535-239328 HESAPTAN AKTARIM 4535 İNTERAKTİF -19.399,19\n")
    assert transactions[0].transaction_type == TransactionType.PAYMENT


def test_installment_is_extracted_without_using_total_as_amount():
    _, transactions = parse("08/07/2026 PIERRE CARDIN MIGROS ANKARA TR 2.203,25 4/4 taksidi (8.813,00)\n")
    tx = transactions[0]
    assert tx.amount == Decimal("2203.25")
    assert (tx.installment_current, tx.installment_total) == (4, 4)
    assert tx.transaction_type == TransactionType.INSTALLMENT


def test_missing_table_header_raises_format_error():
    parser = IsbankMaximumParser()
    with pytest.raises(ParserFormatError, match="transaction table header"):
        parser.parse_transactions(
            StatementDocument(Path("statement.pdf"), HEADER.replace("İŞLEM TARİHİ AÇIKLAMA TUTAR TAKSİT BİLGİSİ MAXİPUAN", "") + "04/07/2026 MARKET 45,00"),
            parser.parse_metadata(document("04/07/2026 MARKET 45,00\n")),
        )


def test_duplicate_statement_and_transaction_are_skipped(tmp_path):
    db = initialize_database(tmp_path / "finance.duckdb")
    statement = Statement(
        bank="isbank_maximum",
        message_id="local",
        attachment_sha256="a" * 64,
        file_path="/tmp/statement.pdf",
    )
    assert db.insert_statement(statement)
    assert not db.insert_statement(statement)
    tx = Transaction(
        bank="isbank_maximum",
        card_identifier="****4187",
        statement_id=statement.id,
        statement_period_start=None,
        statement_period_end=None,
        transaction_date=date(2026, 7, 4),
        merchant_raw="MARKET",
        amount=Decimal("45.00"),
    )
    assert db.insert_transaction(tx)
    assert not db.insert_transaction(tx)
    db.close()
