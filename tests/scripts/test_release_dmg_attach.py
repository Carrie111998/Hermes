"""Regression tests for #85422: release gate attaches only provenance-verified
macOS DMGs and refuses zero-asset publishes.

The #100600 review (v2 head 001e05397) established four contracts this
suite pins, all exercised against the PRODUCTION selector
scripts/release.py::collect_release_dmg_attachments:

1. mtime is not provenance — ARBITRARY BYTES with an aligned mtime must NOT
   attach (the review's stale-or-arbitrary.dmg attack). Only DMGs whose
   RECOMPUTED SHA-256 matches the build manifest's per-artifact inventory
   attach.
2. The gate runs BEFORE tag/push in main() — invalid evidence means no tag,
   no push, no gh release create. (Asserted here at the helper level via
   blocking_reason; the mutation-order contract lives in the release path
   itself, whose gate call now precedes git_result("tag", ...).)
3. A manifest built at a DIFFERENT HEAD must never attach (SHA mismatch).
4. Corrupt/missing/type-invalid manifest fields (non-numeric built_at_unix)
   produce a blocking reason, never a crash.
"""

import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from release import (  # noqa: E402  — production import, deliberately
    DmgAttachmentResult,
    collect_release_dmg_attachments,
)

_BUNDLE_PATH = (
    REPO_ROOT / "apps" / "bootstrap-installer" / "src-tauri"
    / "target" / "release" / "bundle" / "dmg"
)


@pytest.fixture()
def bundle(tmp_path, monkeypatch):
    """Fake bundle dir patched into the production module; per-test manifest."""
    import release as _rel

    dmg_dir = tmp_path / "bundle" / "dmg"
    dmg_dir.mkdir(parents=True)
    monkeypatch.setattr(_rel, "_BUNDLE_DMG_DIR", dmg_dir)
    yield dmg_dir


def _write_manifest(dmg_dir, sha, built_at, artifacts):
    (dmg_dir / ".build-manifest.json").write_text(
        json.dumps({"sha": sha, "built_at_unix": built_at, "artifacts": artifacts}),
        encoding="utf-8",
    )


def _real_artifact(dmg_dir, name="Hermes_0.0.1_aarch64.dmg", content=b"real dmg bytes"):
    p = dmg_dir / name
    p.write_bytes(content)
    return p


