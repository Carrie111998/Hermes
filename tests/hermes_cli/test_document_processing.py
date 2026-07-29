from __future__ import annotations

import json
from argparse import ArgumentParser
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_cli.document_processing import (
    build_source_registry,
    external_processing_gate,
    extract_sources,
    register_documents_parser,
    render_markdown_report,
    run_document_pilot,
)


def test_markdown_extraction_preserves_anchor_and_integrity_terms(tmp_path: Path):
    doc = tmp_path / "note.md"
    doc.write_text("# Title\n\nDe afspraak is op 2026-07-25. Pad: /tmp/source.md.\n", encoding="utf-8")

    registry = build_source_registry(
        [str(doc)], rights_status="allowed", sensitivity="public", routing_policy="external_allowed"
    )
    units = extract_sources(registry, chunk_chars=1000)

    assert registry[0].source_type == "markdown"
    assert registry[0].hash_or_snapshot
    assert units[0].location["line_start"] == 1
    assert units[0].location["line_end"] >= 3
    assert units[0].extraction_status == "ok"
    assert "2026-07-25" in units[0].integrity_terms
    assert "/tmp/source.md" in units[0].integrity_terms


def test_external_gate_fails_closed_by_default(tmp_path: Path):
    doc = tmp_path / "note.txt"
    doc.write_text("plain text", encoding="utf-8")
    registry = build_source_registry([str(doc)])

    allowed, blocks = external_processing_gate(registry)

    assert allowed is False
    assert blocks[0]["reason"] == "routing_policy=local_only"


def test_external_gate_allows_only_allowed_low_sensitivity_external(tmp_path: Path):
    doc = tmp_path / "note.txt"
    doc.write_text("plain text", encoding="utf-8")
    registry = build_source_registry(
        [str(doc)], rights_status="allowed", sensitivity="internal", routing_policy="external_allowed"
    )

    allowed, blocks = external_processing_gate(registry)

    assert allowed is True
    assert blocks == []


