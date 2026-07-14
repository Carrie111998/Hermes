from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from session_bridge.characterize import run_live_characterization


pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_SESSION_BRIDGE_LIVE_TESTS") != "1",
    reason="set HERMES_SESSION_BRIDGE_LIVE_TESTS=1 to create disposable native sessions",
)


@pytest.mark.timeout(600)
def test_real_claude_and_codex_create_discover_read_and_resume() -> None:
    report_path = run_live_characterization()
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report_path.parent == (
        Path.home() / ".hermes" / "session-bridge" / "characterization"
    )
    assert report["automatic_mirroring_enabled"] is False
    for provider in ("claude", "codex"):
        assert report["providers"][provider]["create"] is True
        assert report["providers"][provider]["discover"] is True
        assert report["providers"][provider]["read"] is True
        assert report["providers"][provider]["resume"] is True

    print(f"Hermes Session Bridge characterization report: {report_path}")
    print(
        "Codex registration turn required: "
        f"{report['providers']['codex']['used_registration_turn']}"
    )
