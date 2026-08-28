"""Focused tests for the local-only ``hermes debug bundle`` path."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def diagnostic_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    logs = home / "logs"
    logs.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (logs / "agent.log").write_text(
        "INFO started\n", encoding="utf-8"
    )
    return home


def test_bundle_parser_has_no_unredact_option():
    import argparse

    from hermes_cli.subcommands.debug import build_debug_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_debug_parser(subparsers, cmd_debug=lambda args: args)

    args = parser.parse_args(["debug", "bundle"])
    assert args.debug_command == "bundle"
    assert not hasattr(args, "no_redact")


def test_bundle_redacts_before_serialization(diagnostic_home):
    from hermes_cli.debug import _diagnostic_json_bytes, collect_local_diagnostic_bundle

    secret = "«redacted:sk-…»"
    (diagnostic_home / "logs" / "agent.log").write_text(
        f"api_key={secret}\nperson@example.com\n", encoding="utf-8"
    )

    document = collect_local_diagnostic_bundle()
    serialized = _diagnostic_json_bytes(document).decode("utf-8")

    assert secret not in serialized
    assert "person@example.com" not in serialized
    assert "[REDACTED_EMAIL]" in serialized
    assert document["audience"] == "local-only"
    assert document["exportable"] is False


def test_bundle_enforces_input_and_section_bounds(diagnostic_home):
    from hermes_cli.debug import (
        _DIAGNOSTIC_MAX_INPUT_BYTES,
        _DIAGNOSTIC_MAX_SECTION_BYTES,
        _diagnostic_json_bytes,
        collect_local_diagnostic_bundle,
    )

    line = "x" * 1000 + "\n"
    (diagnostic_home / "logs" / "agent.log").write_text(
        line * 300, encoding="utf-8"
    )

    document = collect_local_diagnostic_bundle()
    item = next(
        item for item in document["sections"]["logs"]["items"] if item["name"] == "agent.log"
    )

    assert item["status"] == "truncated"
    assert item["counts"]["input_bytes"] <= _DIAGNOSTIC_MAX_INPUT_BYTES
    assert item["counts"]["output_bytes"] <= _DIAGNOSTIC_MAX_SECTION_BYTES
    assert len(_diagnostic_json_bytes(document)) <= document["manifest"]["limits"]["total_bytes"]
    assert document["manifest"]["complete"] is False


def test_bundle_is_local_only_and_profile_scoped(diagnostic_home, capsys):
    from hermes_cli.debug import run_debug

    args = SimpleNamespace(debug_command="bundle", output="result.json", no_redact=True)
    with patch("hermes_cli.debug._sweep_expired_pastes") as sweep, \
         patch("hermes_cli.debug.urllib.request.urlopen") as urlopen, \
         patch("hermes_cli.dump.run_dump") as dump:
        run_debug(args)

    sweep.assert_not_called()
    urlopen.assert_not_called()
    dump.assert_not_called()
    output = diagnostic_home / "diagnostics" / "result.json"
    assert output.exists()
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["audience"] == "local-only"
    assert "result.json" in capsys.readouterr().out

    from hermes_cli.debug import DiagnosticBundleError, write_local_diagnostic_bundle

    with pytest.raises(DiagnosticBundleError, match="profile-scoped"):
        write_local_diagnostic_bundle(loaded, diagnostic_home.parent / "escape.json")


def test_bundle_commit_cleans_incomplete_staging(diagnostic_home, monkeypatch):
    from hermes_cli.debug import collect_local_diagnostic_bundle, write_local_diagnostic_bundle

    document = collect_local_diagnostic_bundle()
    destination = diagnostic_home / "diagnostics" / "failed.json"
    destination.parent.mkdir()
    destination.write_text("old", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("hermes_cli.debug.os.replace", fail_replace)
    with pytest.raises(Exception, match="commit failed"):
        write_local_diagnostic_bundle(document, destination)

    assert destination.read_text(encoding="utf-8") == "old"
    assert list(destination.parent.glob("*.tmp")) == []


def test_diagnostic_output_path_rejects_reserved_device_names(diagnostic_home):
    from hermes_cli.debug import DiagnosticBundleError, _diagnostic_output_path

    for name in ("CON.txt", "con.txt", "AUX.log"):
        with pytest.raises(DiagnosticBundleError, match="invalid diagnostic output name"):
            _diagnostic_output_path(name)


def test_diagnostic_truncate_text_is_utf8_safe_and_bounded():
    from hermes_cli.debug import _diagnostic_truncate_text

    kept, truncated = _diagnostic_truncate_text("abcdefg", 3)
    assert kept == "abc"
    assert truncated is True
    kept, truncated = _diagnostic_truncate_text("abcdefg", 7)
    assert kept == "abcdefg"
    assert truncated is False
    value = "x" * 5 + "\U0001F600" + "y" * 10
    kept, truncated = _diagnostic_truncate_text(value, 6)
    assert truncated is True
    encoded = kept.encode("utf-8")
    assert len(encoded) <= 6
    encoded.decode("utf-8")
    assert kept == "xxxxx"


def test_bundle_staging_file_lives_in_diagnostics_dir(diagnostic_home, monkeypatch):
    import tempfile as tempfile_module

    from hermes_cli.debug import collect_local_diagnostic_bundle, write_local_diagnostic_bundle

    captured = {}
    real_mkstemp = tempfile_module.mkstemp

    def spy_mkstemp(*args, **kwargs):
        captured["dir"] = kwargs.get("dir")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr("hermes_cli.debug.tempfile.mkstemp", spy_mkstemp)
    document = collect_local_diagnostic_bundle()
    destination = write_local_diagnostic_bundle(document, "staged.json")
    assert captured["dir"] == str(destination.parent)
    assert captured["dir"] != tempfile_module.gettempdir()
    assert destination.read_bytes()
