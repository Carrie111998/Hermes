from datetime import date
from decimal import Decimal

import pytest

from app.models import TransactionType
from app.parsers.axess import AxessParser
from app.parsers.base import ParserFormatError, StatementDocument


HEADER = """Axess Platinum Hesap Özeti Akbank
Müşteri No: [REDACTED]
Kart Numarası: ****4845
Dönem Borcu: 27.311,90 TL
Son Ödeme Tarihi: 12/08/2026
En Az Ödeme Tutarı: 10.924,76 TL
Ekstre Dönemi: 04/07/2026-02/08/2026
Hesap Kesim Tarihi: 02/08/2026
İşlem Tarihi Dönem İçi İşlemler Borç Tutarı (TL) Kalan Borç / Taksit
"""


def document(body: str) -> StatementDocument:
    return StatementDocument("/tmp/anonymous-axess.pdf", HEADER + body + "\nGenel Toplam 27.311,90")


def parse(body: str):
    parser = AxessParser()
    doc = document(body)
    metadata = parser.parse_metadata(doc)
    return metadata, parser.parse_transactions(doc, metadata)


def test_axess_can_parse():
    assert AxessParser().can_parse(document("04/07/2026 MARKET 10,00"))


def test_axess_metadata():
    metadata, _ = parse("04/07/2026 MARKET 10,00")
    assert metadata.period_start == date(2026, 7, 4)
    assert metadata.period_end == date(2026, 8, 2)
    assert metadata.statement_total == Decimal("27311.90")
    assert metadata.minimum_payment == Decimal("10924.76")
    assert metadata.card_identifier == "****4845"


def test_axess_single_transaction():
    _, transactions = parse("04/07/2026 MARKET ANKARA TR 40,00")
    assert len(transactions) == 1
    assert transactions[0].amount == Decimal("40.00")
    assert transactions[0].transaction_type == TransactionType.PURCHASE


def test_axess_multiple_transactions_and_multiline_description():
    _, transactions = parse("""04/07/2026 MARKET ANKARA TR 40,00
05/07/2026 Superonline 00123-Fatura Otomatik Ödeme Talimatınız
800,00
""")
    assert len(transactions) == 2
    assert transactions[1].transaction_type == TransactionType.PAYMENT
    assert transactions[1].amount == Decimal("800.00")
    assert "superonline" in transactions[1].description_raw.casefold()


def test_axess_payment_with_negative_suffix():
    _, transactions = parse("13/07/2026 İnternet Sb-Ödemeniz için teşekkürler 18.380,72(-)")
    assert transactions[0].amount == Decimal("-18380.72")
    assert transactions[0].transaction_type == TransactionType.PAYMENT


def test_axess_chip_para_payment():
    _, transactions = parse("24/07/2026 Chip-Para ile Ödeme 16,61(-)")
    assert transactions[0].transaction_type == TransactionType.PAYMENT


def test_axess_interest():
    _, transactions = parse("02/08/2026 Otomatik Fatura Ödeme Faizi 18,39")
    assert transactions[0].transaction_type == TransactionType.INTEREST


def test_axess_installment_uses_current_amount_and_reverses_total_current_layout():
    _, transactions = parse("25/07/2026 IKEA-MAPA (2.168,29 TL) 3/1.taksit 722,77 722,76x2")
    tx = transactions[0]
    assert tx.amount == Decimal("722.77")
    assert (tx.installment_current, tx.installment_total) == (1, 3)
    assert tx.transaction_type == TransactionType.INSTALLMENT


def test_axess_missing_header():
    parser = AxessParser()
    doc = StatementDocument("/tmp/anonymous-axess.pdf", "Axess Akbank Hesap Özeti")
    with pytest.raises(ParserFormatError):
        parser.parse_metadata(doc)
