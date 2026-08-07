import os
from pathlib import Path

import pytest

# Real repo paths (read-only check), NOT the patched tmp root — parity is a
# property of the deployed tree, mirroring nightly_gate._dual_path_drift().
_HERMES = Path(os.path.expanduser("~")) / ".hermes"
_ROOT = _HERMES / "scripts" / "devflow_delegation_tick.py"
_PROFILE = _HERMES / "profiles" / "main" / "scripts" / "devflow_delegation_tick.py"


@pytest.mark.skipif(not (_ROOT.exists() and _PROFILE.exists()),
                    reason="wrapper pair not deployed in this checkout")
def test_wrapper_pair_byte_identical():
    assert _ROOT.read_bytes() == _PROFILE.read_bytes(), (
        "scripts/devflow_delegation_tick.py and profiles/main/scripts/"
        "devflow_delegation_tick.py must stay byte-identical "
        "(nightly_gate._dual_path_drift)")
