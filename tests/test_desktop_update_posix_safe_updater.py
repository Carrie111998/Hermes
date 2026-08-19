import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'desktop-update' / 'posix.sh'


class DesktopSafeUpdateSelectionTests(unittest.TestCase):
    def make_repo(self, branch):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name) / 'hermes-agent'
        root.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.test'], cwd=root, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=root, check=True)
        (root / 'README.md').write_text('fixture\n', encoding='utf-8')
        subprocess.run(['git', 'add', '.'], cwd=root, check=True)
        subprocess.run(['git', 'commit', '-qm', 'base'], cwd=root, check=True)
        current = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=root, check=True, text=True, capture_output=True,
        ).stdout.strip()
        if current != branch:
            subprocess.run(['git', 'checkout', '-qb', branch], cwd=root, check=True)
        scripts = root.parent / 'scripts'
        scripts.mkdir()
        updater = scripts / 'update-hermes-local.py'
        updater.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        updater.chmod(0o700)
        return td, root

    def select(self, root):
        return subprocess.run(
            ['/bin/bash', str(SCRIPT), '--install-root', str(root), '--self-test-command'],
            text=True, capture_output=True,
        )

    def test_local_patch_branch_selects_safe_updater(self):
        td, root = self.make_repo('hermes-local-fixes')
        try:
            before = {path.relative_to(root.parent) for path in root.parent.rglob('*')}
            result = self.select(root)
            after = {path.relative_to(root.parent) for path in root.parent.rglob('*')}
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'safe')
            self.assertEqual(after, before)
            self.assertFalse((root.parent / 'logs').exists())
        finally:
            td.cleanup()

    def test_main_branch_keeps_builtin_updater(self):
        td, root = self.make_repo('main')
        try:
            result = self.select(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'builtin')
        finally:
            td.cleanup()

    def test_missing_safe_updater_fails_closed_on_patch_branch(self):
        td, root = self.make_repo('hermes-local-fixes')
        try:
            (root.parent / 'scripts' / 'update-hermes-local.py').unlink()
            result = self.select(root)
            self.assertEqual(result.returncode, 3)
            self.assertIn('safe updater is missing', result.stderr)
        finally:
            td.cleanup()


if __name__ == '__main__':
    unittest.main()
