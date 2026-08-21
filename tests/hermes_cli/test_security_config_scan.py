"""Behavior tests for ``hermes security scan``."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from hermes_cli import security_config_scan as scan
from hermes_cli.subcommands.security import build_security_parser


def _tirith_payload(*, title: str = "Hidden instruction", severity: str = "high") -> dict:
    return {
        "schema_version": 4,
        "scanned_count": 2,
        "total_findings": 1,
        "files": [
            {
                "path": "SOUL.md",
                "is_config_file": True,
                "findings": [
                    {
                        "rule_id": "agent_instruction_hidden",
                        "severity": severity,
                        "title": title,
                        "description": "hidden prompt text",
                    }
                ],
            }
        ],
    }


def _args(path, baseline, **overrides):
    values = {
        "paths": [str(path)],
        "baseline": str(baseline),
        "update_baseline": False,
        "fail_on": "high",
        "timeout": None,
        "json": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _install_fake_tirith(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(
        "tools.tirith_security.resolve_tirith_for_scan",
        lambda: ("tirith", {"tirith_scan_timeout": 17}),
    )
    monkeypatch.setattr(
        scan,
        "_scan_path",
        lambda tirith, path, timeout, include_patterns=(): scan._parse_tirith_output(payload),
    )


class TestTirithOutputParsing:
    def test_directory_output_is_flattened_and_sorted(self):
        result = scan._parse_tirith_output(_tirith_payload())

        assert result.scanned_count == 2
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.path == "SOUL.md"
        assert finding.rule_id == "agent_instruction_hidden"
        assert finding.severity == "HIGH"
        assert len(finding.fingerprint) == 64

    def test_single_file_schema_is_supported(self):
        payload = {
            "schema_version": 3,
            "path": "AGENTS.md",
            "findings": [{"rule_id": "html_comment", "severity": "critical"}],
        }

        result = scan._parse_tirith_output(payload)

        assert result.scanned_count == 1
        assert result.findings[0].path == "AGENTS.md"
        assert result.findings[0].severity == "CRITICAL"

    def test_fingerprint_changes_when_same_rule_has_new_evidence(self):
        before = scan._parse_tirith_output(_tirith_payload(title="Old content"))
        after = scan._parse_tirith_output(_tirith_payload(title="New content"))

        assert before.findings[0].fingerprint != after.findings[0].fingerprint


class TestTirithSubprocessContract:
    def test_finding_exit_code_is_valid_and_json_is_parsed(self, tmp_path, monkeypatch):
        completed = SimpleNamespace(
            returncode=1,
            stdout=json.dumps(_tirith_payload()),
            stderr="",
        )
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return completed

        monkeypatch.setattr(scan.subprocess, "run", fake_run)
        result = scan._scan_path("/bin/tirith", tmp_path, 23)

        assert result.scanned_count == 2
        command, kwargs = calls[0]
        assert command[:3] == ["/bin/tirith", "scan", str(tmp_path)]
        assert command[-1] == "--json"
        assert kwargs["timeout"] == 23

    def test_default_instruction_filter_is_forwarded_to_tirith(self, tmp_path, monkeypatch):
        completed = SimpleNamespace(returncode=0, stdout=json.dumps({"scanned_count": 0, "files": []}), stderr="")
        calls = []
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda command, **kwargs: calls.append(command) or completed,
        )

        scan._scan_path("tirith", tmp_path, 5, ("SOUL.md", "skills/*"))

        command = calls[0]
        assert command.count("--include") == 2
        assert command[-4:] == ["--include", "SOUL.md", "--include", "skills/*"]

    def test_invalid_json_is_an_operational_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
        )

        try:
            scan._scan_path("tirith", tmp_path, 1)
        except RuntimeError as exc:
            assert "invalid JSON" in str(exc)
        else:
            raise AssertionError("invalid JSON must fail closed")


class TestSecurityScanCommand:
    def test_new_high_finding_fails_gate(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        baseline = tmp_path / "baseline.json"
        monkeypatch.setattr(scan, "get_hermes_home", lambda: home)
        _install_fake_tirith(monkeypatch, _tirith_payload())

        code = scan.cmd_security_scan(_args(home, baseline))
        output = json.loads(capsys.readouterr().out)

        assert code == 1
        assert output["new_finding_count"] == 1
        assert output["new_findings"][0]["severity"] == "HIGH"

    def test_explicit_baseline_update_suppresses_unchanged_finding(
        self, tmp_path, monkeypatch, capsys
    ):
        home = tmp_path / "home"
        home.mkdir()
        baseline = tmp_path / "baseline.json"
        monkeypatch.setattr(scan, "get_hermes_home", lambda: home)
        _install_fake_tirith(monkeypatch, _tirith_payload())

        update_code = scan.cmd_security_scan(
            _args(home, baseline, update_baseline=True)
        )
        capsys.readouterr()
        second_code = scan.cmd_security_scan(_args(home, baseline))
        output = json.loads(capsys.readouterr().out)

        assert update_code == 0
        assert second_code == 0
        assert output["new_finding_count"] == 0
        stored = json.loads(baseline.read_text(encoding="utf-8"))
        assert stored["schema_version"] == scan.BASELINE_SCHEMA_VERSION
        assert len(stored["fingerprints"]) == 1

    def test_medium_finding_respects_default_high_gate(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(scan, "get_hermes_home", lambda: home)
        _install_fake_tirith(monkeypatch, _tirith_payload(severity="medium"))

        code = scan.cmd_security_scan(_args(home, tmp_path / "missing-baseline.json"))
        capsys.readouterr()

        assert code == 0

    def test_missing_scan_path_returns_usage_error(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(scan, "get_hermes_home", lambda: home)

        code = scan.cmd_security_scan(
            _args(tmp_path / "does-not-exist", tmp_path / "baseline.json")
        )

        assert code == 2
        assert "does not exist" in capsys.readouterr().err

    def test_default_baseline_is_application_owned_security_state(
        self, tmp_path, monkeypatch
    ):
        from agent.file_safety import is_write_denied

        home = tmp_path / ".hermes" / "profiles" / "work"
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))

        baseline = home / "security" / "tirith-scan-baseline.json"

        assert is_write_denied(str(baseline)) is True


class TestSecurityScanParser:
    def test_scan_options_are_wired_to_security_handler(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        handler = lambda args: args
        build_security_parser(subparsers, cmd_security=handler)

        args = parser.parse_args(
            ["security", "scan", "./workspace", "--fail-on", "critical", "--json"]
        )

        assert args.func is handler
        assert args.security_command == "scan"
        assert args.paths == ["./workspace"]
        assert args.fail_on == "critical"
        assert args.json is True
