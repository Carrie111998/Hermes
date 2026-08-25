"""`hermes doctor --json` — machine-readable findings tail.

With --json, every check_ok/warn/fail/info finding is collected and a
marker-delimited JSON document (schema doctor-json/v1) is appended after
the human report: counts per severity, the full findings list, and the
auto-fix/manual action lists. The human report is unchanged.
"""

import json
import types

import pytest


@pytest.fixture()
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _run(monkeypatch, capsys, **extra):
    from hermes_cli import doctor as doctor_mod

    # Keep the run cheap + deterministic: skip the network-y sections.
    monkeypatch.setattr(
        doctor_mod,
        "_section",
        lambda title: None,
    )

    def _noop(*a, **k):
        return None

    monkeypatch.setattr(doctor_mod, "check_certificates", _noop)

    args = types.SimpleNamespace(fix=False, live=False, ack=None, json=True)
    doctor_mod.run_doctor(args)
    out = capsys.readouterr().out
    marker = "----- doctor-json v1 -----"
    assert marker in out, "--json tail missing"
    doc = json.loads(out.split(marker, 1)[1])
    return doc, out


def test_json_tail_parses_with_expected_schema(tmp_home, monkeypatch, capsys):
    doc, _out = _run(monkeypatch, capsys)
    assert doc["schema"] == "doctor-json/v1"
    assert set(doc["counts"].keys()) == {"ok", "info", "warn", "fail"}
    assert isinstance(doc["findings"], list) and doc["findings"], (
        "at least the always-on checks must record findings"
    )
    for f in doc["findings"]:
        assert f["severity"] in {"ok", "info", "warn", "fail"}
        assert isinstance(f["check"], str)
    assert isinstance(doc["manual_issues"], list)
    assert "generated_at" in doc


def test_findings_match_counts(tmp_home, monkeypatch, capsys):
    doc, _out = _run(monkeypatch, capsys)
    total = sum(doc["counts"].values())
    assert total == len(doc["findings"])


def test_no_json_flag_keeps_output_clean(tmp_home, monkeypatch, capsys):
    from hermes_cli import doctor as doctor_mod

    args = types.SimpleNamespace(fix=False, live=False, ack=None)
    monkeypatch.setattr(doctor_mod, "_section", lambda title: None)
    try:
        doctor_mod.run_doctor(args)
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "doctor-json" not in out
