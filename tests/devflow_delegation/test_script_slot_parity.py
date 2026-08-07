import os
from pathlib import Path

import pytest

# Real repo paths (read-only check), NOT the patched tmp root — parity is a
# property of the deployed tree, mirroring nightly_gate._dual_path_drift().
_HERMES = Path(os.path.expanduser("~")) / ".hermes"
_ROOT = _HERMES / "scripts" / "devflow_delegation_tick.py"
_PROFILE = _HERMES / "profiles" / "main" / "scripts" / "devflow_delegation_tick.py"


def _parity_verdict(root: Path, profile: Path) -> str:
    """Classify a wrapper pair.

    'missing'   -- NEITHER twin deployed: foreign checkout, skip.
    'violation' -- exactly one twin present (a half-deleted pair IS the parity
                   violation) or bytes differ.
    'ok'        -- both present and byte-identical.
    """
    if not root.exists() and not profile.exists():
        return "missing"
    if not root.exists() or not profile.exists():
        return "violation"
    if root.read_bytes() != profile.read_bytes():
        return "violation"
    return "ok"


def test_wrapper_pair_byte_identical():
    verdict = _parity_verdict(_ROOT, _PROFILE)
    if verdict == "missing":
        pytest.skip("wrapper pair not deployed in this checkout")
    assert verdict == "ok", (
        "scripts/devflow_delegation_tick.py and profiles/main/scripts/"
        "devflow_delegation_tick.py must BOTH exist and stay byte-identical "
        "(nightly_gate._dual_path_drift cannot see a half-deleted pair)")


def test_parity_verdict_semantics(tmp_path):
    """The skip/fail seam itself, exercised on disposable paths. A
    HALF-DELETED pair (exactly one twin present) must classify as a violation,
    NOT a skip: nightly_gate._dual_path_drift intersects the two script dirs
    and silently drops a one-sided pair, so this test is the only guard that
    can see it. 'missing' is reserved for foreign checkouts with NEITHER twin.
    """
    root = tmp_path / "scripts" / "devflow_delegation_tick.py"
    profile = tmp_path / "profiles" / "main" / "scripts" / "devflow_delegation_tick.py"

    assert _parity_verdict(root, profile) == "missing"  # neither deployed

    root.parent.mkdir(parents=True)
    root.write_text("x\n", encoding="utf-8")
    assert _parity_verdict(root, profile) == "violation"  # half-deleted pair

    profile.parent.mkdir(parents=True)
    profile.write_text("x\n", encoding="utf-8")
    assert _parity_verdict(root, profile) == "ok"  # both present, identical

    profile.write_text("y\n", encoding="utf-8")
    assert _parity_verdict(root, profile) == "violation"  # byte drift

    root.unlink()
    assert _parity_verdict(root, profile) == "violation"  # other half deleted
