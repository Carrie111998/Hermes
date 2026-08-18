from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from hermes_cli import web_git


class WebGitWindowsConsoleTests(unittest.TestCase):
    def test_git_forwards_windows_hide_flags_to_subprocess(self) -> None:
        sentinel = 0x08000000
        completed = subprocess.CompletedProcess(
            args=["git", "status"], returncode=0, stdout="ok", stderr=""
        )

        with (
            patch.object(web_git, "windows_hide_flags", return_value=sentinel, create=True),
            patch.object(web_git.subprocess, "run", return_value=completed) as run,
        ):
            code, stdout, stderr = web_git._git("C:/repo", ["status"])

        self.assertEqual((code, stdout, stderr), (0, "ok", ""))
        self.assertEqual(run.call_args.kwargs["creationflags"], sentinel)


if __name__ == "__main__":
    unittest.main()
