from __future__ import annotations

from pathlib import Path

from plugins.memory.intelligent_memory.doctor import run_doctor
from plugins.memory.intelligent_memory.installer import install_plugin

HERMES_SOURCE = Path(__file__).resolve().parents[4]


def test_doctor_detects_installed_plugin_and_database(tmp_path) -> None:
    source = Path(__file__).resolve().parents[4] / "plugins" / "memory" / "intelligent_memory"
    install_plugin(source, tmp_path)

    result = run_doctor(tmp_path, hermes_source=HERMES_SOURCE)

    assert result.healthy is HERMES_SOURCE.exists()
    assert result.integrity is True
    assert result.database is True


def test_doctor_detects_deleted_plugin_after_update(tmp_path) -> None:
    result = run_doctor(tmp_path, hermes_source=HERMES_SOURCE)

    assert result.healthy is False
    assert result.integrity is False
    assert any("integrity" in detail for detail in result.details)
