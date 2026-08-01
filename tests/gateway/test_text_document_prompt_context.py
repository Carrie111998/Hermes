from gateway.run import (
    _MAX_INLINE_TEXT_DOCUMENT_BYTES,
    _build_document_prompt_context,
    _is_text_document,
)


def _context(path, *, name="sample.csv", mime="text/csv", existing_text=""):
    return _build_document_prompt_context(
        host_path=str(path),
        display_name=name,
        agent_path=f"/root/.hermes/cache/documents/{name}",
        mtype=mime,
        existing_text=existing_text,
    )


def test_csv_content_is_inlined_from_cached_file(tmp_path):
    document = tmp_path / "sample.csv"
    document.write_text("name,value\nalice,42\n", encoding="utf-8")

    context = _context(document)

    assert "Its content has been included below" in context
    assert "[Content of sample.csv]:" in context
    assert "name,value\nalice,42" in context


def test_application_json_is_treated_as_text(tmp_path):
    document = tmp_path / "sample.json"
    document.write_text('{"ok": true}', encoding="utf-8")

    assert _is_text_document(str(document), "application/json") is True
    assert '{"ok": true}' in _context(
        document,
        name="sample.json",
        mime="application/json",
    )


def test_utf8_bom_is_removed_before_inlining(tmp_path):
    document = tmp_path / "sample.csv"
    document.write_bytes(b"\xef\xbb\xbfcolumn\nvalue\n")

    context = _context(document)

    assert "\ufeff" not in context
    assert "column\nvalue" in context


def test_adapter_injected_content_is_not_duplicated(tmp_path):
    document = tmp_path / "sample.csv"
    document.write_text("name,value\nalice,42\n", encoding="utf-8")
    existing = "[Content of sample.csv]:\nname,value\nalice,42"

    context = _context(document, existing_text=existing)

    assert "Its content has been included below" in context
    assert "[Content of sample.csv]:" not in context
    assert "name,value" not in context


def test_oversized_text_document_uses_truthful_path_only_note(tmp_path):
    document = tmp_path / "sample.csv"
    document.write_bytes(b"x" * (_MAX_INLINE_TEXT_DOCUMENT_BYTES + 1))

    context = _context(document)

    assert "Its content was not inlined" in context
    assert "Its content has been included below" not in context
    assert "[Content of sample.csv]:" not in context
    assert "/root/.hermes/cache/documents/sample.csv" in context


def test_non_utf8_text_document_uses_truthful_path_only_note(tmp_path):
    document = tmp_path / "sample.csv"
    document.write_bytes(b"\xff\xfe\x00\x01")

    context = _context(document)

    assert "Its content was not inlined" in context
    assert "Its content has been included below" not in context


def test_binary_document_keeps_extraction_guidance(tmp_path):
    document = tmp_path / "sample.pdf"
    document.write_bytes(b"%PDF-1.7\n")

    context = _context(document, name="sample.pdf", mime="application/pdf")

    assert "Its text is not inlined here" in context
    assert "extract the document's text yourself" in context
    assert "[Content of sample.pdf]:" not in context
