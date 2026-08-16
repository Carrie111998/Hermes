from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from app.database import initialize_database
from app.ingestion.service import IngestionService
from app.config import load_config
from app.gmail import GmailApiError, GmailAuthError, GmailAuthenticator, GmailClient, GmailConfigError, GmailSource
from app.analysis.service import AnalysisService
from app.reporting.service import ReportService
from app.models import IngestionStatus
from app.parsers.axess import AxessParser
from app.parsers.base import ParserRegistry
from app.parsers.enpara import EnparaParser
from app.parsers.isbank_maximum import IsbankMaximumParser
from app.parsers.utils import load_pdf_document

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "database" / "finance.duckdb"
SECRETS_DIR = ROOT / "secrets"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PARSER_REGISTRY = ParserRegistry([IsbankMaximumParser(), AxessParser(), EnparaParser()])


def ensure_local_dirs() -> None:
    for name in ("inbox", "archive", "failed", "raw", "extracted", "reports", "database", "logs"):
        (DATA_DIR / name).mkdir(parents=True, exist_ok=True)


def build_ingestion_service() -> IngestionService:
    ensure_local_dirs()
    return IngestionService(data_dir=DATA_DIR, db_path=DB_PATH, registry=PARSER_REGISTRY)


def init_command() -> int:
    ensure_local_dirs()
    db = initialize_database(DB_PATH)
    db.close()
    logger.info("Yerel veritabanı ve veri klasörleri hazırlandı")
    return 0


def parse_command(pdf_path: str) -> int:
    result = build_ingestion_service().process_file(pdf_path)
    _log_result(result)
    return 0 if result.status in {IngestionStatus.SUCCESS, IngestionStatus.SUCCESS_WITH_WARNINGS, IngestionStatus.SKIPPED_DUPLICATE} else 1


def ingest_command(directory: str | None = None) -> int:
    service = build_ingestion_service()
    results = service.process_directory(directory)
    logger.info("Files found: %d", len(results))
    for result in results:
        _log_result(result)
    counts = {status: sum(result.status is status for result in results) for status in IngestionStatus}
    logger.info("Summary")
    logger.info("Processed: %d", len(results))
    logger.info("Success: %d", counts[IngestionStatus.SUCCESS])
    logger.info("Warnings: %d", counts[IngestionStatus.SUCCESS_WITH_WARNINGS])
    logger.info("Duplicate: %d", counts[IngestionStatus.SKIPPED_DUPLICATE])
    logger.info("Failed: %d", counts[IngestionStatus.FAILED] + counts[IngestionStatus.FORMAT_ERROR] + counts[IngestionStatus.UNSUPPORTED])
    return 0 if not any(result.status in {IngestionStatus.FAILED, IngestionStatus.FORMAT_ERROR} for result in results) else 1


def gmail_auth_command() -> int:
    GmailAuthenticator(SECRETS_DIR / "gmail_credentials.json", SECRETS_DIR / "gmail_token.json").authorize()
    logger.info("Gmail authorization successful")
    return 0


def gmail_sync_command(*, since: str | None, month: str | None, bank: str | None, dry_run: bool) -> int:
    if since and month:
        raise ValueError("Use either --since or --month, not both")
    end = date.today()
    if month:
        start = date.fromisoformat(f"{month}-01")
        end = (date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1) - timedelta(days=1))
    elif since:
        start = date.fromisoformat(since)
    else:
        start = end - timedelta(days=90)
    config = load_config(ROOT / "config")
    credentials = GmailAuthenticator(SECRETS_DIR / "gmail_credentials.json", SECRETS_DIR / "gmail_token.json").authorize()
    source = GmailSource(client=GmailClient.from_credentials(credentials), ingestion_service=build_ingestion_service(), config=config)
    summary = source.sync(since=start, until=end, bank_id=bank, dry_run=dry_run)
    logger.info("Gmail sync%s", " (dry-run)" if dry_run else "")
    for item in summary.banks:
        logger.info("%s [%s]", item.bank, item.status)
        logger.info("  messages found: %d", item.messages_found)
        logger.info("  PDF attachments: %d", item.pdf_attachments)
        if dry_run:
            logger.info("  would process: %d", item.pdf_attachments)
        else:
            for result in item.results or []:
                logger.info("  result: %s", result.status.value)
    logger.info("Summary")
    logger.info("Messages checked: %d", summary.messages_checked)
    logger.info("PDF attachments: %d", summary.pdf_attachments)
    logger.info("Imported: %d", summary.imported)
    logger.info("Duplicates: %d", summary.duplicates)
    logger.info("Failed: %d", summary.failed)
    return 0 if summary.failed == 0 else 1


def _log_result(result) -> None:
    logger.info("%s", result.status.value)
    if result.bank:
        logger.info("  bank: %s", result.bank)
    if result.status in {IngestionStatus.UNSUPPORTED, IngestionStatus.FORMAT_ERROR, IngestionStatus.FAILED}:
        logger.info("  file: %s", result.safe_name)
    logger.info("  transactions: %d", result.transactions_found)
    logger.info("  inserted: %d", result.transactions_inserted)
    logger.info("  duplicates: %d", result.duplicates_skipped)
    if result.warnings:
        logger.info("  warnings: %d", len(result.warnings))
    if result.errors:
        logger.info("  errors: %d", len(result.errors))


