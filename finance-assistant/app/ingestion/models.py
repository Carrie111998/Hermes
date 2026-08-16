from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.models import IngestionSource, IngestionStatus


@dataclass(slots=True)
class ProcessingResult:
    source: IngestionSource
    file_name: str
    sha256: str
    source_external_id: str | None = None
    bank: str | None = None
    statement_id: str | None = None
    statement_period_start: date | None = None
    statement_period_end: date | None = None
    transactions_found: int = 0
    transactions_inserted: int = 0
    duplicates_skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: IngestionStatus = IngestionStatus.FAILED
    archive_path: str | None = None
    failed_path: str | None = None

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def safe_name(self) -> str:
        return f"document-{self.sha256[:12]}.pdf"
