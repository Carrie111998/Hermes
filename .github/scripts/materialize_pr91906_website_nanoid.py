#!/usr/bin/env python3
"""Reconcile the standalone website Nano ID advisory graph for PR #91906.

The repository root does not own ``website`` as an npm workspace, so the root
materialization pass cannot mutate or verify this lock graph. Keep this lane
explicit rather than allowing the one-shot publisher to certify an incomplete
repository-wide closure.

Provenance:
- NousResearch/hermes-agent#85916 by @hdy2001: Nano ID 3.3.18 advisory lane;
  review identified the standalone website graph as part of closure.
- NousResearch/hermes-agent#89335 by @SovereignSignal: root + website
  implementation of the Nano ID 3.3.18 remediation.
- NousResearch/hermes-agent#92573 by @mrxmoex: later root + website split that
  reconfirmed the same standalone graph and was triaged as overlapping #89335.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
WEBSITE = ROOT / "website"
TARGET_NANOID = "3.3.18"
ALLOWED_SOURCE_NANOID = {"3.3.17", TARGET_NANOID}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def apply() -> None:
    package_path = WEBSITE / "package.json"
    package = load_json(package_path)
    overrides = package.get("overrides")
    expect(isinstance(overrides, dict), "website package overrides are absent")
    source = overrides.get("nanoid")
    expect(source in ALLOWED_SOURCE_NANOID, f"unrecognized website Nano ID source: {source}")
    overrides["nanoid"] = TARGET_NANOID
    write_json(package_path, package)

    npmrc_path = WEBSITE / ".npmrc"
    npmrc = npmrc_path.read_text(encoding="utf-8")
    legacy_note = "# nanoid 3.3.17 includes fixes for GHSA-2v37-7h3g-55p8. remove when > 2wks old (rel 2026-08-03)"
    target_note = "# nanoid 3.3.18 fixes GHSA-2v37-7h3g-55p8. remove when > 2wks old (rel 2026-08-07)"
    if legacy_note in npmrc:
        npmrc = npmrc.replace(legacy_note, target_note, 1)
    else:
        expect(target_note in npmrc, "unexpected website Nano ID release-age note")
    npmrc_path.write_text(npmrc, encoding="utf-8")


def verify() -> None:
    package = load_json(WEBSITE / "package.json")
    overrides = package.get("overrides")
    expect(isinstance(overrides, dict), "website package overrides are absent")
    expect(overrides.get("nanoid") == TARGET_NANOID, "website Nano ID override is not 3.3.18")

    lock = load_json(WEBSITE / "package-lock.json")
    packages = lock.get("packages")
    expect(isinstance(packages, dict), "website lockfile package map is absent")
    nanoid_versions = [
        data.get("version")
        for path, data in packages.items()
        if path.endswith("node_modules/nanoid") and isinstance(data, dict)
    ]
    expect(TARGET_NANOID in nanoid_versions, f"website lock has no Nano ID {TARGET_NANOID}: {nanoid_versions}")
    expect(
        all(
            not (isinstance(version, str) and version.startswith("3.") and version != TARGET_NANOID)
            for version in nanoid_versions
        ),
        f"website lock still contains stale Nano ID 3.x: {nanoid_versions}",
    )

    npmrc = (WEBSITE / ".npmrc").read_text(encoding="utf-8")
    expect("# nanoid 3.3.17 includes fixes" not in npmrc, "website .npmrc still documents Nano ID 3.3.17")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"apply", "verify"}:
        raise SystemExit("usage: materialize_pr91906_website_nanoid.py {apply|verify}")
    if sys.argv[1] == "apply":
        apply()
    else:
        verify()
