from __future__ import annotations

import csv
import html
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from app.analysis.service import AnalysisService, MonthlyAnalysis


@dataclass(frozen=True, slots=True)
class ReportPaths:
    csv_path: Path
    html_path: Path


class ReportService:
    """Create deterministic, aggregate-only reports from an analysis service."""

    def __init__(self, analysis: AnalysisService):
        self.analysis = analysis

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        if path.is_symlink():
            raise ValueError("report target must not be a symlink")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _rows(result: MonthlyAnalysis) -> list[tuple[str, str, str, str, str]]:
        rows: list[tuple[str, str, str, str, str]] = []
        summary = (
            ("total_spending", result.total_spending), ("purchase_total", result.purchase_total),
            ("refund_total", result.refund_total), ("fee_total", result.fee_total),
            ("interest_total", result.interest_total), ("tax_total", result.tax_total),
            ("cash_advance_total", result.cash_advance_total),
        )
        rows.extend((result.period, "summary", name, str(amount), "") for name, amount in summary)
        rows.extend((result.period, "bank", name, str(amount), "") for name, amount in result.by_bank.items())
        rows.extend((result.period, "category", name, str(amount), "") for name, amount in result.by_category.items())
        rows.append((result.period, "count", "transactions", "", str(result.transaction_count)))
        rows.append((result.period, "count", "uncategorized", "", str(result.uncategorized_count)))
        rows.extend((result.period, "completeness", item.bank, "", item.status)
                    for item in result.statement_completeness)
        return rows

    def generate(self, month: str, output_dir: str | Path) -> ReportPaths:
        directory = Path(output_dir)
        if directory.suffix or directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("output directory must be a directory")
        directory.mkdir(parents=True, exist_ok=True)
        result = self.analysis.analyze(month)
        rows = self._rows(result)
        csv_path = directory / f"{result.period}.csv"
        html_path = directory / f"{result.period}.html"
        if csv_path.is_symlink() or html_path.is_symlink():
            raise ValueError("report target must not be a symlink")
        csv_buffer = io.StringIO(newline="")
        writer = csv.writer(csv_buffer, lineterminator="\n")
        writer.writerow(("month", "section", "name", "amount", "value"))
        writer.writerows(rows)
        self._write_atomic(csv_path, csv_buffer.getvalue())
        body = "\n".join(
            f"<tr><td>{html.escape(month)}</td><td>{html.escape(section)}</td>"
            f"<td>{html.escape(name)}</td><td>{html.escape(amount)}</td><td>{html.escape(value)}</td></tr>"
            for month, section, name, amount, value in rows
        )
        html_content = (
            "<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>Finance report "
            f"{html.escape(result.period)}</title></head><body><h1>Finance report {html.escape(result.period)}</h1>"
            "<table><thead><tr><th>Month</th><th>Section</th><th>Name</th><th>Amount</th><th>Value</th></tr></thead>"
            f"<tbody>{body}</tbody></table></body></html>\n"
        )
        self._write_atomic(html_path, html_content)
        return ReportPaths(csv_path, html_path)
