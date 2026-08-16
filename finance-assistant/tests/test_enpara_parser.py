from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import TransactionType
from app.parsers.base import ParserFormatError, StatementDocument
from app.parsers.enpara import EnparaParser


HEADER = """en para Kredi Kartı Ekstresi
Ekstre tarihi 04/08/2026
Ekstre borcu 711,44 TL
Kart numarası 5269 11***** 3339
Minimum ödeme tutarı 143,00 TL
Son ödeme tarihi 14/08/2026
İşlem tarihi Açıklama Taksit Tutar
"""


def document(body: str) -> StatementDocument:
    return StatementDocument(Path("anonymous-enpara.pdf"), HEADER + body)


def parse(body: str):
    parser = EnparaParser()
    doc = document(body)
    metadata = parser.parse_metadata(doc)
    return metadata, parser.parse_transactions(doc, metadata)


def test_enpara_can_parse_uses_content_anchors_not_filename():
    assert EnparaParser().can_parse(document("13/07/2026 Ödeme - Enpara.com Cep Şubesi - 427,00 TL"))
    assert not EnparaParser().can_parse(
        StatementDocument(Path("enpara.pdf"), "Kredi kartı özeti İşlem tarihi Açıklama Tutar")
    )


def test_enpara_metadata_and_derived_period():
    metadata, _ = parse("13/07/2026 Ödeme - Enpara.com Cep Şubesi - 427,00 TL")
    assert metadata.period_start == date(2026, 7, 5)
    assert metadata.period_end == date(2026, 8, 4)
    assert metadata.cutoff_date == date(2026, 8, 4)
    assert metadata.statement_date == date(2026, 8, 4)
    assert metadata.due_date == date(2026, 8, 14)
    assert metadata.statement_total == Decimal("711.44")
    assert metadata.minimum_payment == Decimal("143.00")
    assert metadata.card_identifier == "****3339"
    assert metadata.currency == "TRY"


def test_enpara_transactions_and_payment_sign():
    _, transactions = parse(
        "13/07/2026 Ödeme - Enpara.com Cep Şubesi - 427,00 TL\n"
        "22/07/2026 5538472096-TURKCELL-ÖDEME 434,00 TL\n"
    )
    assert len(transactions) == 2
    assert transactions[0].amount == Decimal("-427.00")
    assert transactions[0].transaction_type == TransactionType.PAYMENT
    assert transactions[1].amount == Decimal("434.00")
    assert transactions[1].transaction_type == TransactionType.PURCHASE


def test_enpara_virtual_card_section_is_masked_per_transaction():
    _, transactions = parse(
        "5269 11* *** 7702 numaralı sanal kredi kartınızla yapılan işlemler\n"
        "11/07/2026 OPENROUTER,INC(5,80USD) 277,44 TL\n"
    )
    assert len(transactions) == 1
    assert transactions[0].card_identifier == "****7702"
    assert transactions[0].merchant_raw == "OPENROUTER,INC(5,80USD)"
    assert transactions[0].amount == Decimal("277.44")


def test_enpara_card_section_boundary_keeps_primary_card_on_previous_row():
    _, transactions = parse(
        "13/07/2026 Ödeme - Enpara.com Cep Şubesi - 427,00 TL\n"
        "5269 11* *** 7702 numaralı sanal kredi kartınızla yapılan işlemler\n"
        "11/07/2026 OPENROUTER,INC 277,44 TL\n"
    )
    assert transactions[0].card_identifier == "****3339"
    assert transactions[1].card_identifier == "****7702"


def test_enpara_missing_header_raises_format_error():
    parser = EnparaParser()
    doc = StatementDocument(Path("anonymous-enpara.pdf"), HEADER.replace("İşlem tarihi Açıklama Taksit Tutar", ""))
    with pytest.raises(ParserFormatError, match="transaction table header"):
        parser.parse_metadata(doc)


def test_enpara_rejects_empty_transaction_section():
    parser = EnparaParser()
    with pytest.raises(ParserFormatError, match="no transaction rows"):
        parser.parse_transactions(document(""), parser.parse_metadata(document("")))