def _sha256_of(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


class TestDigestVerification:
    def test_matching_manifest_and_digest_attaches(self, bundle):
        sha = "abc123def4567890"
        built = int(time.time()) - 3600
        dmg = _real_artifact(bundle)
        _write_manifest(bundle, sha, built, [
            {"filename": dmg.name, "sha256": _sha256_of(dmg), "size": dmg.stat().st_size, "arch": "aarch64"},
        ])
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX")
        assert len(r.attachments) == 1
        assert r.blocking_reason is None
        assert "digest verified" in r.evidence[0][1]

    def test_arbitrary_bytes_with_aligned_mtime_rejected(self, bundle):
        """The #100600 v2 review attack: stale-or-arbitrary.dmg whose mtime
        happens to align must NOT attach — its digest cannot match."""
        sha = "abc123def4567890"
        built = int(time.time()) - 3600
        real = _real_artifact(bundle)
        # The ATTACK: a second file whose mtime aligns but bytes are garbage.
        attacker = bundle / "stale-or-arbitrary.dmg"
        attacker.write_bytes(b"arbitrary bytes")
        import os
        os.utime(attacker, (built + 60, built + 60))
        _write_manifest(bundle, sha, built, [
            {"filename": real.name, "sha256": _sha256_of(real), "size": real.stat().st_size, "arch": "aarch64"},
        ])
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX")
        assert len(r.attachments) == 1
        assert attacker.name not in [Path(a).name for a in r.attachments]

    def test_modified_real_dmg_rejected(self, bundle):
        """The REAL DMG edited after the manifest — digest mismatch blocks."""
        sha = "abc123def4567890"
        built = int(time.time()) - 3600
        dmg = _real_artifact(bundle)
        _write_manifest(bundle, sha, built, [
            {"filename": dmg.name, "sha256": "0" * 64, "size": dmg.stat().st_size, "arch": "aarch64"},
        ])
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX")
        assert r.attachments == []
        assert r.blocking_reason and "digest" in r.blocking_reason.lower()

    def test_size_mismatch_rejected(self, bundle):
        sha = "abc123def4567890"
        built = int(time.time()) - 3600
        dmg = _real_artifact(bundle)
        _write_manifest(bundle, sha, built, [
            {"filename": dmg.name, "sha256": _sha256_of(dmg), "size": 999, "arch": "aarch64"},
        ])
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX")
        assert r.attachments == []
        assert r.blocking_reason


class TestManifestIntegrity:
    def test_sha_mismatch_blocks(self, bundle):
        built = int(time.time()) - 3600
        dmg = _real_artifact(bundle)
        _write_manifest(bundle, "ffffffffffff", built, [
            {"filename": dmg.name, "sha256": _sha256_of(dmg), "size": dmg.stat().st_size, "arch": "aarch64"},
        ])
        r = collect_release_dmg_attachments(release_sha="abc123def4567", release_tag="vX")
        assert r.attachments == []
        assert r.blocking_reason and "mismatch" in r.blocking_reason

    def test_missing_manifest_blocks(self, bundle):
        _real_artifact(bundle)
        r = collect_release_dmg_attachments(release_sha="abc", release_tag="vX")
        assert r.blocking_reason and "no build manifest" in r.blocking_reason

    def test_non_numeric_built_at_blocks_not_crashes(self, bundle):
        """The v2 review: valid JSON with non-numeric built_at_unix raised
        TypeError. Must produce a blocking reason instead."""
        sha = "abc123def456"
        dmg = _real_artifact(bundle)
        (bundle / ".build-manifest.json").write_text(
            json.dumps({
                "sha": sha,
                "built_at_unix": "yesterday",
                "artifacts": [{"filename": dmg.name, "sha256": _sha256_of(dmg), "size": 1, "arch": "a"}],
            }),
            encoding="utf-8",
        )
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX")
        assert r.blocking_reason and "built_at_unix" in r.blocking_reason

    def test_corrupt_manifest_blocks(self, bundle):
        (bundle / ".build-manifest.json").write_text("not json{", encoding="utf-8")
        r = collect_release_dmg_attachments(release_sha="abc", release_tag="vX")
        assert r.blocking_reason and "unreadable" in r.blocking_reason

    def test_non_object_manifest_blocks(self, bundle):
        (bundle / ".build-manifest.json").write_text("[1,2,3]", encoding="utf-8")
        r = collect_release_dmg_attachments(release_sha="abc", release_tag="vX")
        assert r.blocking_reason and "not a JSON object" in r.blocking_reason

    def test_empty_inventory_blocks(self, bundle):
        _write_manifest(bundle, "abc123", int(time.time()) - 60, [])
        r = collect_release_dmg_attachments(release_sha="abc123", release_tag="vX")
        assert r.blocking_reason and "no artifact inventory" in r.blocking_reason

    def test_stale_manifest_blocks(self, bundle):
        sha = "abc123def456"
        built = int(time.time()) - 100 * 3600
        dmg = _real_artifact(bundle)
        _write_manifest(bundle, sha, built, [
            {"filename": dmg.name, "sha256": _sha256_of(dmg), "size": dmg.stat().st_size, "arch": "aarch64"},
        ])
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX", max_age_hours=72)
        assert r.attachments == []
        assert r.blocking_reason and "h old" in r.blocking_reason

    def test_missing_bundle_dir_blocks(self, tmp_path, monkeypatch):
        import release as _rel
        monkeypatch.setattr(_rel, "_BUNDLE_DMG_DIR", tmp_path / "nonexistent")
        r = collect_release_dmg_attachments(release_sha="abc", release_tag="vX")
        assert r.attachments == []
        assert r.blocking_reason and "no Tauri bundle" in r.blocking_reason


class TestHeadResolutionCrash:
    """The v2 review's blocker 1: git_result returns CompletedProcess, so
    ``(git_result(...) or "").strip()`` crashed with AttributeError AFTER
    the tag was pushed. The production gate now reads .stdout explicitly."""

    def test_git_result_returns_completedprocess(self):
        from release import git_result

        result = git_result("rev-parse", "--is-inside-work-tree")
        assert hasattr(result, "stdout")
        assert not hasattr(result, "strip")


class TestResultShape:
    def test_result_type_contract(self):
        r = DmgAttachmentResult()
        assert r.attachments == []
        assert r.evidence == []
        assert r.blocking_reason is None
