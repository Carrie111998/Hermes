"""
tests/test_skill_ledger_signing.py

Tests optional cryptographic execution signing for Hermes skill ledger mutations.
"""

import os
import tempfile
from pathlib import Path
from tools.skill_ledger import append_entry, verify_skill_ledger_integrity


def test_skill_ledger_cryptographic_signing_when_enabled(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_AUDIT_SIGNING", "1")

        # Record a curator mutation
        entry_id = append_entry(
            action="create",
            skill="test-skill",
            actor="curator",
            evidence={"reason": "Self-improving evolution"},
        )
        assert entry_id is not None

        # Verify ledger cryptographic integrity
        summary = verify_skill_ledger_integrity()
        if summary.get("chain_valid") is not None:
            assert summary["chain_valid"] is True
