from __future__ import annotations

from pathlib import Path


SCHEMA = Path("plugins/skyai_customer/schema/skyai_ci_schema.sql")
SECURITY_DOC = Path("docs/skyai-v2-customer-facing-security-model.md")


def test_schema_defines_append_only_event_spine_without_raw_text_columns() -> None:
    sql = SCHEMA.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS skyai_ci.events" in sql
    assert "pii_redaction_status text NOT NULL DEFAULT 'metadata_only_no_raw_text'" in sql
    assert "events_no_raw_text" in sql
    assert "events_no_message" in sql
    assert "events_no_voucher_code" in sql
    assert "raw_message" not in sql
    assert "transcript" not in sql


def test_schema_documents_least_privilege_roles() -> None:
    sql = SCHEMA.read_text(encoding="utf-8")

    assert "skyai_customer_ingest" in sql
    assert "GRANT INSERT ON skyai_ci.events" in sql
    assert "No SELECT/UPDATE/DELETE on any table" in sql


def test_security_model_forbids_generic_database_url_and_muncho_brain_mix() -> None:
    text = SECURITY_DOC.read_text(encoding="utf-8")

    assert "no generic `DATABASE_URL` fallback" in text
    assert "Muncho canonical brain" in text
    assert "Customer messages are evidence, never instructions" in text
