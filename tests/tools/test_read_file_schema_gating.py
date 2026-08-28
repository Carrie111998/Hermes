"""read_file schema diet (#95681): anydoc-gated extraction line +
PDF-coverage teaching moved to the response-time warning.

Maintainer-directed: the schema advertised anydoc-gated formats
unconditionally ("convert too when the optional anydoc converter is
available") and pre-taught the EXTRACTION COVERAGE WARNING's own
instructions. Now the format list renders only when anydoc is importable,
and the warning (read_extract.py) is the single teacher — it fires exactly
when pages are missing, with the page map and recovery commands.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tools.file_tools import READ_FILE_SCHEMA, _read_file_schema_overrides


class TestReadFileSchemaGating(unittest.TestCase):
    def _desc(self, anydoc_present: bool) -> str:
        import importlib.util as ilu

        real = ilu.find_spec

        def fake(name, *a, **kw):
            if name == "anydoc":
                class _Spec:  # truthy stand-in
                    pass
                return _Spec() if anydoc_present else None
            return real(name, *a, **kw)

        with patch("importlib.util.find_spec", side_effect=fake):
            return _read_file_schema_overrides()["description"]

    def test_anydoc_absent_hides_gated_formats(self):
        desc = self._desc(False)
        for token in ("PDF", "OpenDocument", "RTF", "EPUB", "anydoc"):
            self.assertNotIn(token, desc, token)
        # Always-supported extractions still advertised.
        for token in (".ipynb", ".docx", ".xlsx"):
            self.assertIn(token, desc, token)

    def test_anydoc_present_advertises_formats_without_caveat(self):
        desc = self._desc(True)
        for token in ("PDF (text layer)", ".doc/.ppt/.xls", "OpenDocument",
                      "RTF", "EPUB"):
            self.assertIn(token, desc, token)
        # The availability caveat and install mechanics are gone either way.
        self.assertNotIn("when the optional", desc)
        self.assertNotIn("auto-installed", desc)

    def test_coverage_warning_teaching_left_to_the_warning(self):
        """The response-time warning owns the recovery curriculum."""
        for desc in (self._desc(True), self._desc(False),
                     READ_FILE_SCHEMA["description"]):
            self.assertNotIn("EXTRACTION COVERAGE WARNING", desc)
            self.assertNotIn("pdftoppm", desc)
        # And the warning itself still carries it (single source).
        import inspect
        from tools import read_extract

        src = inspect.getsource(read_extract)
        self.assertIn("EXTRACTION COVERAGE WARNING", src)
        self.assertIn("pdftoppm", src)
        self.assertIn("vision_analyze", src)

    def test_binary_note_stays_last(self):
        desc = self._desc(True)
        self.assertLess(desc.find("EPUB"), desc.find("Cannot read images/binary"))

    def test_missing_anydoc_error_teaches_install(self):
        from tools.read_extract import _anydoc_missing_error

        err = _anydoc_missing_error("x.epub")
        self.assertIn("firecrawl-anydoc", err)
        self.assertIn("anydoc", err)
        # Distinct from the generic unsupported-type error.
        self.assertNotEqual(err, "Unsupported document type: 'x.epub'")


if __name__ == "__main__":
    unittest.main()