def test_manifest_overrides_source_metadata(tmp_path: Path):
    doc = tmp_path / "manual.html"
    doc.write_text("<h1>Manual</h1><script>noise</script><p>Stap 1: zet aan.</p>", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "path": str(doc),
                        "source_id": "manual",
                        "source_type": "html",
                        "rights_status": "allowed",
                        "sensitivity": "public",
                        "routing_policy": "external_allowed",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = run_document_pilot([], manifest_path=str(manifest))

    assert result["registry"][0]["source_id"] == "manual"
    assert result["external_gate"]["allowed"] is True
    assert "noise" not in result["units"][0]["text_preview"]
    assert "Stap 1" in result["units"][0]["text_preview"]


def test_auxiliary_summary_is_skipped_when_gate_blocks(tmp_path: Path):
    doc = tmp_path / "private.md"
    doc.write_text("private content", encoding="utf-8")

    with patch("agent.auxiliary_client.call_llm") as call_llm:
        result = run_document_pilot([str(doc)], use_auxiliary=True)

    call_llm.assert_not_called()
    assert result["auxiliary"]["status"] == "skipped"
    assert result["auxiliary"]["reason"] == "external gate blocked"


def test_auxiliary_summary_uses_document_task_after_gate(tmp_path: Path):
    doc = tmp_path / "public.md"
    doc.write_text("# Public\n\nRelease date 2026-07-25.", encoding="utf-8")
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = '{"source_summaries": []}'
    response.choices[0].message.reasoning = None
    response.choices[0].message.reasoning_content = None
    response.choices[0].message.reasoning_details = None

    with patch("agent.auxiliary_client.call_llm", return_value=response) as call_llm:
        result = run_document_pilot(
            [str(doc)],
            rights_status="allowed",
            sensitivity="public",
            routing_policy="external_allowed",
            use_auxiliary=True,
        )

    assert result["auxiliary"]["status"] == "ok"
    assert result["auxiliary"]["task"] == "document_summarization"
    assert call_llm.call_args.kwargs["task"] == "document_summarization"


def test_markdown_report_contains_gate_and_sources(tmp_path: Path):
    doc = tmp_path / "note.md"
    doc.write_text("# A\n", encoding="utf-8")
    result = run_document_pilot([str(doc)])

    md = render_markdown_report(result)

    assert "# Document processing pilot report" in md
    assert "Allowed: False" in md
    assert "routing_policy=local_only" in md
    assert "note" in md


def test_documents_parser_wires_summarize_command(tmp_path: Path):
    doc = tmp_path / "note.md"
    doc.write_text("# A\n", encoding="utf-8")
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd")
    register_documents_parser(subparsers)

    args = parser.parse_args(["documents", "summarize", str(doc), "--format", "json"])

    assert args.documents_command == "summarize"
    assert args.func(args) == 0



def test_docx_extraction_uses_stdlib_zip_xml_adapter(tmp_path: Path):
    import zipfile

    docx = tmp_path / "sample.docx"
    document_xml = """<?xml version='1.0' encoding='UTF-8'?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>Policy date 2026-07-25</w:t></w:r></w:p></w:body>
    </w:document>"""
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("word/document.xml", document_xml)

    registry = build_source_registry([str(docx)], rights_status="allowed", sensitivity="public", routing_policy="external_allowed")
    units = extract_sources(registry)

    assert registry[0].source_type == "docx"
    assert units[0].extractor == "stdlib-docx"
    assert "Policy date 2026-07-25" in units[0].text_preview
    assert "2026-07-25" in units[0].integrity_terms


def test_epub_extraction_preserves_html_text_and_chapter_context(tmp_path: Path):
    import zipfile

    epub = tmp_path / "book.epub"
    with zipfile.ZipFile(epub, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("OPS/chapter1.xhtml", "<html><body><h1>Intro</h1><p>Version 3.2 costs €12.</p></body></html>")

    registry = build_source_registry([str(epub)], rights_status="allowed", sensitivity="public", routing_policy="external_allowed")
    units = extract_sources(registry)

    assert registry[0].source_type == "epub"
    assert units[0].extractor == "stdlib-epub"
    assert units[0].location["chapter"] == "OPS/chapter1.xhtml"
    assert "Version 3.2 costs €12" in units[0].text_preview
    assert "€12" in units[0].integrity_terms


def test_xlsx_extraction_outputs_cell_anchors(tmp_path: Path):
    import zipfile

    xlsx = tmp_path / "sheet.xlsx"
    with zipfile.ZipFile(xlsx, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Total</t></si></sst>""")
        zf.writestr("xl/worksheets/sheet1.xml", """
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row></sheetData>
        </worksheet>""")

    registry = build_source_registry([str(xlsx)], rights_status="allowed", sensitivity="public", routing_policy="external_allowed")
    units = extract_sources(registry)

    assert registry[0].source_type == "xlsx"
    assert units[0].extractor == "stdlib-xlsx"
    assert units[0].location["sheet"] == "xl/worksheets/sheet1.xml"
    assert "A1=Total" in units[0].text_preview
    assert "B1=42" in units[0].text_preview


def test_integrity_check_flags_missing_exact_quotes(tmp_path: Path):
    doc = tmp_path / "source.md"
    doc.write_text("# Source\n\nCorrect quote 2026-07-25.\n", encoding="utf-8")
    result = run_document_pilot([str(doc)], rights_status="allowed", sensitivity="public", routing_policy="external_allowed")
    result["auxiliary"] = {
        "status": "ok",
        "result": {
            "material_claims": [
                {
                    "claim": "bad",
                    "source_id": result["registry"][0]["source_id"],
                    "unit_id": result["units"][0]["unit_id"],
                    "location": result["units"][0]["location"],
                    "exact_quote": "This quote is absent",
                }
            ]
        },
    }

    from hermes_cli.document_processing import deterministic_integrity_check

    check = deterministic_integrity_check(result)

    assert check["status"] == "failed"
    assert check["failed_claims"][0]["reason"] == "exact_quote_not_found_in_unit_text"


def test_merge_draft_uses_proposal_only_document_merge_task(tmp_path: Path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("# A\n\nClaim A 2026-07-25.", encoding="utf-8")
    b.write_text("# B\n\nClaim B /tmp/b.md.", encoding="utf-8")
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = '{"change_manifest": {"kept_claims": [], "changed_claims": [], "deleted_claims": [], "conflicts": [], "uncertainties": []}, "draft_markdown": "# Draft"}'
    response.choices[0].message.reasoning = None
    response.choices[0].message.reasoning_content = None
    response.choices[0].message.reasoning_details = None

    from hermes_cli.document_processing import run_document_pilot

    with patch("agent.auxiliary_client.call_llm", return_value=response) as call_llm:
        result = run_document_pilot(
            [str(a), str(b)],
            rights_status="allowed",
            sensitivity="public",
            routing_policy="external_allowed",
            use_auxiliary=True,
            auxiliary_task="document_merge_draft",
        )

    assert result["auxiliary"]["status"] == "ok"
    assert result["auxiliary"]["task"] == "document_merge_draft"
    assert result["write_boundary"]["source_artifacts_mutated"] is False
    assert result["write_boundary"]["output_is_proposal_only"] is True
    assert call_llm.call_args.kwargs["task"] == "document_merge_draft"


def test_documents_parser_wires_merge_and_integrity_commands(tmp_path: Path):
    doc = tmp_path / "note.md"
    doc.write_text("# A\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(run_document_pilot([str(doc)])), encoding="utf-8")
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd")
    register_documents_parser(subparsers)

    merge_args = parser.parse_args(["documents", "merge-draft", str(doc), "--format", "json"])
    integrity_args = parser.parse_args(["documents", "integrity-check", str(report), "--format", "json"])

    assert merge_args.documents_command == "merge-draft"
    assert integrity_args.documents_command == "integrity-check"



def test_document_auxiliary_tasks_are_registered_in_config_and_model_menu():
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.main import _all_aux_tasks

    keys = {task for task, _label, _description in _all_aux_tasks()}
    for task in [
        "document_summarization",
        "document_merge_draft",
        "document_integrity_check",
        "document_corpus_planner",
    ]:
        assert task in keys
        assert task in DEFAULT_CONFIG["auxiliary"]
        assert DEFAULT_CONFIG["auxiliary"][task]["provider"] == "auto"
        assert "api_key" in DEFAULT_CONFIG["auxiliary"][task]


def test_corpus_plan_is_metadata_only_and_does_not_extract_text(tmp_path: Path):
    doc = tmp_path / "private.md"
    doc.write_text("Secret text that must not be extracted for metadata planning.", encoding="utf-8")

    from hermes_cli.document_processing import run_corpus_plan

    result = run_corpus_plan([str(doc)], routing_policy="metadata_only")

    assert result["schema_version"] == "document-plan-v1"
    assert result["mode"] == "metadata_only"
    assert "units" not in result
    assert result["external_gate"]["allowed"] is True
    assert result["write_boundary"]["source_artifacts_mutated"] is False


def test_pptx_extraction_uses_stdlib_zip_xml_adapter(tmp_path: Path):
    import zipfile

    pptx = tmp_path / "slides.pptx"
    slide_xml = """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Launch URL https://example.com</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"""
    with zipfile.ZipFile(pptx, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", slide_xml)

    registry = build_source_registry([str(pptx)], rights_status="allowed", sensitivity="public", routing_policy="external_allowed")
    units = extract_sources(registry)

    assert registry[0].source_type == "pptx"
    assert units[0].extractor == "stdlib-pptx"
    assert units[0].location["slide"] == 1
    assert "Launch URL https://example.com" in units[0].text_preview
    assert "https://example.com" in units[0].integrity_terms



def test_integrity_check_can_call_auxiliary_document_integrity_task(tmp_path: Path):
    doc = tmp_path / "source.md"
    doc.write_text("# Source\n\nExact quote 2026-07-25.\n", encoding="utf-8")
    report = run_document_pilot([str(doc)], rights_status="allowed", sensitivity="public", routing_policy="external_allowed")
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = '{"reviewed_claims": []}'
    response.choices[0].message.reasoning = None
    response.choices[0].message.reasoning_content = None
    response.choices[0].message.reasoning_details = None

    from hermes_cli.document_processing import run_integrity_check_report

    with patch("agent.auxiliary_client.call_llm", return_value=response) as call_llm:
        result = run_integrity_check_report(report, use_auxiliary=True)

    assert result["deterministic"]["status"] == "ok"
    assert result["auxiliary"]["status"] == "ok"
    assert result["auxiliary"]["task"] == "document_integrity_check"
    assert call_llm.call_args.kwargs["task"] == "document_integrity_check"


def test_integrity_check_parser_accepts_use_auxiliary(tmp_path: Path):
    doc = tmp_path / "note.md"
    doc.write_text("# A\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(run_document_pilot([str(doc)])), encoding="utf-8")
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd")
    register_documents_parser(subparsers)

    args = parser.parse_args(["documents", "integrity-check", str(report), "--use-auxiliary", "--format", "json"])

    assert args.documents_command == "integrity-check"
    assert args.use_auxiliary is True
