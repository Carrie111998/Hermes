"""Redaction and provenance contracts for agent run evidence."""

import pytest

from server.agent_evidence import (
    REDACTED,
    evidence_from_log,
    evidence_from_output,
    redact_evidence,
)
from server.agent_service import AgentRunService, StubRunExecutor
from server.db import Database, json_dump, new_id, now


def test_evidence_redacts_credentials_and_keeps_source_result():
    raw = {
        "url": "https://example.test/report",
        "authorization": "Bearer secret",
        "result": {"name": "Widget"},
    }
    safe = redact_evidence(raw)
    assert safe["url"] == "https://example.test/report"
    assert safe["authorization"] == REDACTED
    assert safe["result"] == {"name": "Widget"}


@pytest.mark.parametrize(
    "key",
    ["Authorization", "cookie", "api_key", "X-Api-Key", "refresh_token",
     "password", "client_secret", "private_key", "signature"],
)
def test_every_credential_shaped_key_is_redacted(key):
    assert redact_evidence({key: "sensitive"})[key] == REDACTED


def test_prompt_and_tool_internals_are_omitted_not_stored():
    safe = redact_evidence({
        "system_prompt": "You are...",
        "tool_args": {"query": "x"},
        "messages": [{"role": "user"}],
        "url": "https://example.test",
    })
    assert safe["system_prompt"] == "[OMITTED]"
    assert safe["tool_args"] == "[OMITTED]"
    assert safe["messages"] == "[OMITTED]"
    assert safe["url"] == "https://example.test"


def test_redaction_reaches_nested_values():
    safe = redact_evidence({"outer": {"inner": [{"token": "abc", "ok": 1}]}})
    assert safe["outer"]["inner"][0]["token"] == REDACTED
    assert safe["outer"]["inner"][0]["ok"] == 1


def test_oversized_values_are_bounded():
    safe = redact_evidence({"body": "x" * 50_000, "items": list(range(500))})
    assert len(safe["body"]) < 5_000
    assert len(safe["items"]) <= 51


def test_deeply_nested_structures_terminate():
    node = {"a": 1}
    for _ in range(50):
        node = {"child": node}
    assert "[TRUNCATED]" in str(redact_evidence(node))


def test_output_sources_are_extracted_and_deduplicated():
    output = {
        "records": [
            {"title": "Report", "url": "https://example.test/report", "result": {"n": 1}},
            {"title": "Report again", "url": "https://example.test/report"},
            {"title": "Catalog", "file": "catalog.md"},
        ],
        "rejects": [],
    }
    evidence = evidence_from_output(output)
    keys = {(item.source_type, item.source_url, item.file_reference) for item in evidence}
    assert ("web", "https://example.test/report", "") in keys
    assert ("file", "", "catalog.md") in keys
    assert len(evidence) == 2, "the repeated URL must appear once"


def test_output_extraction_ignores_non_source_urls():
    """A URL inside prose is not provenance and must not become evidence."""
    output = {"records": [{"description": "see https://example.test/blog for details"}]}
    assert evidence_from_output(output) == []


def test_log_urls_become_evidence():
    evidence = evidence_from_log("fetched https://example.test/a and https://example.test/b")
    assert [item.source_url for item in evidence] == [
        "https://example.test/a", "https://example.test/b"
    ]


def test_log_lines_carrying_credentials_are_dropped_entirely():
    for line in (
        "Authorization: Bearer abc https://example.test/x",
        "curl https://example.test?api_key=abc",
        "using token=secret at https://example.test",
    ):
        assert evidence_from_log(line) == []


def test_log_evidence_never_keeps_the_line_itself():
    evidence = evidence_from_log("visiting https://example.test/a with debug output")
    assert evidence
    assert all("debug output" not in str(item.metadata) for item in evidence)


# ── persistence ────────────────────────────────────────────────────────────


@pytest.fixture()
def runs(tmp_path):
    db = Database(tmp_path / "interfaze.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,legal_name,status,data,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        ("cmp_1", "Acme", "Acme", "active", "{}", stamp, stamp),
    )
    service = AgentRunService(db, StubRunExecutor())
    yield service
    service.pool.shutdown(wait=False, cancel_futures=True)


def completed_run_with_output_and_source(runs, company_id):
    run = runs.create(company_id, "document_processing", {"document_id": "doc_1"})
    runs.db.execute(
        "UPDATE agent_runs SET status='succeeded',output=? WHERE id=?",
        (json_dump({"records": [{"name": "Widget"}], "rejects": []}), run["id"]),
    )
    runs.event(run["id"], company_id, "started")
    runs.record_evidence(
        company_id, run["id"],
        evidence_from_output({"records": [{
            "url": "https://example.test/report",
            "authorization": "Bearer secret",
            "result": {"name": "Widget"},
        }]}),
    )
    return runs.get(company_id, run["id"])


def test_run_detail_includes_output_events_and_evidence(runs):
    run = completed_run_with_output_and_source(runs, "cmp_1")
    detail = runs.detail("cmp_1", run["id"])
    assert detail["output"]
    assert detail["events"]
    assert detail["evidence"][0]["source_url"] == "https://example.test/report"


def test_persisted_evidence_carries_no_credentials(runs):
    run = completed_run_with_output_and_source(runs, "cmp_1")
    serialized = str(runs.detail("cmp_1", run["id"])["evidence"])
    assert "Bearer secret" not in serialized
    assert REDACTED in serialized


def test_detail_exposes_related_entities_from_the_payload(runs):
    run = runs.create("cmp_1", "document_processing",
                      {"document_id": "doc_1", "source_document_id": "doc_1"})
    detail = runs.detail("cmp_1", run["id"])
    assert detail["related"] == {"document_id": "doc_1", "source_document_id": "doc_1"}


def test_recording_the_same_source_twice_stores_one_row(runs):
    run = runs.create("cmp_1", "document_processing", {"document_id": "doc_1"})
    items = evidence_from_log("visited https://example.test/a")
    assert runs.record_evidence("cmp_1", run["id"], items) == 1
    assert runs.record_evidence("cmp_1", run["id"], items) == 0
    assert len(runs.evidence("cmp_1", run["id"])) == 1


def test_recording_evidence_never_raises(runs):
    """An evidence failure must not be able to abort the run that produced it."""
    assert runs.record_evidence("cmp_1", "run_that_does_not_exist",
                                evidence_from_log("see https://example.test/a")) == 0


def test_detail_is_tenant_scoped(runs):
    stamp = now()
    runs.db.execute(
        "INSERT INTO companies(id,name,legal_name,status,data,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        ("cmp_2", "Other", "Other", "active", "{}", stamp, stamp),
    )
    run = runs.create("cmp_1", "document_processing", {"document_id": "doc_1"})
    with pytest.raises(Exception):
        runs.detail("cmp_2", run["id"])