def inspect_pdf_command(pdf_path: str) -> int:
    document = load_pdf_document(pdf_path)
    parser = PARSER_REGISTRY.find(document)
    logger.info("Page text loaded: %s characters", len(document.text))
    logger.info("Parser detection: %s", parser.bank_id if parser else "none")
    if parser:
        metadata = parser.parse_metadata(document)
        transactions = parser.parse_transactions(document, metadata)
        logger.info("Transaction table: detected (%d rows)", len(transactions))
    return 0


def analyze_command(month: str, as_json: bool) -> int:
    service = AnalysisService.from_path(DB_PATH, ROOT / "config")
    analysis = service.analyze(month)
    if as_json:
        print(json.dumps({"analysis": analysis.to_public_dict(), "audit": service.audit()}, ensure_ascii=False, indent=2))
        return 0
    logger.info("Month: %s", analysis.period)
    logger.info("Total spending: %s", analysis.total_spending)
    logger.info("Refunds: %s", analysis.refund_total)
    logger.info("Bank fees: %s", analysis.fee_total)
    logger.info("Interest: %s", analysis.interest_total)
    logger.info("Tax: %s", analysis.tax_total)
    logger.info("By bank: %s", {key: str(value) for key, value in analysis.by_bank.items()})
    logger.info("By category: %s", {key: str(value) for key, value in analysis.by_category.items()})
    logger.info("Uncategorized: %d", analysis.uncategorized_count)
    logger.info("Statement completeness: %s", {item.bank: item.status for item in analysis.statement_completeness})
    audit = service.audit()
    logger.info("Audit transaction types: %s", audit["transaction_type_counts"])
    logger.info("Audit categorized: %d", audit["categorized_count"])
    logger.info("Audit uncategorized: %d", audit["uncategorized_count"])
    logger.info("Audit known merchant rules: %d", audit["known_merchant_rule_count"])
    return 0


def report_command(month: str, output_dir: str) -> int:
    paths = ReportService(AnalysisService.from_path(DB_PATH, ROOT / "config")).generate(month, output_dir)
    logger.info("CSV report: %s", paths.csv_path)
    logger.info("HTML report: %s", paths.html_path)
    return 0


def dashboard_command() -> int:
    return subprocess.run([sys.executable, "-m", "streamlit", "run", str(ROOT / "app" / "dashboard.py")], check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local finance statement processing")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    parse = subparsers.add_parser("parse")
    parse.add_argument("pdf_path")
    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("directory", nargs="?")
    subparsers.add_parser("gmail-auth")
    gmail = subparsers.add_parser("gmail-sync")
    scope = gmail.add_mutually_exclusive_group()
    scope.add_argument("--since")
    scope.add_argument("--month", help="YYYY-MM")
    gmail.add_argument("--bank", choices=("isbank_maximum", "axess", "enpara"))
    gmail.add_argument("--dry-run", action="store_true")
    inspect = subparsers.add_parser("inspect-pdf")
    inspect.add_argument("pdf_path")
    analyze = subparsers.add_parser("analyze")
    analyze.add_argument("--month", required=True, help="YYYY-MM")
    analyze.add_argument("--json", action="store_true", dest="as_json")
    subparsers.add_parser("sync")
    report = subparsers.add_parser("report")
    report.add_argument("--month", required=True, help="YYYY-MM")
    report.add_argument("--output-dir", required=True)
    subparsers.add_parser("dashboard")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "init":
        return init_command()
    if args.command == "parse":
        return parse_command(args.pdf_path)
    if args.command == "ingest":
        return ingest_command(args.directory)
    if args.command == "gmail-auth":
        try:
            return gmail_auth_command()
        except GmailAuthError as exc:
            logger.error("Gmail authorization failed: %s", exc)
            return 1
    if args.command == "gmail-sync":
        try:
            return gmail_sync_command(since=args.since, month=args.month, bank=args.bank, dry_run=args.dry_run)
        except (GmailApiError, GmailAuthError, GmailConfigError, ValueError) as exc:
            logger.error("Gmail sync failed: %s", exc)
            return 1
    if args.command == "inspect-pdf":
        return inspect_pdf_command(args.pdf_path)
    if args.command == "analyze":
        try:
            return analyze_command(args.month, args.as_json)
        except (ValueError, OSError) as exc:
            logger.error("Analysis failed: %s", exc)
            return 1
    if args.command == "report":
        try:
            return report_command(args.month, args.output_dir)
        except (ValueError, OSError) as exc:
            logger.error("Report failed: %s", exc)
            return 1
    if args.command == "dashboard":
        return dashboard_command()
    logger.info("%s komutu sonraki faz için ayrılmıştır", args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
