from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from plugins.finance_records import on_pre_gateway_dispatch, set_test_adapter
from plugins.finance_records.parser import AMBIGUOUS_INCOME_REPLY, parse_finance_message
from plugins.finance_records.service import FinanceProcessor
from plugins.finance_records.sheets import DryRunSheetAdapter, FinanceSheetError

TOKYO_NOW = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))


def processor(adapter: DryRunSheetAdapter | None = None) -> FinanceProcessor:
    return FinanceProcessor(adapter or DryRunSheetAdapter(), timezone="Asia/Tokyo")


def test_duplicate_message_only_applied_once() -> None:
    adapter = DryRunSheetAdapter(initial_shortage=100_000)
    p = processor(adapter)
    first = p.process(text="今日シェアフルで6,000円稼いだ。今日入金済み", telegram_chat_id="1", telegram_message_id="m1", received_at=TOKYO_NOW)
    second = p.process(text="今日シェアフルで6,000円稼いだ。今日入金済み", telegram_chat_id="1", telegram_message_id="m1", received_at=TOKYO_NOW)
    assert first.success is True
    assert second.success is True
    assert len(adapter.cashflow_rows) == 1
    assert len([r for r in adapter.audit_rows if r["processing_status"] == "applied"]) == 1
    assert "重複" in (second.reply or "")


def test_same_day_received_income_reduces_shortage_by_6000() -> None:
    adapter = DryRunSheetAdapter(initial_shortage=100_000)
    p = processor(adapter)
    before = adapter.current_month_shortage("2026-08")
    result = p.process(text="今日シェアフルで6,000円稼いだ。今日入金済み", telegram_chat_id="1", telegram_message_id="m2", received_at=TOKYO_NOW)
    after = adapter.current_month_shortage("2026-08")
    assert result.success is True
    assert before - after == 6000
    assert adapter.cashflow_rows[0]["currency"] == "JPY"


def test_future_income_does_not_change_current_month_shortage() -> None:
    adapter = DryRunSheetAdapter(initial_shortage=100_000)
    p = processor(adapter)
    before = adapter.current_month_shortage("2026-08")
    result = p.process(text="今日シェアフルで6,000円稼いだ。9月15日入金予定", telegram_chat_id="1", telegram_message_id="m3", received_at=TOKYO_NOW)
    after = adapter.current_month_shortage("2026-08")
    assert result.success is True
    assert after == before
    assert adapter.cashflow_rows[0]["status"] == "scheduled"


def test_expense_sign_increases_shortage_and_is_expense() -> None:
    adapter = DryRunSheetAdapter(initial_shortage=100_000)
    p = processor(adapter)
    p.process(text="今日松屋で750円使った", telegram_chat_id="1", telegram_message_id="m4", received_at=TOKYO_NOW)
    assert adapter.cashflow_rows[0]["type"] == "expense"
    assert adapter.cashflow_rows[0]["amount"] == 750
    assert adapter.current_month_shortage("2026-08") == 100_750


def test_repayment_writes_cashflow_and_repayment_once() -> None:
    adapter = DryRunSheetAdapter(initial_shortage=100_000)
    p = processor(adapter)
    result = p.process(text="今日ぼんに70,000円返した", telegram_chat_id="1", telegram_message_id="m5", received_at=TOKYO_NOW)
    duplicate = p.process(text="今日ぼんに70,000円返した", telegram_chat_id="1", telegram_message_id="m5", received_at=TOKYO_NOW)
    assert result.success is True
    assert duplicate.success is True
    assert len(adapter.cashflow_rows) == 1
    assert len(adapter.repayment_rows) == 1
    assert adapter.repayment_rows[0]["cashflow_event_id"] == adapter.cashflow_rows[0]["event_id"]
    assert adapter.cashflow_rows[0]["type"] == "repayment"
    assert adapter.cashflow_rows[0]["creditor"] == "ぼん"


def test_ambiguous_income_asks_confirmation_and_does_not_write() -> None:
    adapter = DryRunSheetAdapter()
    p = processor(adapter)
    result = p.process(text="今日シェアフルで6,000円稼いだ", telegram_chat_id="1", telegram_message_id="m6", received_at=TOKYO_NOW)
    assert result.handled is True
    assert result.success is False
    assert result.reply == AMBIGUOUS_INCOME_REPLY
    assert adapter.cashflow_rows == []
    assert adapter.audit_rows == []


def test_correction_and_cancellation_append_audit_trail() -> None:
    adapter = DryRunSheetAdapter(initial_shortage=100_000)
    p = processor(adapter)
    original = p.process(text="今日シェアフルで6,000円稼いだ。今日入金済み", telegram_chat_id="1", telegram_message_id="m7", received_at=TOKYO_NOW)
    assert original.record is not None
    corrected = p.process(text="さっきの6,000円は5,800円だった", telegram_chat_id="1", telegram_message_id="m8", received_at=TOKYO_NOW)
    assert corrected.success is True
    assert adapter.cashflow_rows[0]["amount"] == 5800
    assert adapter.audit_rows[-1]["correction_of"] == original.record["event_id"]
    cancelled = p.process(text="さっきの記録を取り消して", telegram_chat_id="1", telegram_message_id="m9", received_at=TOKYO_NOW)
    assert cancelled.success is True
    assert adapter.cashflow_rows[0]["cancelled"] is True
    assert [r["type"] for r in adapter.audit_rows] == ["income", "correction", "cancellation"]
    assert adapter.audit_rows[-1]["correction_of"] == original.record["event_id"]


class FailingAdapter(DryRunSheetAdapter):
    def append_cashflow(self, record):  # type: ignore[no-untyped-def]
        raise FinanceSheetError("simulated google failure")


