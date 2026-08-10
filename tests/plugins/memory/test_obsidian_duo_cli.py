import argparse
import json
from pathlib import Path

from plugins.memory.obsidian_duo import cli
from plugins.memory.obsidian_duo.config import ObsidianDuoConfig
from plugins.memory.obsidian_duo.contracts import MemoryRecord
from plugins.memory.obsidian_duo.vault import ObsidianVault


def test_register_cli_exposes_required_commands():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="provider")
    provider_parser = sub.add_parser("obsidian_duo")
    cli.register_cli(provider_parser)

    for command in ("status", "doctor", "rebuild-index", "reconcile", "pending", "conflicts", "stats"):
        args = parser.parse_args(["obsidian_duo", command])
        assert args.obsidian_duo_command == command


def test_doctor_returns_safe_structured_checks(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    ObsidianDuoConfig(vault_path=str(tmp_path / "Vault")).save(tmp_path)
    result = cli.run_diagnostics()

    assert "vault_reachable" in result
    assert all("sk-" not in str(value) for value in result.values())


def test_rebuild_index_performs_the_named_action(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    config = ObsidianDuoConfig(vault_path=str(tmp_path / "Vault"))
    config.save(tmp_path)
    vault = ObsidianVault(Path(config.vault_path), config.managed_folder)
    vault.write_managed_note(MemoryRecord("mem_1", "A decision", "decision", "global"))

    cli.obsidian_duo_command(argparse.Namespace(obsidian_duo_command="rebuild-index", full=True))
    result = json.loads(capsys.readouterr().out)

    assert result["rebuild"]["scanned"] == 1
    assert result["rebuild"]["reparsed"] == 1
