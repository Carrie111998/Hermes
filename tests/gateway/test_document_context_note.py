"""Tests for the document context note prepended to user turns with attachments.

A user who attaches a PDF / DOCX in chat used to see the agent treat it as
"unreadable" because the context note told the model to "Ask the user what
they'd like you to do with it" — steering it away from extracting the text it
is perfectly capable of reading. These tests pin the contract:

- text documents: note confirms the (adapter-)inlined content + records path.
- binary documents (PDF/DOCX/…): note tells the agent to extract the text
  itself and never tells it to punt back to the user.
"""

import importlib

import pytest

gateway_run = importlib.import_module("gateway.run")
_build_document_context_note = gateway_run._build_document_context_note


class TestTextDocumentNote:
    @pytest.mark.parametrize("mtype", ["text/plain", "text/markdown", "text/csv"])
    def test_text_note_mentions_included_content_and_path(self, mtype):
        note = _build_document_context_note("notes.txt", "/cache/doc_notes.txt", mtype)
        assert "text document" in note
        assert "notes.txt" in note
        assert "/cache/doc_notes.txt" in note
        assert "included below" in note


class TestBinaryDocumentNote:
    @pytest.mark.parametrize(
        "mtype",
        [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/octet-stream",
        ],
    )
    def test_binary_note_guides_extraction(self, mtype):
        note = _build_document_context_note("contract.pdf", "/cache/doc_contract.pdf", mtype)
        # Records the path so the agent can open it.
        assert "/cache/doc_contract.pdf" in note
        # Tells the agent to read it by extracting the text...
        assert "extract" in note.lower()
        # ...and does NOT steer it into punting back to the user (the bug).
        assert "ask the user" not in note.lower()
        assert "paste" in note.lower()

    def test_binary_note_distinct_from_text_note(self):
        text_note = _build_document_context_note("a.txt", "/c/a.txt", "text/plain")
        pdf_note = _build_document_context_note("a.pdf", "/c/a.pdf", "application/pdf")
        assert text_note != pdf_note
        # The text path claims content is inlined; the binary path must not.
        assert "included below" in text_note
        assert "included below" not in pdf_note


class TestProcessedDocumentNote:
    """When a readable form exists, the agent is sent there directly."""

    def test_note_points_at_the_readable_form(self):
        note = _build_document_context_note(
            "contract.pdf", "/cache/doc_contract.pdf", "application/pdf",
            "/cache/documents/pdoc_1/derived/content.md",
        )
        assert "/cache/documents/pdoc_1/derived/content.md" in note
        # The user's own filename is what the transcript still shows.
        assert "contract.pdf" in note
        # No self-extraction instructions once a readable form exists.
        assert "extract" not in note.lower()

    def test_note_never_names_the_machinery(self):
        note = _build_document_context_note(
            "contract.pdf", "/cache/doc_contract.pdf", "application/pdf",
            "/cache/documents/pdoc_1/derived/content.md",
        )
        for forbidden in ("anydoc", "converter", "conversion", "ocr", "markdown"):
            assert forbidden not in note.lower()

    def test_text_documents_ignore_a_sidecar(self):
        """Text is already inlined upstream; a sidecar would be a second copy."""
        note = _build_document_context_note(
            "notes.txt", "/cache/doc_notes.txt", "text/plain", "/cache/whatever.md"
        )
        assert "included below" in note
        assert "/cache/whatever.md" not in note


DOCX_FIXTURE = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "fixtures" / "documents" / "sample.docx"
)
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_cached_document_context_uses_processed_sidecar(tmp_path, monkeypatch):
    """End to end: cached bytes → sidecar → a note that carries the content.

    A DOCX, not a CSV: text/* attachments are inlined by the adapter upstream,
    so their note deliberately stays the text note. The sidecar exists to make
    the formats that *cannot* be inlined readable.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )
    import agent.document_artifacts as store_module
    from gateway.platforms.base import cache_media_bytes

    store_module.reset_store_cache()
    try:
        cached = cache_media_bytes(
            DOCX_FIXTURE.read_bytes(), filename="catalog.docx", mime_type=DOCX_MIME
        )
        assert cached is not None
        assert cached.processing_status == "ready"
        assert cached.processed_path and cached.processed_path.endswith(".md")
        # The original is untouched and still the user-facing file.
        assert cached.display_name == "catalog.docx"
        assert cached.path.endswith(".docx")
        assert cached.agent_path() == cached.processed_path

        from pathlib import Path

        text = Path(cached.processed_path).read_text(encoding="utf-8")
        assert "Widget" in text

        note = _build_document_context_note(
            cached.display_name, cached.path, cached.media_type, cached.processed_path
        )
        assert "binary" not in note.lower()
        assert cached.processed_path in note
    finally:
        store_module.reset_store_cache()


def test_text_attachment_gets_a_sidecar_but_keeps_the_text_note(tmp_path, monkeypatch):
    """A CSV is inlined upstream, so its note must not gain a second copy."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )
    import agent.document_artifacts as store_module
    from gateway.platforms.base import cache_media_bytes

    store_module.reset_store_cache()
    try:
        cached = cache_media_bytes(
            b"name\nWidget\n", filename="catalog.csv", mime_type="text/csv"
        )
        assert cached is not None and cached.processed_path
        note = _build_document_context_note(
            cached.display_name, cached.path, cached.media_type, cached.processed_path
        )
        assert "included below" in note
        assert cached.processed_path not in note
    finally:
        store_module.reset_store_cache()


def test_unprocessable_attachment_keeps_existing_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )
    import agent.document_artifacts as store_module
    from gateway.platforms.base import cache_media_bytes

    store_module.reset_store_cache()
    try:
        cached = cache_media_bytes(
            b"\x00\x01binary", filename="firmware.bin", mime_type="application/octet-stream"
        )
        assert cached is not None
        assert cached.processed_path is None
        assert cached.agent_path() == cached.path
    finally:
        store_module.reset_store_cache()
