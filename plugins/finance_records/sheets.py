"""Sheet adapters for the finance_records plugin.

The dry-run adapter is deterministic and in-memory for tests/development.  The
live adapter is deliberately fail-closed unless Sheets are explicitly enabled and
service-account based auth/mappings are available.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

AUDIT_HEADERS = [
    "event_id", "telegram_chat_id", "telegram_message_id", "received_at", "type",
    "occurred_date", "payment_date", "source", "creditor", "amount", "status",
    "raw_text", "correction_of", "processing_status", "error", "created_at",
]
DEFAULT_CASHFLOW_SPREADSHEET_ID = "1QXrtN2MVNfvjIFYUlcpuI8yEPjrFDLCep07eMYOcc0Q"
DEFAULT_REPAYMENT_SPREADSHEET_ID = "1ufERJYzKAMZUoErVFXN02eEXdzd4FHivS17gCrFNoiI"


class FinanceSheetError(RuntimeError):
    """Raised when a sheet read/write cannot be completed safely."""


class FinanceSheetAdapter:
    def has_event(self, chat_id: str, message_id: str) -> bool: raise NotImplementedError
    def append_audit(self, row: dict[str, Any]) -> None: raise NotImplementedError
    def update_audit_status(self, event_id: str, status: str, error: str = "") -> None: raise NotImplementedError
    def append_cashflow(self, record: dict[str, Any]) -> None: raise NotImplementedError
    def append_repayment(self, record: dict[str, Any]) -> None: raise NotImplementedError
    def current_month_shortage(self, yyyy_mm: str) -> int: raise NotImplementedError
    def latest_records(self, limit: int = 5) -> list[dict[str, Any]]: raise NotImplementedError
    def find_latest_applied(self, *, amount: int | None = None) -> dict[str, Any] | None: raise NotImplementedError
    def mark_corrected(self, original_event_id: str, new_amount: int) -> None: raise NotImplementedError
    def cancel_original(self, original_event_id: str) -> None: raise NotImplementedError
    def scan_formula_errors(self) -> list[str]: return []
    def check_pattern_1_to_1000(self) -> bool: return True
    def check_repayment_linkage(self) -> bool: return True


@dataclass
class DryRunSheetAdapter(FinanceSheetAdapter):
    initial_shortage: int = 100_000
    audit_rows: list[dict[str, Any]] = field(default_factory=list)
    cashflow_rows: list[dict[str, Any]] = field(default_factory=list)
    repayment_rows: list[dict[str, Any]] = field(default_factory=list)
    formula_errors: list[str] = field(default_factory=list)
    pattern_ok: bool = True
    linkage_ok: bool = True

    def has_event(self, chat_id: str, message_id: str) -> bool:
        return any(
            str(r.get("telegram_chat_id")) == str(chat_id)
            and str(r.get("telegram_message_id")) == str(message_id)
            and r.get("processing_status") in {"pending", "applied"}
            for r in self.audit_rows
        )

    def append_audit(self, row: dict[str, Any]) -> None:
        self.audit_rows.append({h: row.get(h) for h in AUDIT_HEADERS})

    def update_audit_status(self, event_id: str, status: str, error: str = "") -> None:
        for row in reversed(self.audit_rows):
            if row.get("event_id") == event_id:
                row["processing_status"] = status
                row["error"] = error
                return
        raise FinanceSheetError("audit event not found")

    def append_cashflow(self, record: dict[str, Any]) -> None:
        self.cashflow_rows.append(dict(record))

    def append_repayment(self, record: dict[str, Any]) -> None:
        if any(r.get("cashflow_event_id") == record.get("cashflow_event_id") for r in self.repayment_rows):
            return
        self.repayment_rows.append(dict(record))

    def _active_cashflow(self) -> list[dict[str, Any]]:
        return [r for r in self.cashflow_rows if not r.get("cancelled")]

    def current_month_shortage(self, yyyy_mm: str) -> int:
        shortage = self.initial_shortage
        for row in self._active_cashflow():
            payment_date = str(row.get("payment_date") or row.get("occurred_date") or "")
            if not payment_date.startswith(yyyy_mm):
                continue
            typ = row.get("type")
            amount = int(row.get("amount") or 0)
            status = row.get("status")
            if typ == "income" and status == "received":
                shortage -= amount
            elif typ in {"expense", "repayment"}:
                shortage += amount
        return shortage

    def latest_records(self, limit: int = 5) -> list[dict[str, Any]]:
        return list(reversed(self.cashflow_rows))[:limit]

    def find_latest_applied(self, *, amount: int | None = None) -> dict[str, Any] | None:
        for row in reversed(self.cashflow_rows):
            if row.get("cancelled"):
                continue
            if amount is None or int(row.get("amount") or 0) == int(amount):
                return row
        return None

    def mark_corrected(self, original_event_id: str, new_amount: int) -> None:
        for row in reversed(self.cashflow_rows):
            if row.get("event_id") == original_event_id:
                row["amount"] = int(new_amount)
                row["corrected"] = True
                return
        raise FinanceSheetError("original record not found")

    def cancel_original(self, original_event_id: str) -> None:
        for row in reversed(self.cashflow_rows):
            if row.get("event_id") == original_event_id:
                row["cancelled"] = True
                return
        raise FinanceSheetError("original record not found")

    def scan_formula_errors(self) -> list[str]:
        return list(self.formula_errors)

    def check_pattern_1_to_1000(self) -> bool:
        return self.pattern_ok

    def check_repayment_linkage(self) -> bool:
        return self.linkage_ok and all(r.get("cashflow_event_id") for r in self.repayment_rows)


class GoogleSheetsAdapter(FinanceSheetAdapter):
    """Fail-closed Google Sheets adapter skeleton.

    Live writes require FINANCE_SHEETS_ENABLED=true, FINANCE_DRY_RUN=false,
    GOOGLE_APPLICATION_CREDENTIALS pointing to a service-account file, and a
    future explicit SheetLayout mapping. Until mappings are confirmed, methods
    raise FinanceSheetError instead of guessing ranges/columns.
    """

    def __init__(self, *, cashflow_spreadsheet_id: str, repayment_spreadsheet_id: str) -> None:
        self.cashflow_spreadsheet_id = cashflow_spreadsheet_id
        self.repayment_spreadsheet_id = repayment_spreadsheet_id
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not credentials_path or not os.path.exists(credentials_path):
            raise FinanceSheetError("Google service-account credentials are not configured")
        try:
            __import__("google.oauth2.service_account")
            __import__("googleapiclient.discovery")
        except Exception as exc:  # pragma: no cover - depends on optional Google packages
            raise FinanceSheetError("Google Sheets client libraries are unavailable") from exc
        raise FinanceSheetError("Live SheetLayout mappings are not confirmed; refusing live writes")


def build_adapter_from_env() -> FinanceSheetAdapter:
    dry_run = os.getenv("FINANCE_DRY_RUN", "true").strip().lower() not in {"0", "false", "no", "off"}
    enabled = os.getenv("FINANCE_SHEETS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if dry_run or not enabled:
        return DryRunSheetAdapter()
    return GoogleSheetsAdapter(
        cashflow_spreadsheet_id=os.getenv("CASHFLOW_SPREADSHEET_ID", DEFAULT_CASHFLOW_SPREADSHEET_ID),
        repayment_spreadsheet_id=os.getenv("REPAYMENT_SPREADSHEET_ID", DEFAULT_REPAYMENT_SPREADSHEET_ID),
    )
