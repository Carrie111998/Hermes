from __future__ import annotations

from dataclasses import dataclass

from app.models import Transaction, TransactionType


FEE_TERMS = {
    "kart aidatı": TransactionType.FEE,
    "yıllık kart ücreti": TransactionType.FEE,
    "üyelik ücreti": TransactionType.FEE,
    "kart kullanım ücreti": TransactionType.FEE,
    "nakit avans ücreti": TransactionType.FEE,
    "işlem ücreti": TransactionType.FEE,
    "gecikme faizi": TransactionType.INTEREST,
    "akdi faiz": TransactionType.INTEREST,
    "faiz": TransactionType.INTEREST,
    "kkdf": TransactionType.TAX,
    "bsmv": TransactionType.TAX,
}


@dataclass(frozen=True, slots=True)
class FeeDetection:
    transaction_type: TransactionType
    label: str


def detect_fee(description: str) -> FeeDetection | None:
    # Turkish capital İ casefolds to ``i`` plus a combining dot.
    def normalize(value: str) -> str:
        return value.casefold().replace("\u0307", "").replace("ı", "i")

    lowered = normalize(description)
    for term, tx_type in FEE_TERMS.items():
        if normalize(term) in lowered:
            return FeeDetection(tx_type, term)
    return None
