#!/usr/bin/env python3
"""Materialize the dependency-security closure from exact landing-main truth.

Source/provenance:
- NousResearch/hermes-agent#89479 by @schmitzi8: Electron 41.10.3,
  internal extractor transition, and Linux ARM64 packaged-layout repair.
- NousResearch/hermes-agent#90486 by @orcaspainting-dev: scoped PostCSS
  -> Nano ID 3.3.18 override preserving Nano ID 6 consumers.
- NousResearch/hermes-agent#92543 by @mrxmoex: candidate Electron 43.4.1 and
  Nano ID 3.3.18 source. If those bytes are already landing-main truth, they
  are preserved rather than downgraded to the historical 41.x source.
- NousResearch/hermes-agent#91042: historical combined exact-object evidence,
  including the Python h2 4.4.1 lock repair.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LEGACY_ELECTRON = "40.10.2"
VERIFIED_ELECTRON = "41.10.3"
NEWER_ELECTRON = "43.4.1"
ALLOWED_ELECTRON = {LEGACY_ELECTRON, VERIFIED_ELECTRON, NEWER_ELECTRON}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    expect(count == 1, f"{label}: expected one source occurrence, found {count}")
    return text.replace(old, new, 1)


def source_electron_version() -> str:
    desktop = load_json(ROOT / "apps/desktop/package.json")
    dev = desktop["devDependencies"].get("electron")
    build = desktop["build"].get("electronVersion")
    expect(dev == build, f"Desktop Electron pins disagree: dev={dev}, build={build}")
    expect(dev in ALLOWED_ELECTRON, f"unrecognized Electron source: {dev}")
    return str(dev)


def target_electron_version() -> str:
    source = source_electron_version()
    return VERIFIED_ELECTRON if source == LEGACY_ELECTRON else source


def apply() -> None:
    source_electron = source_electron_version()
    target_electron = VERIFIED_ELECTRON if source_electron == LEGACY_ELECTRON else source_electron

    npmrc_path = ROOT / ".npmrc"
    npmrc = npmrc_path.read_text(encoding="utf-8")
    old_note = "# nanoid 3.3.17 includes fixes for GHSA-2v37-7h3g-55p8. remove when > 2wks old (rel 2026-08-03)"
    new_note = "# nanoid 3.3.18 fixes GHSA-2v37-7h3g-55p8. remove when > 2wks old (rel 2026-08-07)"
    if old_note in npmrc:
        npmrc = replace_once(npmrc, old_note, new_note, label=".npmrc Nano ID release-age note")
    else:
        expect(new_note in npmrc, "unexpected Nano ID release-age note on landing main")
    npmrc_path.write_text(npmrc, encoding="utf-8")

    root_package_path = ROOT / "package.json"
    root_package = load_json(root_package_path)
    overrides = root_package["overrides"]
    expect(overrides.get("nanoid@^6") == "6.0.0", "Nano ID v6 isolation changed on landing main")
    postcss = overrides.get("postcss")
    nanoid_v3 = overrides.get("nanoid@^3")
    if postcss == "8.5.23":
        expect(nanoid_v3 in {"3.3.17", "3.3.18"}, "unexpected Nano ID v3 override on landing main")
        del overrides["nanoid@^3"]
        overrides["postcss"] = {".": "8.5.23", "nanoid": "3.3.18"}
    else:
        expect(nanoid_v3 is None, "broad Nano ID v3 override survived alongside scoped PostCSS")
        expect(postcss == {".": "8.5.23", "nanoid": "3.3.18"}, "unexpected scoped PostCSS/Nano ID state")

    allow_scripts = root_package["allowScripts"]
    source_key = f"electron@{source_electron}"
    target_key = f"electron@{target_electron}"
    expect(allow_scripts.get(source_key) is True, f"missing Electron allowScripts key: {source_key}")
    rewritten_allow_scripts: dict[str, Any] = {}
    inserted = False
    for key, value in allow_scripts.items():
        if key == source_key:
            rewritten_allow_scripts[target_key] = True
            inserted = True
            continue
        expect(not (key.startswith("electron@") and key != "electron-winstaller@5.4.0"), f"unexpected extra Electron script key: {key}")
        rewritten_allow_scripts[key] = value
    expect(inserted, "Electron script allowlist reconciliation did not execute")
    root_package["allowScripts"] = rewritten_allow_scripts
    write_json(root_package_path, root_package)

    desktop_package_path = ROOT / "apps/desktop/package.json"
    desktop_package = load_json(desktop_package_path)
    expect(desktop_package["devDependencies"].get("electron") == source_electron, "Desktop Electron source changed during apply")
    expect(desktop_package["build"].get("electronVersion") == source_electron, "electron-builder source pin changed during apply")
    desktop_package["devDependencies"]["electron"] = target_electron
    desktop_package["build"]["electronVersion"] = target_electron
    write_json(desktop_package_path, desktop_package)

    layout_path = ROOT / "apps/desktop/scripts/packaged-app-layout.mjs"
    layout_path.write_text(
        "export function resolveLinuxUnpackedDirName(arch) {\n"
        "  return arch === 'x64' ? 'linux-unpacked' : `linux-${arch}-unpacked`\n"
        "}\n",
        encoding="utf-8",
    )

    layout_test_path = ROOT / "apps/desktop/scripts/packaged-app-layout.test.mjs"
    layout_test_path.write_text(
        "import assert from 'node:assert/strict'\n"
        "import test from 'node:test'\n\n"
        "import { resolveLinuxUnpackedDirName } from './packaged-app-layout.mjs'\n\n"
        "test('uses electron-builder default directory on Linux x64', () => {\n"
        "  assert.equal(resolveLinuxUnpackedDirName('x64'), 'linux-unpacked')\n"
        "})\n\n"
        "test('includes architecture in electron-builder Linux ARM64 directory', () => {\n"
        "  assert.equal(resolveLinuxUnpackedDirName('arm64'), 'linux-arm64-unpacked')\n"
        "})\n",
        encoding="utf-8",
    )

    desktop_test_path = ROOT / "apps/desktop/scripts/test-desktop.mjs"
    desktop_test = desktop_test_path.read_text(encoding="utf-8")
    if "resolveLinuxUnpackedDirName(ARCH)" not in desktop_test:
        desktop_test = replace_once(
            desktop_test,
            "import { listPackage } from '@electron/asar'\n\nimport PACKAGE_JSON from '../package.json' with { type: 'json' }",
            "import { listPackage } from '@electron/asar'\n\nimport { resolveLinuxUnpackedDirName } from './packaged-app-layout.mjs'\nimport PACKAGE_JSON from '../package.json' with { type: 'json' }",
            label="Desktop packaged-layout import",
        )
        desktop_test = replace_once(
            desktop_test,
            "  // linux unpacked layout matches windows but with different binary name\n  const unpacked = path.join(RELEASE_ROOT, 'linux-unpacked')",
            "  // electron-builder includes the architecture suffix for non-x64 Linux output.\n  const unpacked = path.join(RELEASE_ROOT, resolveLinuxUnpackedDirName(ARCH))",
            label="Desktop Linux packaged-layout resolution",
        )
        desktop_test_path.write_text(desktop_test, encoding="utf-8")


def verify() -> None:
    target_electron = target_electron_version()
    root_package = load_json(ROOT / "package.json")
    overrides = root_package["overrides"]
    expect("nanoid@^3" not in overrides, "broad Nano ID v3 override survived")
    expect(overrides.get("nanoid@^6") == "6.0.0", "Nano ID v6 isolation was not preserved")
    expect(overrides.get("postcss") == {".": "8.5.23", "nanoid": "3.3.18"}, "scoped PostCSS/Nano ID override is wrong")
    expect(root_package["allowScripts"].get(f"electron@{target_electron}") is True, "target Electron script allowlist is missing")
    expect("electron@40.10.2" not in root_package["allowScripts"], "Electron 40 script allowlist survived")

    desktop_package = load_json(ROOT / "apps/desktop/package.json")
    expect(desktop_package["devDependencies"].get("electron") == target_electron, f"Desktop Electron dependency is not {target_electron}")
    expect(desktop_package["build"].get("electronVersion") == target_electron, f"electron-builder runtime pin is not {target_electron}")
    for name, version in {
        "@babel/core": "8.0.1",
        "@rolldown/plugin-babel": "0.2.3",
        "babel-plugin-react-compiler": "1.0.0",
    }.items():
        expect(desktop_package["devDependencies"].get(name) == version, f"landing-main React Compiler dependency lost: {name}")

    lock = load_json(ROOT / "package-lock.json")
    packages = lock["packages"]
    desktop_lock = packages["apps/desktop"]
    expect(desktop_lock["devDependencies"].get("electron") == target_electron, "lockfile Desktop Electron manifest is stale")
    electron_entries = [(path, data) for path, data in packages.items() if path.endswith("node_modules/electron") and isinstance(data, dict)]
    expect(electron_entries, "lockfile has no Electron package entry")
    expect(all(data.get("version") == target_electron for _, data in electron_entries), f"unexpected Electron versions: {electron_entries}")
    for path, electron in electron_entries:
        dependencies = electron.get("dependencies", {})
        expect("@electron-internal/extract-zip" in dependencies, f"{path} did not move to Electron's internal extractor")
        expect("extract-zip" not in dependencies, f"{path} still depends on legacy extract-zip")
        expect(dependencies.get("@electron/get", "").startswith("^5."), f"{path} did not move to @electron/get 5")

    nanoid_versions = [data.get("version") for path, data in packages.items() if path.endswith("node_modules/nanoid") and isinstance(data, dict)]
    expect("3.3.18" in nanoid_versions, f"Nano ID 3.3.18 is absent: {nanoid_versions}")
    expect("6.0.0" in nanoid_versions, f"Nano ID 6.0.0 isolation is absent: {nanoid_versions}")
    expect(all(not (isinstance(version, str) and version.startswith("3.") and version != "3.3.18") for version in nanoid_versions), f"unexpected Nano ID 3.x survived: {nanoid_versions}")

    uv_lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    h2_match = re.search(r'\[\[package\]\]\nname = "h2"\nversion = "([^"]+)"', uv_lock)
    expect(h2_match is not None, "h2 package is absent from uv.lock")
    expect(h2_match.group(1) == "4.4.1", f"uv.lock resolved h2 to {h2_match.group(1)}, expected 4.4.1")

    desktop_test = (ROOT / "apps/desktop/scripts/test-desktop.mjs").read_text(encoding="utf-8")
    expect("resolveLinuxUnpackedDirName(ARCH)" in desktop_test, "Desktop package collector still hard-codes linux-unpacked")
    expect((ROOT / "apps/desktop/scripts/packaged-app-layout.mjs").exists(), "packaged-layout helper is absent")
    expect((ROOT / "apps/desktop/scripts/packaged-app-layout.test.mjs").exists(), "packaged-layout regression is absent")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"apply", "verify"}:
        raise SystemExit("usage: materialize_pr91906_dependency_security.py {apply|verify}")
    if sys.argv[1] == "apply":
        apply()
    else:
        verify()
