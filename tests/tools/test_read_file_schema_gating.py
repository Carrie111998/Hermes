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


class TestNeedsOcrPath(unittest.TestCase):
    """anydoc>=0.2 NeedsOcrError wiring: hosted OCR attempt + typed warning
    (maintainer caveats: #1 nous-gateway Parse was live-probed HTTP 500 →
    attempt-and-fall-through; #2 warning recommends LOCAL OCR skills)."""

    def _fake_mod(self, hosted_result=None, hosted_exc=None):
        class NeedsOcrError(Exception):
            def __init__(self, pages):
                super().__init__("needs ocr")
                self.pages = pages

        calls = []

        class Mod:
            pass

        mod = Mod()
        mod.NeedsOcrError = NeedsOcrError

        def to_markdown(path, **kw):
            calls.append(kw)
            if not kw:
                raise NeedsOcrError([2, 3])
            if hosted_exc is not None:
                raise hosted_exc
            return hosted_result

        mod.to_markdown = to_markdown
        return mod, calls

    def test_hosted_success_returns_ocr_text(self):
        from tools import read_extract as rx

        mod, calls = self._fake_mod(hosted_result="OCR TEXT")
        with patch.object(rx, "_anydoc", return_value=mod),              patch.object(rx, "_hosted_ocr_config",
                          return_value=(True, "key", None)),              patch.object(rx.os.path, "getsize", return_value=10):
            out = rx._extract_anydoc("scan.pdf")
        self.assertEqual(out, "OCR TEXT\n")
        self.assertEqual(calls[1].get("ocr"), "hosted")

    def test_hosted_failure_warns_and_prefers_local_skills(self):
        from tools import read_extract as rx

        mod, _ = self._fake_mod(hosted_exc=RuntimeError("HTTP 500"))
        with patch.object(rx, "_anydoc", return_value=mod),              patch.object(rx, "_hosted_ocr_config",
                          return_value=(True, "key", "https://gw")),              patch.object(rx.os.path, "getsize", return_value=10):
            out = rx._extract_anydoc("scan.pdf")
        self.assertIn("[NEEDS OCR", out)
        self.assertIn("pages 2, 3", out)
        self.assertIn("attempted and failed", out)
        # Maintainer-directed: HINT at checking for an OCR skill; never
        # name one (none is guaranteed to exist), never sell config knobs.
        self.assertIn("check whether an OCR skill is available", out)
        self.assertIn("skills_list", out)
        self.assertNotIn("ocr-and-documents", out)
        self.assertNotIn("marker-pdf", out)
        self.assertNotIn("hosted_ocr", out)

    def test_disabled_warns_without_attempt(self):
        from tools import read_extract as rx

        mod, calls = self._fake_mod()
        with patch.object(rx, "_anydoc", return_value=mod),              patch.object(rx, "_hosted_ocr_config",
                          return_value=(False, None, None)),              patch.object(rx.os.path, "getsize", return_value=10):
            out = rx._extract_anydoc("scan.pdf")
        self.assertIn("[NEEDS OCR", out)
        self.assertEqual(len(calls), 1)  # no hosted attempt
        # Same shape when disabled: skill hint, no knob advertising.
        self.assertIn("check whether an OCR skill is available", out)
        self.assertNotIn("hosted_ocr", out)
        self.assertNotIn("ocr-and-documents", out)

    def test_pin_lockstep(self):
        """pyproject core pin and lazy_deps self-heal pin must match."""
        import re
        from pathlib import Path

        py = Path("pyproject.toml").read_text(encoding="utf-8")
        lz = Path("tools/lazy_deps.py").read_text(encoding="utf-8")
        m1 = re.search(r'"firecrawl-anydoc==([\d.]+)"', py)
        m2 = re.search(r'"firecrawl-anydoc==([\d.]+)"', lz)
        self.assertIsNotNone(m1)
        self.assertIsNotNone(m2)
        self.assertEqual(m1.group(1), m2.group(1))


if __name__ == "__main__":
    unittest.main()
