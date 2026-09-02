"""Regression tests for #85422: release gate attaches only provenance-verified
macOS DMGs and refuses zero-asset publishes.

Reviews of the first iteration (#100600) established two contracts this suite
pins:

1. mtime is not provenance — a copied/touched DMG must NOT attach, and a
   publish with no verifiable DMG must be a blocking outcome the caller can
   fail closed on (not a warning the release path ignores).
2. Tests must exercise the PRODUCTION selector —
   scripts/release.py::collect_release_dmg_attachments — not a mirrored
   local copy that can stay green while the real code diverges.

The provenance authority is the build receipt (.build-receipt.json: source
git SHA + build timestamp, written by the build host), with the DMG mtime
as corroborating evidence within a tolerance.
"""

import json
import os
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


def _make_bundle(tmp_path: Path, *, receipt: dict | None,
                 dmgs: dict[str, float] | None = None,
                 receipt_mtime_offset: float = 0.0) -> Path:
    """Create a fake Tauri bundle/dmg directory with receipt and DMGs.

    ``dmgs`` maps filename -> mtime offset from the receipt build time in
    SECONDS (positive = newer than receipt, negative = older).
    """
    import release as _rel
    dmg_dir = tmp_path / "apps" / "bootstrap-installer" / "src-tauri" / "target" / "release" / "bundle" / "dmg"
    dmg_dir.mkdir(parents=True)
    built_at = receipt.get("built_at_unix", time.time()) if receipt else time.time()

    if receipt is not None:
        rp = dmg_dir / ".build-receipt.json"
        rp.write_text(json.dumps(receipt), encoding="utf-8")
        m = built_at + receipt_mtime_offset
        os.utime(rp, (m, m))

    for name, offset in (dmgs or {}).items():
        p = dmg_dir / name
        p.write_bytes(b"fake dmg")
        m = built_at + offset
        os.utime(p, (m, m))

    # Point the production module at this fake bundle dir.
    orig = _rel._BUNDLE_DMG_DIR
    _rel._BUNDLE_DMG_DIR = dmg_dir
    return dmg_dir, orig


@pytest.fixture()
def patched_bundle_dir():
    """Restore the production bundle dir path after each test."""
    import release as _rel
    yield
    # each _make_bundle call stashes orig; restore via a fresh marker
    if getattr(_rel, "_BUNDLE_DMG_DIR", None) != (
        REPO_ROOT / "apps" / "bootstrap-installer" / "src-tauri"
        / "target" / "release" / "bundle" / "dmg"
    ):
        _rel._BUNDLE_DMG_DIR = (
            REPO_ROOT / "apps" / "bootstrap-installer" / "src-tauri"
            / "target" / "release" / "bundle" / "dmg"
        )


class TestProvenanceGate:
    def test_matching_receipt_fresh_dmg_attaches(self, tmp_path):
        sha = "abc123def4567890"
        built = time.time() - 3600  # 1h ago
        _make_bundle(tmp_path, receipt={"sha": sha, "built_at_unix": built},
                    dmgs={"Hermes_0.0.1_aarch64.dmg": 0.0})
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX")
        assert len(r.attachments) == 1
        assert r.blocking_reason is None
        assert any("receipt sha" in note for _, note in r.evidence)

    def test_sha_mismatch_blocks(self, tmp_path):
        """DMG built from a DIFFERENT tree must not attach (#100600 review)."""
        built = time.time() - 3600
        _make_bundle(tmp_path, receipt={"sha": "ffffffffffff", "built_at_unix": built},
                    dmgs={"H.dmg": 0.0})
        r = collect_release_dmg_attachments(release_sha="abc123def4567", release_tag="vX")
        assert r.attachments == []
        assert r.blocking_reason and "mismatch" in r.blocking_reason

    def test_missing_receipt_blocks(self, tmp_path):
        """No receipt = no provenance = blocking, not warning."""
        _make_bundle(tmp_path, receipt=None, dmgs={"H.dmg": 0.0})
        r = collect_release_dmg_attachments(release_sha="abc", release_tag="vX")
        assert r.attachments == []
        assert r.blocking_reason and "no build receipt" in r.blocking_reason

    def test_stale_receipt_blocks(self, tmp_path):
        """Receipt older than the release window (the June-6 shape)."""
        sha = "abc123def456"
        built = time.time() - 100 * 3600  # 100h ago
        _make_bundle(tmp_path, receipt={"sha": sha, "built_at_unix": built},
                    dmgs={"H.dmg": 0.0})
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX", max_age_hours=72)
        assert r.attachments == []
        assert r.blocking_reason and "h old" in r.blocking_reason

    def test_touched_copied_dmg_not_attached(self, tmp_path):
        """mtime drift beyond tolerance from the receipt = provenance broken.

        This is the copied-DMG attack from the #100600 review: a file touched
        NOW in a directory whose receipt is 1h old must not attach.
        """
        sha = "abc123def456"
        built = time.time() - 3600
        _make_bundle(tmp_path, receipt={"sha": sha, "built_at_unix": built},
                    dmgs={"H.dmg": 86400.0})  # mtime 24h AFTER receipt time
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX")
        assert r.attachments == []
        assert r.blocking_reason  # no provable DMG -> blocking

    def test_missing_bundle_dir_blocks(self, tmp_path):
        """Non-macOS release host: blocking_reason set (caller bypasses via flag)."""
        import release as _rel
        orig = _rel._BUNDLE_DMG_DIR
        _rel._BUNDLE_DMG_DIR = tmp_path / "nonexistent"
        try:
            r = collect_release_dmg_attachments(release_sha="abc", release_tag="vX")
            assert r.attachments == []
            assert r.blocking_reason and "no Tauri bundle" in r.blocking_reason
        finally:
            _rel._BUNDLE_DMG_DIR = orig

    def test_corrupt_receipt_blocks(self, tmp_path):
        d, orig = _make_bundle(tmp_path, receipt={"sha": "abc", "built_at_unix": 1})
        (d / ".build-receipt.json").write_text("not json{", encoding="utf-8")
        import release as _rel
        _rel._BUNDLE_DMG_DIR = d
        try:
            r = collect_release_dmg_attachments(release_sha="abc", release_tag="vX")
            assert r.blocking_reason and "unreadable" in r.blocking_reason
        finally:
            _rel._BUNDLE_DMG_DIR = orig

    def test_both_architectures_attach_ordered(self, tmp_path):
        sha = "abc123def456"
        built = time.time() - 600
        _make_bundle(tmp_path, receipt={"sha": sha, "built_at_unix": built},
                    dmgs={"H_aarch64.dmg": 0.0, "H_x64.dmg": 0.0})
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX")
        assert len(r.attachments) == 2
        assert "aarch64" in r.attachments[0]  # sorted order
        assert "x64" in r.attachments[1]
        assert r.blocking_reason is None

    def test_non_dmg_files_ignored(self, tmp_path):
        sha = "abc123def456"
        built = time.time() - 600
        _make_bundle(tmp_path, receipt={"sha": sha, "built_at_unix": built},
                    dmgs={"H.dmg": 0.0})
        (d, orig) = (None, None)
        # add junk files
        bd = __import__("release")._BUNDLE_DMG_DIR
        (bd / "notes.txt").write_text("x")
        (bd / "H.app").mkdir()
        r = collect_release_dmg_attachments(release_sha=sha, release_tag="vX")
        assert len(r.attachments) == 1
        assert r.attachments[0].endswith(".dmg")


class TestResultShape:
    def test_result_type_contract(self):
        r = DmgAttachmentResult()
        assert r.attachments == []
        assert r.evidence == []
        assert r.blocking_reason is None
