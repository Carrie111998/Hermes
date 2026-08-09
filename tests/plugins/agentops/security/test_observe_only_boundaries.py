from __future__ import annotations

from pathlib import Path

import pytest

from plugins.agentops.control.events import EventSpool, EventValidationError
from plugins.agentops.control.store import open_store


def test_synthetic_secret_never_enters_spool_or_database(tmp_path, make_event):
    secret = "sk-test-canary-secret"
    spool = EventSpool(tmp_path / "spool")
    store = open_store(tmp_path / "state.db")

    with pytest.raises(EventValidationError):
        event = make_event(payload={"cookie": secret})
        spool.write(event)

    assert secret.encode() not in (tmp_path / "state.db").read_bytes()
    assert not spool.pending_paths()


def test_phase_one_source_contains_no_target_execution_primitives():
    root = Path(__file__).resolve().parents[4] / "plugins" / "agentops"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.rglob("*.py")
    )

    assert "subprocess" not in source
    assert "os.system" not in source
    assert "launchctl" not in source
    assert "shell=True" not in source
