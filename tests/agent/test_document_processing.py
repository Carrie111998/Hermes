"""Contract tests for the shared local document processor."""

from pathlib import Path

import pytest

from agent.document_processing import (
    DocumentProcessingResult,
    ProcessingDisposition,
    is_processable_document,
    process_document,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "documents"


def test_csv_uses_filename_hint(monkeypatch):
    monkeypatch.setattr(
        "agent.document_processing._anydoc_to_markdown",
        lambda data, fmt: "| a |\n|---|\n| 1 |",
    )
    result = process_document(data=b"a\n1\n", filename="sample.csv")
    assert result.disposition is ProcessingDisposition.CONVERTED
    assert result.markdown.startswith("| a |")
    assert result.source_format == "csv"


def test_empty_primary_output_requests_fallback(monkeypatch):
    monkeypatch.setattr(
        "agent.document_processing._anydoc_to_markdown", lambda data, fmt: "  "
    )
    monkeypatch.setattr(
        "agent.document_processing._advanced_pdf_markdown",
        lambda path: "# Scan\nRecovered",
    )
    result = process_document(data=b"%PDF-1.4", filename="scan.pdf")
    assert result.disposition is ProcessingDisposition.CONVERTED
    assert result.used_fallback is True


def test_missing_advanced_dependency_needs_attention(monkeypatch):
    monkeypatch.setattr(
        "agent.document_processing._anydoc_to_markdown",
        lambda data, fmt: (_ for _ in ()).throw(RuntimeError("unsupported")),
    )
    monkeypatch.setattr(
        "agent.document_processing._advanced_pdf_markdown",
        lambda path: (_ for _ in ()).throw(ModuleNotFoundError("marker")),
    )
    result = process_document(data=b"%PDF-1.4", filename="scan.pdf")
    assert result.disposition is ProcessingDisposition.NEEDS_ATTENTION
    assert result.reason_code == "advanced_processing_unavailable"


def test_exactly_one_input_source_is_required(tmp_path):
    with pytest.raises(ValueError):
        process_document()
    with pytest.raises(ValueError):
        process_document(path=tmp_path / "x.csv", data=b"a")


def test_readable_text_passes_through_without_conversion():
    result = process_document(data=b"# Title\n\nBody", filename="notes.md")
    assert result.disposition is ProcessingDisposition.PASSTHROUGH
    assert result.markdown == "# Title\n\nBody"
    assert result.source_format == "md"


def test_undecodable_readable_text_fails_stably():
    result = process_document(data=b"\xff\xfe\x00bad", filename="notes.txt")
    assert result.disposition is ProcessingDisposition.FAILED
    assert result.reason_code == "undecodable_text"


def test_unsupported_extension_is_not_processable():
    assert is_processable_document("photo.png") is False
    assert is_processable_document("archive.zip") is False
    assert is_processable_document("report.pdf") is True
    assert is_processable_document("sheet.xlsx") is True
    assert is_processable_document("notes.txt") is True
    assert is_processable_document("data", content_type="text/csv") is True


def test_encrypted_document_maps_to_stable_reason(monkeypatch):
    import anydoc

    def _raise(data, fmt):
        raise anydoc.EncryptedError("locked")

    monkeypatch.setattr("agent.document_processing._anydoc_to_markdown", _raise)
    result = process_document(data=b"%PDF-1.4", filename="locked.pdf", use_fallback=False)
    assert result.disposition is ProcessingDisposition.FAILED
    assert result.reason_code == "encrypted"


def test_diagnostic_never_leaks_document_bytes(monkeypatch):
    secret = "SUPER-SECRET-CONTENT"

    def _raise(data, fmt):
        raise RuntimeError(f"boom while reading {secret}")

    monkeypatch.setattr("agent.document_processing._anydoc_to_markdown", _raise)
    result = process_document(
        data=secret.encode(), filename="leak.docx", use_fallback=False
    )
    assert result.disposition is ProcessingDisposition.FAILED
    assert secret not in (result.diagnostic or "")


@pytest.mark.parametrize(
    "name,needle",
    [
        ("sample.csv", "Widget"),
        ("sample.docx", "Quarterly catalogue"),
        ("sample.pdf", "Terms and conditions"),
    ],
)
def test_real_anydoc_fixture_yields_meaningful_markdown(name, needle):
    result = process_document(path=FIXTURES / name)
    assert result.disposition is ProcessingDisposition.CONVERTED
    assert needle in result.markdown


def test_result_is_frozen():
    result = DocumentProcessingResult(ProcessingDisposition.FAILED)
    with pytest.raises(Exception):
        result.markdown = "nope"  # type: ignore[misc]
