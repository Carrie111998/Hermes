"""Regression tests for issue #54347: search_files skips empty directories.

The bug: search_files is documented as an ls replacement but the
underlying find/rg commands only enumerate files (-type f, rg --files),
so empty directories are never visible. Users trying to navigate a
fresh structure hit "no results" because their vault dirs are empty.

The fix: add a `type` parameter to search_files (default "files") with
three values: "files", "dirs", "all". When type=dirs or type=all,
directory entries are returned with a trailing '/' so callers can
distinguish them from files.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path


class _FakeTerminalEnv:
    """Minimal terminal_env that runs commands via subprocess on the host."""

    def execute(self, command, cwd=None, timeout=60):
        import subprocess
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "output": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"output": "", "stderr": "timeout", "returncode": 124}


class TestSearchFilesEmptyDirs(unittest.TestCase):
    """Regression tests for #54347."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="search_files_test_")
        self.root = Path(self.tmp)

        (self.root / "file1.py").write_text("print('hello')")
        (self.root / "file2.txt").write_text("data")
        sub_dir = self.root / "subdir"
        sub_dir.mkdir()
        (sub_dir / "nested.py").write_text("nested")

        (self.root / "empty_dir").mkdir()
        (self.root / "another_empty").mkdir()
        (sub_dir / "empty_in_subdir").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_shell_op(self):
        from tools.file_operations import ShellFileOperations
        return ShellFileOperations(terminal_env=_FakeTerminalEnv())

    def test_search_files_default_excludes_empty_dirs(self):
        """Default behavior (type='files') must NOT enumerate empty dirs."""
        op = self._make_shell_op()
        result = op.search(
            pattern="*",
            path=str(self.root),
            target="files",
            type="files",
            limit=100,
        )
        self.assertIsNone(result.error, f"unexpected error: {result.error}")
        for f in result.files:
            self.assertFalse(
                f.endswith("/"),
                f"unexpected dir entry in default mode: {f!r}",
            )
        self.assertGreaterEqual(len(result.files), 3)

    def test_search_files_type_dirs_lists_empty_dirs(self):
        """type=dirs must surface empty directories."""
        op = self._make_shell_op()
        result = op.search(
            pattern="*",
            path=str(self.root),
            target="files",
            type="dirs",
            limit=100,
        )
        self.assertIsNone(result.error, f"unexpected error: {result.error}")
        self.assertGreater(len(result.files), 0, "no directories returned")
        for f in result.files:
            self.assertTrue(
                f.endswith("/"),
                f"dir entry should end with '/': {f!r}",
            )
        names = [os.path.basename(f.rstrip("/")) for f in result.files]
        self.assertIn("empty_dir", names, "empty_dir missing")
        self.assertIn("another_empty", names, "another_empty missing")

    def test_search_files_type_all_lists_files_and_dirs(self):
        """type=all must include both files and directories."""
        op = self._make_shell_op()
        result = op.search(
            pattern="*",
            path=str(self.root),
            target="files",
            type="all",
            limit=100,
        )
        self.assertIsNone(result.error, f"unexpected error: {result.error}")
        dir_entries = [f for f in result.files if f.endswith("/")]
        file_entries = [f for f in result.files if not f.endswith("/")]
        self.assertGreater(len(dir_entries), 0, "no directories")
        self.assertGreater(len(file_entries), 0, "no files")

    def test_search_files_invalid_type_returns_error(self):
        """Invalid type values must be rejected with a clear error."""
        op = self._make_shell_op()
        result = op.search(
            pattern="*",
            path=str(self.root),
            target="files",
            type="invalid_value",
            limit=100,
        )
        self.assertIsNotNone(result.error)
        self.assertIn("Invalid type", result.error)

    def test_search_files_dirs_excludes_files(self):
        """type=dirs must NOT include files."""
        op = self._make_shell_op()
        result = op.search(
            pattern="*",
            path=str(self.root),
            target="files",
            type="dirs",
            limit=100,
        )
        self.assertIsNone(result.error)
        for f in result.files:
            self.assertFalse(
                f.endswith(".py") or f.endswith(".txt"),
                f"unexpected file in dirs mode: {f!r}",
            )


if __name__ == "__main__":
    unittest.main()