def test_google_failure_no_success_reply_and_error_audit() -> None:
    adapter = FailingAdapter()
    p = processor(adapter)
    result = p.process(text="今日松屋で750円使った", telegram_chat_id="1", telegram_message_id="m10", received_at=TOKYO_NOW)
    assert result.success is False
    assert "記録しました" not in (result.reply or "")
    assert "失敗" in (result.reply or "")
    assert adapter.audit_rows[-1]["processing_status"] == "error"


def test_latest_5_query() -> None:
    adapter = DryRunSheetAdapter()
    p = processor(adapter)
    for i in range(6):
        p.process(text=f"今日松屋で{i + 1},000円使った", telegram_chat_id="1", telegram_message_id=f"q{i}", received_at=TOKYO_NOW)
    result = p.process(text="直近の記録を5件見せて", telegram_chat_id="1", telegram_message_id="q-latest", received_at=TOKYO_NOW)
    assert result.success is True
    assert (result.reply or "").count("- fin_") == 5
    assert "6,000円" in (result.reply or "")
    assert "1,000円" not in (result.reply or "")


def test_formula_error_check_blocks_write() -> None:
    adapter = DryRunSheetAdapter(formula_errors=["取引実績!C2 #REF!"])
    result = processor(adapter).process(text="今日松屋で750円使った", telegram_chat_id="1", telegram_message_id="m11", received_at=TOKYO_NOW)
    assert result.success is False
    assert adapter.cashflow_rows == []
    assert adapter.audit_rows[-1]["processing_status"] == "error"
    assert "formula errors" in adapter.audit_rows[-1]["error"]


def test_pattern_1_to_1000_preservation_check_blocks_write() -> None:
    adapter = DryRunSheetAdapter(pattern_ok=False)
    result = processor(adapter).process(text="今日松屋で750円使った", telegram_chat_id="1", telegram_message_id="m12", received_at=TOKYO_NOW)
    assert result.success is False
    assert adapter.cashflow_rows == []
    assert "1..1000 pattern" in adapter.audit_rows[-1]["error"]


def test_repayment_linkage_consistency_check_blocks_write() -> None:
    adapter = DryRunSheetAdapter(linkage_ok=False)
    result = processor(adapter).process(text="今日ぼんに70,000円返した", telegram_chat_id="1", telegram_message_id="m13", received_at=TOKYO_NOW)
    assert result.success is False
    assert adapter.cashflow_rows == []
    assert adapter.repayment_rows == []
    assert "repayment linkage" in adapter.audit_rows[-1]["error"]


def test_parser_preserves_required_strict_shape() -> None:
    intent = parse_finance_message("今日シェアフルで6,000円稼いだ。今日入金済み", now=TOKYO_NOW)
    assert intent is not None
    assert intent.as_record() == {
        "type": "income",
        "source": "シェアフル",
        "creditor": None,
        "amount": 6000,
        "occurred_date": "2026-08-10",
        "payment_date": "2026-08-10",
        "status": "received",
        "note": "",
        "confidence": 0.99,
    }


@pytest.mark.asyncio
async def test_hook_allows_unrelated_or_unallowed_and_replies_for_allowed(monkeypatch) -> None:
    adapter = DryRunSheetAdapter()
    set_test_adapter(adapter)
    monkeypatch.setenv("FINANCE_ALLOWED_TELEGRAM_CHAT_IDS", "123")
    source = SimpleNamespace(platform="telegram", chat_id="123")
    event = SimpleNamespace(text="今日松屋で750円使った", message_id="hm1", source=source, timestamp=TOKYO_NOW, metadata={})
    gateway = SimpleNamespace(adapters={source.platform: SimpleNamespace(send=AsyncMock())})
    result = on_pre_gateway_dispatch(event=event, gateway=gateway)
    await asyncio_sleep()
    assert result == {"action": "skip", "reason": "finance_records_handled"}
    gateway.adapters[source.platform].send.assert_awaited_once()

    unrelated = SimpleNamespace(text="こんにちは", message_id="hm2", source=source, timestamp=TOKYO_NOW, metadata={})
    assert on_pre_gateway_dispatch(event=unrelated, gateway=gateway) == {"action": "allow"}
    unallowed = SimpleNamespace(text="今日松屋で750円使った", message_id="hm3", source=SimpleNamespace(platform="telegram", chat_id="999"), timestamp=TOKYO_NOW, metadata={})
    assert on_pre_gateway_dispatch(event=unallowed, gateway=gateway) == {"action": "allow"}
    set_test_adapter(None)


@pytest.mark.asyncio
async def test_hook_skips_with_failure_reply_when_live_adapter_init_fails(monkeypatch) -> None:
    set_test_adapter(None)
    monkeypatch.setenv("FINANCE_ALLOWED_TELEGRAM_CHAT_IDS", "123")
    monkeypatch.setenv("FINANCE_DRY_RUN", "false")
    monkeypatch.setenv("FINANCE_SHEETS_ENABLED", "true")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    source = SimpleNamespace(platform="telegram", chat_id="123")
    event = SimpleNamespace(text="今日松屋で750円使った", message_id="hm-live-fail", source=source, timestamp=TOKYO_NOW, metadata={})
    gateway = SimpleNamespace(adapters={source.platform: SimpleNamespace(send=AsyncMock())})

    result = on_pre_gateway_dispatch(event=event, gateway=gateway)
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "finance_records_init_failed"}
    gateway.adapters[source.platform].send.assert_awaited_once()
    reply = gateway.adapters[source.platform].send.await_args.args[1]
    assert "初期化に失敗" in reply
    assert "成功として扱いません" in reply


async def asyncio_sleep() -> None:
    import asyncio
    await asyncio.sleep(0)
