from __future__ import annotations

import plistlib
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hermes_cli.desktop_install import (
    DesktopInstallError,
    canonical_macos_app,
    install_macos_app,
    macos_app_executable,
)


def _bundle(root: Path, version: str) -> Path:
    app = root / "Hermes.app"
    contents = app / "Contents"
    executable = contents / "MacOS" / "Hermes"
    executable.parent.mkdir(parents=True)
    executable.write_text(version, encoding="utf-8")
    executable.chmod(0o755)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.nousresearch.hermes",
                "CFBundleExecutable": "Hermes",
                "CFBundleShortVersionString": version,
            },
            stream,
        )
    return app


def _copytree(source: Path, target: Path) -> None:
    shutil.copytree(source, target)


class DesktopInstallContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_canonical_install_contract_is_per_user_applications(self) -> None:
        target = canonical_macos_app(self.root)
        self.assertEqual(target, self.root / "Applications" / "Hermes.app")
        self.assertEqual(macos_app_executable(target), target / "Contents" / "MacOS" / "Hermes")

    def test_two_updates_replace_one_stable_install_in_place(self) -> None:
        first = _bundle(self.root / "build-v1", "1")
        second = _bundle(self.root / "build-v2", "2")
        target = canonical_macos_app(self.root / "user")
        dock_url = target.as_uri()

        one = install_macos_app(first, home=self.root / "user", copy_bundle=_copytree)
        two = install_macos_app(second, home=self.root / "user", copy_bundle=_copytree)

        self.assertEqual(one.target, target)
        self.assertEqual(two.target, target)
        self.assertEqual(two.executable.read_text(encoding="utf-8"), "2")
        self.assertEqual(target.as_uri(), dock_url)
        self.assertFalse(target.with_name("Hermes.app.installing").exists())
        self.assertFalse(target.with_name("Hermes.app.rollback").exists())

    def test_failed_second_update_restores_previous_bundle(self) -> None:
        first = _bundle(self.root / "build-v1", "1")
        second = _bundle(self.root / "build-v2", "2")
        home = self.root / "user"
        target = canonical_macos_app(home)
        install_macos_app(first, home=home, copy_bundle=_copytree)
        calls = 0

        def fail_new_bundle(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated final rename failure")
            source.rename(destination)

        with self.assertRaisesRegex(DesktopInstallError, "previous app was restored"):
            install_macos_app(second, home=home, copy_bundle=_copytree, move=fail_new_bundle)

        self.assertEqual(macos_app_executable(target).read_text(encoding="utf-8"), "1")

    def test_refuses_noncanonical_target_and_source_alias(self) -> None:
        source = _bundle(self.root / "release", "1")
        home = self.root / "user"

        with self.assertRaisesRegex(DesktopInstallError, "canonical"):
            install_macos_app(
                source,
                home=home,
                target=self.root / ".hermes" / "Hermes.app",
                copy_bundle=_copytree,
            )

        canonical = canonical_macos_app(home)
        canonical.parent.mkdir(parents=True)
        shutil.copytree(source, canonical)
        with self.assertRaisesRegex(DesktopInstallError, "artifact source"):
            install_macos_app(canonical, home=home, copy_bundle=_copytree)

    def test_refuses_wrong_bundle_identity(self) -> None:
        source = _bundle(self.root / "release", "1")
        info = source / "Contents" / "Info.plist"
        with info.open("wb") as stream:
            plistlib.dump(
                {"CFBundleIdentifier": "example.not-hermes", "CFBundleExecutable": "Hermes"},
                stream,
            )

        with self.assertRaisesRegex(DesktopInstallError, "bundle identifier"):
            install_macos_app(source, home=self.root / "user", copy_bundle=_copytree)


if __name__ == "__main__":
    unittest.main()
