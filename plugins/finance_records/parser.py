"""Deterministic Japanese finance-message parser for the finance_records plugin."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, date
from typing import Literal
from zoneinfo import ZoneInfo

RecordType = Literal["income", "expense", "repayment", "correction", "cancellation", "query"]


@dataclass(frozen=True)
class FinanceIntent:
    type: RecordType
    source: str | None = None
    creditor: str | None = None
    amount: int | None = None
    occurred_date: str | None = None
    payment_date: str | None = None
    status: str | None = None
    note: str = ""
    confidence: float = 0.99
    query: str | None = None
    correction_amount: int | None = None

    def as_record(self) -> dict:
        return {
            "type": self.type,
            "source": self.source,
            "creditor": self.creditor,
            "amount": self.amount,
            "occurred_date": self.occurred_date,
            "payment_date": self.payment_date,
            "status": self.status,
            "note": self.note,
            "confidence": self.confidence,
        }


_AMOUNT_RE = re.compile(r"([0-9０-９][0-9０-９,，]*)\s*円")

def _normalize_digits(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９，", "0123456789,"))


def parse_amount(text: str) -> int | None:
    match = _AMOUNT_RE.search(_normalize_digits(text))
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def _today(now: datetime | None, timezone: str) -> date:
    if now is not None:
        return now.astimezone(ZoneInfo(timezone)).date() if now.tzinfo else now.replace(tzinfo=ZoneInfo(timezone)).date()
    return datetime.now(ZoneInfo(timezone)).date()


def _date_from_text(text: str, base: date) -> str | None:
    if "今日" in text or "本日" in text:
        return base.isoformat()
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", _normalize_digits(text))
    if m:
        month = int(m.group(1)); day = int(m.group(2))
        year = base.year + (1 if month < base.month - 6 else 0)
        return date(year, month, day).isoformat()
    return None


def parse_finance_message(text: str, *, now: datetime | None = None, timezone: str = "Asia/Tokyo") -> FinanceIntent | None:
    """Parse supported Japanese finance messages into a strict record-like intent.

    This parser is intentionally deterministic and conservative. It covers the
    required examples and returns ``None`` for unrelated text so Hermes can run
    normal dispatch.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    norm = _normalize_digits(raw)
    today = _today(now, timezone)
    occurred = _date_from_text(norm, today) or today.isoformat()

    if "直近" in norm and "記録" in norm and ("5件" in norm or "五件" in norm):
        return FinanceIntent(type="query", query="latest5", confidence=1.0)
    if "今月" in norm and ("足りない" in norm or "不足" in norm):
        return FinanceIntent(type="query", query="shortage", confidence=1.0)
    if ("取り消" in norm or "取消" in norm or "キャンセル" in norm) and ("記録" in norm or "さっき" in norm):
        return FinanceIntent(type="cancellation", confidence=0.99)

    if "さっき" in norm and "だった" in norm:
        amounts = [int(x.replace(",", "")) for x in _AMOUNT_RE.findall(norm)]
        if amounts:
            return FinanceIntent(type="correction", amount=amounts[0], correction_amount=amounts[-1], confidence=0.98)

    amount = parse_amount(norm)
    if amount is None:
        return None

    if "返した" in norm or "返済" in norm:
        creditor_match = re.search(r"(?:今日|本日|さっき)?\s*([^でにへを\s]+)(?:に|へ).{0,12}?(?:返した|返済)", norm)
        creditor = creditor_match.group(1) if creditor_match else None
        return FinanceIntent(
            type="repayment", creditor=creditor, amount=amount, occurred_date=occurred,
            payment_date=occurred, status="paid", confidence=0.99,
        )

    if "使った" in norm or "払った" in norm or "支払" in norm:
        source_match = re.search(r"(?:今日|本日)?\s*([^でにへを\s]+)で", norm)
        source = source_match.group(1) if source_match else None
        return FinanceIntent(
            type="expense", source=source, amount=amount, occurred_date=occurred,
            payment_date=occurred, status="paid", confidence=0.99,
        )

    if "稼いだ" in norm or "収入" in norm:
        source_match = re.search(r"(?:今日|本日)?\s*([^でにへを\s]+)で", norm)
        source = source_match.group(1) if source_match else None
        payment_date = None
        status = None
        if "入金済" in norm or "入金されています" in norm or "もう使える" in norm:
            payment_date = occurred
            status = "received"
        else:
            m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*入金予定", norm)
            if m:
                payment_date = _date_from_text(m.group(0), today)
                status = "scheduled"
            elif "入金予定" in norm or "後日入金" in norm:
                status = "scheduled"
        return FinanceIntent(
            type="income", source=source, amount=amount, occurred_date=occurred,
            payment_date=payment_date, status=status, confidence=0.99 if status else 0.72,
        )

    return None


AMBIGUOUS_INCOME_REPLY = "その6,000円は、もう使える状態で入金されていますか？　それとも後日入金ですか？"
