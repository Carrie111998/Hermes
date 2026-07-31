#!/usr/bin/env python3
"""
Tests for structured-document extraction in the read_file tool.

Covers .ipynb / .docx / .xlsx extraction (ported from Kilo-Org/kilocode
#10733, #10737, #10740) and the read_file_tool integration: pagination,
line-numbering, graceful fallback on malformed input, and hidden-sheet
omission.

Run with:  python -m pytest tests/tools/test_read_extract.py -v
"""

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.read_extract import (
    ExtractionError,
    extract_document_text,
    is_extractable_document,
)
from tools.file_tools import read_file_tool


# ---------------------------------------------------------------------------
# Fixture builders — construct minimal valid OOXML / notebook files.
# ---------------------------------------------------------------------------

def _write_notebook(path, cells, nbformat=4):
    nb = {"cells": cells, "metadata": {}, "nbformat": nbformat, "nbformat_minor": 5}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(nb, fh)


def _write_docx(path, document_xml):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", document_xml)


def _write_xlsx(path, *, workbook, rels, shared, sheets):
    """sheets: dict of part-name -> xml string."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        if shared is not None:
            z.writestr("xl/sharedStrings.xml", shared)
        for part, xml in sheets.items():
            z.writestr(part, xml)


_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


# ---------------------------------------------------------------------------
# is_extractable_document
# ---------------------------------------------------------------------------

class TestIsExtractable(unittest.TestCase):
    def test_recognized_extensions(self):
        self.assertTrue(is_extractable_document("a.ipynb"))
        self.assertTrue(is_extractable_document("/x/B.DOCX"))
        self.assertTrue(is_extractable_document("report.xlsx"))

    def test_unrecognized_extensions(self):
        self.assertFalse(is_extractable_document("a.py"))
        self.assertFalse(is_extractable_document("a.pdf"))
        self.assertFalse(is_extractable_document("a.txt"))


# ---------------------------------------------------------------------------
# Notebooks (.ipynb) — #10733
# ---------------------------------------------------------------------------

class TestNotebookExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_nb_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_markdown_and_code_in_order(self):
        p = os.path.join(self.tmp, "nb.ipynb")
        _write_notebook(p, [
            {"cell_type": "markdown", "source": ["# Title\n", "para"]},
            {"cell_type": "code", "source": "x = 1\nprint(x)",
             "outputs": [{"output_type": "stream", "text": ["1\n"]}],
             "execution_count": 1},
        ])
        text = extract_document_text(p)
        self.assertIn("# Title", text)
        self.assertIn("print(x)", text)
        # Output payloads must NOT leak into the extracted text.
        self.assertNotIn("output_type", text)
        self.assertNotIn("execution_count", text)
        # Order preserved: markdown before code.
        self.assertLess(text.index("Title"), text.index("print(x)"))


    def test_empty_cells_raises(self):
        p = os.path.join(self.tmp, "empty.ipynb")
        _write_notebook(p, [])
        with self.assertRaises(ExtractionError):
            extract_document_text(p)


# ---------------------------------------------------------------------------
# Word documents (.docx) — #10737
# ---------------------------------------------------------------------------

class TestDocxExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_docx_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _doc(self, body):
        return (f'<?xml version="1.0"?><w:document xmlns:w="{_NS_W}">'
                f'<w:body>{body}</w:body></w:document>')

    def test_paragraphs_and_runs(self):
        p = os.path.join(self.tmp, "d.docx")
        _write_docx(p, self._doc(
            '<w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>World</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Second</w:t></w:r></w:p>'))
        text = extract_document_text(p)
        self.assertIn("Hello World", text)
        self.assertIn("Second", text)


    def test_missing_document_xml_raises(self):
        p = os.path.join(self.tmp, "nodoc.docx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("other.xml", "<x/>")
        with self.assertRaises(ExtractionError):
            extract_document_text(p)

    def test_rejects_high_ratio_document_xml_before_decompression(self):
        p = os.path.join(self.tmp, "bomb.docx")
        bomb = self._doc(
            '<w:p><w:r><w:t>' + ("A" * (2 * 1024 * 1024)) + "</w:t></w:r></w:p>"
        )
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", bomb)

        with self.assertRaisesRegex(ExtractionError, "compression ratio"):
            extract_document_text(p)


    def test_rejects_oversized_document_xml_before_decompression(self):
        p = os.path.join(self.tmp, "oversized.docx")
        bomb = self._doc(
            '<w:p><w:r><w:t>' + ("A" * (17 * 1024 * 1024)) + "</w:t></w:r></w:p>"
        )
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as z:
            z.writestr("word/document.xml", bomb)

        with self.assertRaisesRegex(ExtractionError, "decompression limit"):
            extract_document_text(p)


# ---------------------------------------------------------------------------
# Excel workbooks (.xlsx) — #10740
# ---------------------------------------------------------------------------

class TestXlsxExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_xlsx_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, path, *, include_hidden=True):
        r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        hidden_sheet = (f'<sheet name="Hidden" sheetId="2" state="hidden" '
                        f'xmlns:r="{r}" r:id="rId2"/>') if include_hidden else ""
        workbook = (
            f'<workbook xmlns="{_NS_S}" xmlns:r="{r}"><sheets>'
            f'<sheet name="Data" sheetId="1" r:id="rId1"/>{hidden_sheet}'
            f'</sheets></workbook>')
        rels = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml" Type="x"/>'
            '</Relationships>')
        shared = (f'<sst xmlns="{_NS_S}"><si><t>Name</t></si><si><t>Score</t></si>'
                  f'<si><t>Alice</t></si></sst>')
        sheet1 = (
            f'<worksheet xmlns="{_NS_S}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>95</v></c></row>'
            '</sheetData></worksheet>')
        sheet2 = (f'<worksheet xmlns="{_NS_S}"><sheetData>'
                  '<row r="1"><c r="A1" t="str"><v>SECRETDATA</v></c></row>'
                  '</sheetData></worksheet>')
        _write_xlsx(path, workbook=workbook, rels=rels, shared=shared,
                    sheets={"xl/worksheets/sheet1.xml": sheet1,
                            "xl/worksheets/sheet2.xml": sheet2})

    def test_visible_sheet_content(self):
        p = os.path.join(self.tmp, "wb.xlsx")
        self._build(p)
        text = extract_document_text(p)
        self.assertIn("Data", text)        # sheet label
        self.assertIn("Name\tScore", text)  # shared-string header row
        self.assertIn("Alice\t95", text)    # string + numeric cells


    def test_not_a_zip_raises(self):
        p = os.path.join(self.tmp, "bad.xlsx")
        with open(p, "wb") as fh:
            fh.write(b"nope")
        with self.assertRaises(ExtractionError):
            extract_document_text(p)

    def test_rejects_high_ratio_sheet_xml_before_decompression(self):
        p = os.path.join(self.tmp, "bomb.xlsx")
        office_relationships = (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        )
        package_relationships = (
            "http://schemas.openxmlformats.org/package/2006/relationships"
        )
        workbook = (
            f'<workbook xmlns="{_NS_S}" xmlns:r="{office_relationships}"><sheets>'
            '<sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'
        )
        rels = (
            f'<Relationships xmlns="{package_relationships}">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/>'
            '</Relationships>'
        )
        sheet = (
            f'<worksheet xmlns="{_NS_S}"><sheetData><row><c t="str"><v>'
            + ("A" * (2 * 1024 * 1024))
            + "</v></c></row></sheetData></worksheet>"
        )
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/workbook.xml", workbook)
            z.writestr("xl/_rels/workbook.xml.rels", rels)
            z.writestr("xl/worksheets/sheet1.xml", sheet)

        with self.assertRaisesRegex(ExtractionError, "compression ratio"):
            extract_document_text(p)


    def test_rejects_cumulative_sheet_xml_before_third_member_read(self):
        p = os.path.join(self.tmp, "cumulative.xlsx")
        office_relationships = (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        )
        package_relationships = (
            "http://schemas.openxmlformats.org/package/2006/relationships"
        )
        workbook = (
            f'<workbook xmlns="{_NS_S}" xmlns:r="{office_relationships}"><sheets>'
            + "".join(
                f'<sheet name="Data{index}" sheetId="{index}" r:id="rId{index}"/>'
                for index in range(1, 4)
            )
            + "</sheets></workbook>"
        )
        rels = (
            f'<Relationships xmlns="{package_relationships}">'
            + "".join(
                f'<Relationship Id="rId{index}" Target="worksheets/sheet{index}.xml" Type="x"/>'
                for index in range(1, 4)
            )
            + "</Relationships>"
        )
        sheet = (
            f'<worksheet xmlns="{_NS_S}"><sheetData><row><c t="str"><v>'
            + ("A" * (11 * 1024 * 1024))
            + "</v></c></row></sheetData></worksheet>"
        )
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as z:
            z.writestr("xl/workbook.xml", workbook)
            z.writestr("xl/_rels/workbook.xml.rels", rels)
            for index in range(1, 4):
                z.writestr(f"xl/worksheets/sheet{index}.xml", sheet)

        with self.assertRaisesRegex(ExtractionError, "cumulative decompression limit"):
            extract_document_text(p)


# ---------------------------------------------------------------------------
# read_file_tool integration
# ---------------------------------------------------------------------------

class TestReadFileToolIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_int_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_notebook_read_is_line_numbered(self):
        p = os.path.join(self.tmp, "nb.ipynb")
        _write_notebook(p, [
            {"cell_type": "markdown", "source": "# H"},
            {"cell_type": "code", "source": "print(1)"},
        ])
        res = json.loads(read_file_tool(p))
        self.assertTrue(res.get("extracted_document"))
        self.assertIn("1|", res["content"])  # line-number gutter
        self.assertIn("print(1)", res["content"])


    def test_corrupt_docx_falls_through_to_binary_guard(self):
        p = os.path.join(self.tmp, "bad.docx")
        with open(p, "wb") as fh:
            fh.write(b"not a zip")
        res = json.loads(read_file_tool(p))
        # Should NOT crash; falls through to the binary-extension guard.
        self.assertIn("error", res)
        self.assertIn("binary", res["error"].lower())

    def test_docx_read_extracts(self):
        p = os.path.join(self.tmp, "d.docx")
        _write_docx(p, (f'<?xml version="1.0"?><w:document xmlns:w="{_NS_W}">'
                        '<w:body><w:p><w:r><w:t>Report body</w:t></w:r></w:p>'
                        '</w:body></w:document>'))
        res = json.loads(read_file_tool(p))
        self.assertTrue(res.get("extracted_document"))
        self.assertIn("Report body", res["content"])


class _WorkspaceOnlyFileOps:
    def __init__(self, allowed):
        self.env = SimpleNamespace(_workspace_only=True)
        self.allowed = allowed

    def read_bytes(self, path, max_bytes):
        if path not in self.allowed:
            raise OSError("outside workspace")
        content = self.allowed[path]
        if len(content) > max_bytes:
            raise OSError("document too large")
        return content

    @staticmethod
    def _add_line_numbers(content, start_line=1):
        return "\n".join(
            f"{line_number}|{line}"
            for line_number, line in enumerate(content.split("\n"), start=start_line)
        )


class TestWorkspaceOnlyStructuredReadBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rex_boundary_")
        self.paths = {}
        notebook = os.path.join(self.tmp, "foreign.ipynb")
        _write_notebook(notebook, [{"cell_type": "markdown", "source": "FOREIGN-NOTEBOOK"}])
        self.paths[".ipynb"] = notebook

        docx = os.path.join(self.tmp, "foreign.docx")
        _write_docx(
            docx,
            f'<w:document xmlns:w="{_NS_W}"><w:body><w:p><w:r>'
            '<w:t>FOREIGN-DOCX</w:t></w:r></w:p></w:body></w:document>',
        )
        self.paths[".docx"] = docx

        xlsx = os.path.join(self.tmp, "foreign.xlsx")
        relationships = "http://schemas.openxmlformats.org/package/2006/relationships"
        office_relationships = (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        )
        _write_xlsx(
            xlsx,
            workbook=(
                f'<workbook xmlns="{_NS_S}" xmlns:r="{office_relationships}"><sheets>'
                '<sheet name="Data" sheetId="1" r:id="rId1"/>'
                '</sheets></workbook>'
            ),
            rels=(
                f'<Relationships xmlns="{relationships}">'
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/>'
                '</Relationships>'
            ),
            shared=None,
            sheets={
                "xl/worksheets/sheet1.xml": (
                    f'<worksheet xmlns="{_NS_S}"><sheetData><row r="1">'
                    '<c r="A1" t="str"><v>FOREIGN-XLSX</v></c>'
                    '</row></sheetData></worksheet>'
                )
            },
        )
        self.paths[".xlsx"] = xlsx

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_internal_structured_documents_are_read_through_selected_environment(self):
        for extension, host_path in self.paths.items():
            with self.subTest(extension=extension):
                container_path = f"/workspace/internal{extension}"
                file_ops = _WorkspaceOnlyFileOps(
                    {container_path: Path(host_path).read_bytes()}
                )
                with patch("tools.file_tools._get_file_ops", return_value=file_ops):
                    result = json.loads(
                        read_file_tool(container_path, task_id=f"internal-{extension}")
                    )
                self.assertTrue(result.get("extracted_document"), result)
                self.assertIn("FOREIGN-", result["content"])

    def test_foreign_absolute_and_symlinked_structured_documents_are_refused(self):
        file_ops = _WorkspaceOnlyFileOps({})
        for extension, foreign_path in self.paths.items():
            symlink_path = os.path.join(self.tmp, f"link{extension}")
            os.symlink(foreign_path, symlink_path)
            for candidate in (foreign_path, symlink_path):
                with self.subTest(extension=extension, candidate=candidate):
                    with patch("tools.file_tools._get_file_ops", return_value=file_ops):
                        result = json.loads(
                            read_file_tool(candidate, task_id=f"foreign-{extension}")
                        )
                    self.assertIn("error", result)
                    self.assertNotIn("FOREIGN-", result.get("content", ""))

    def test_zip_bomb_rejection_removes_workspace_bridge_temp_file(self):
        container_path = "/workspace/bomb.docx"
        host_bomb = os.path.join(self.tmp, "bomb.docx")
        document = (
            f'<w:document xmlns:w="{_NS_W}"><w:body><w:p><w:r><w:t>'
            + ("A" * (2 * 1024 * 1024))
            + "</w:t></w:r></w:p></w:body></w:document>"
        )
        with zipfile.ZipFile(host_bomb, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", document)
        file_ops = _WorkspaceOnlyFileOps(
            {container_path: Path(host_bomb).read_bytes()}
        )
        real_named_temporary_file = tempfile.NamedTemporaryFile

        def local_temporary_file(*args, **kwargs):
            return real_named_temporary_file(*args, dir=self.tmp, **kwargs)

        before = set(os.listdir(self.tmp))
        with patch("tools.file_tools._get_file_ops", return_value=file_ops), patch(
            "tools.file_tools.tempfile.NamedTemporaryFile",
            side_effect=local_temporary_file,
        ):
            result = json.loads(read_file_tool(container_path, task_id="bomb-cleanup"))

        self.assertIn("error", result)
        self.assertEqual(set(os.listdir(self.tmp)), before)


if __name__ == "__main__":
    unittest.main()
