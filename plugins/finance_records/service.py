"""Core processing service for finance_records."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .parser import AMBIGUOUS_INCOME_REPLY, FinanceIntent, parse_finance_message
from .sheets import AUDIT_HEADERS, FinanceSheetAdapter, FinanceSheetError


@dataclass(frozen=True)
class ProcessResult:
    handled: bool
    reply: str | None = None
    success: bool = False
    record: dict[str, Any] | None = None


def event_id_for(chat_id: str, message_id: str) -> str:
    digest = hashlib.sha256(f"telegram:{chat_id}:{message_id}".encode("utf-8")).hexdigest()[:16]
    return f"fin_{digest}"


def _now(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))


def _audit_row(*, event_id: str, chat_id: str, message_id: str, received_at: str, intent: FinanceIntent, raw_text: str, status: str, error: str = "", correction_of: str | None = None, created_at: str | None = None) -> dict[str, Any]:
    row = {
        "event_id": event_id,
        "telegram_chat_id": str(chat_id),
        "telegram_message_id": str(message_id),
        "received_at": received_at,
        "type": intent.type,
        "occurred_date": intent.occurred_date,
        "payment_date": intent.payment_date,
        "source": intent.source,
        "creditor": intent.creditor,
        "amount": intent.correction_amount if intent.type == "correction" else intent.amount,
        "status": intent.status,
        "raw_text": raw_text,
        "correction_of": correction_of,
        "processing_status": status,
        "error": error,
        "created_at": created_at or received_at,
    }
    return {h: row.get(h) for h in AUDIT_HEADERS}


class FinanceProcessor:
    def __init__(self, adapter: FinanceSheetAdapter, *, timezone: str = "Asia/Tokyo") -> None:
        self.adapter = adapter
        self.timezone = timezone

    def parse(self, text: str, *, now: datetime | None = None) -> FinanceIntent | None:
        return parse_finance_message(text, now=now, timezone=self.timezone)

    def process(self, *, text: str, telegram_chat_id: str, telegram_message_id: str, received_at: datetime | None = None) -> ProcessResult:
        received_dt = received_at or _now(self.timezone)
        received_iso = received_dt.astimezone(ZoneInfo(self.timezone)).isoformat() if received_dt.tzinfo else received_dt.replace(tzinfo=ZoneInfo(self.timezone)).isoformat()
        intent = self.parse(text, now=received_dt)
        if intent is None:
            return ProcessResult(handled=False)
        eid = event_id_for(telegram_chat_id, telegram_message_id)
        yyyy_mm = received_iso[:7]

        if self.adapter.has_event(telegram_chat_id, telegram_message_id):
            return ProcessResult(handled=True, success=True, reply="このTelegramメッセージは既に記録済みです（重複のため追記しません）。")

        if intent.type == "income" and not intent.payment_date:
            amount_text = f"{int(intent.amount or 0):,}円" if intent.amount else "その収入"
            return ProcessResult(
                handled=True,
                success=False,
                reply=AMBIGUOUS_INCOME_REPLY.replace("6,000円", amount_text),
                record=intent.as_record(),
            )

        before = self.adapter.current_month_shortage(yyyy_mm)
        try:
            if intent.type == "query":
                return self._process_query(intent, yyyy_mm)
            if intent.type == "correction":
                return self._process_correction(intent, eid, telegram_chat_id, telegram_message_id, received_iso, text, before)
            if intent.type == "cancellation":
                return self._process_cancellation(intent, eid, telegram_chat_id, telegram_message_id, received_iso, text, before)
            return self._process_record(intent, eid, telegram_chat_id, telegram_message_id, received_iso, text, before)
        except Exception as exc:
            error = str(exc)
            try:
                if self.adapter.has_event(telegram_chat_id, telegram_message_id):
                    self.adapter.update_audit_status(eid, "error", error)
                else:
                    self.adapter.append_audit(_audit_row(event_id=eid, chat_id=telegram_chat_id, message_id=telegram_message_id, received_at=received_iso, intent=intent, raw_text=text, status="error", error=error))
            except Exception:
                pass
            return ProcessResult(handled=True, success=False, reply=f"記録に失敗しました。成功として扱いません。エラー: {error}")

    def _preflight_checks(self) -> None:
        errors = self.adapter.scan_formula_errors()
        if errors:
            raise FinanceSheetError("formula errors detected: " + ", ".join(errors))
        if not self.adapter.check_pattern_1_to_1000():
            raise FinanceSheetError("1..1000 pattern check failed")
        if not self.adapter.check_repayment_linkage():
            raise FinanceSheetError("repayment linkage check failed")

    def _process_record(self, intent: FinanceIntent, eid: str, chat_id: str, message_id: str, received_iso: str, text: str, before: int) -> ProcessResult:
        self._preflight_checks()
        record = intent.as_record() | {"event_id": eid, "currency": "JPY"}
        self.adapter.append_audit(_audit_row(event_id=eid, chat_id=chat_id, message_id=message_id, received_at=received_iso, intent=intent, raw_text=text, status="pending"))
        self.adapter.append_cashflow(record)
        if intent.type == "repayment":
            self.adapter.append_repayment({
                "event_id": f"{eid}_repayment",
                "cashflow_event_id": eid,
                "creditor": intent.creditor,
                "amount": intent.amount,
                "payment_date": intent.payment_date or intent.occurred_date,
                "currency": "JPY",
            })
        self.adapter.update_audit_status(eid, "applied")
        after = self.adapter.current_month_shortage(received_iso[:7])
        reply = self._format_record_reply(intent, eid, before, after)
        return ProcessResult(handled=True, success=True, reply=reply, record=record)

    def _format_record_reply(self, intent: FinanceIntent, eid: str, before: int, after: int) -> str:
        amount = f"{int(intent.amount or 0):,}円"
        if intent.type == "income":
            label = f"{intent.source or '収入'}{amount}"
            state = "入金済み" if intent.status == "received" else "入金予定"
            first = f"✅ {label}を{state}で記録しました"
        elif intent.type == "expense":
            first = f"✅ {intent.source or '支出'}{amount}の支出を記録しました"
        elif intent.type == "repayment":
            first = f"✅ {intent.creditor or '返済先'}への返済{amount}を記録しました"
        else:
            first = f"✅ {amount}を記録しました"
        return f"{first}\n今月の不足：{before:,}円 → {after:,}円\n記録ID：{eid}"

    def _process_correction(self, intent: FinanceIntent, eid: str, chat_id: str, message_id: str, received_iso: str, text: str, before: int) -> ProcessResult:
        original = self.adapter.find_latest_applied(amount=intent.amount)
        if not original or intent.correction_amount is None:
            raise FinanceSheetError("correction target not found")
        self._preflight_checks()
        self.adapter.append_audit(_audit_row(event_id=eid, chat_id=chat_id, message_id=message_id, received_at=received_iso, intent=intent, raw_text=text, status="pending", correction_of=str(original["event_id"])))
        self.adapter.mark_corrected(str(original["event_id"]), int(intent.correction_amount))
        self.adapter.update_audit_status(eid, "applied")
        after = self.adapter.current_month_shortage(received_iso[:7])
        return ProcessResult(handled=True, success=True, reply=f"訂正しました。record_id: {eid}\n今月の不足: {before:,}円 → {after:,}円")

    def _process_cancellation(self, intent: FinanceIntent, eid: str, chat_id: str, message_id: str, received_iso: str, text: str, before: int) -> ProcessResult:
        original = self.adapter.find_latest_applied()
        if not original:
            raise FinanceSheetError("cancellation target not found")
        self._preflight_checks()
        self.adapter.append_audit(_audit_row(event_id=eid, chat_id=chat_id, message_id=message_id, received_at=received_iso, intent=intent, raw_text=text, status="pending", correction_of=str(original["event_id"])))
        self.adapter.cancel_original(str(original["event_id"]))
        self.adapter.update_audit_status(eid, "applied")
        after = self.adapter.current_month_shortage(received_iso[:7])
        return ProcessResult(handled=True, success=True, reply=f"取り消しました。record_id: {eid}\n今月の不足: {before:,}円 → {after:,}円")

    def _process_query(self, intent: FinanceIntent, yyyy_mm: str) -> ProcessResult:
        if intent.query == "latest5":
            rows = self.adapter.latest_records(5)
            if not rows:
                return ProcessResult(handled=True, success=True, reply="直近の記録はありません。")
            lines = ["直近の記録5件:"]
            for row in rows:
                lines.append(f"- {row.get('event_id')}: {row.get('type')} {int(row.get('amount') or 0):,}円")
            return ProcessResult(handled=True, success=True, reply="\n".join(lines))
        shortage = self.adapter.current_month_shortage(yyyy_mm)
        return ProcessResult(handled=True, success=True, reply=f"今月あと {shortage:,}円 足りません。")
