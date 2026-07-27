import argparse
import json

import pytest

from hermes_cli import business


def _parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    business.build_parser(sub)
    return parser


def test_business_bootstrap_is_noninteractive_and_persists_charter(
    tmp_path, monkeypatch, capsys
):
    charter_path = tmp_path / "charter.json"
    charter = {"enabled": True, "initial_mandate": {"desired_outcome": "revenue"}}
    charter_path.write_text(json.dumps(charter), encoding="utf-8")
    saved = []
    monkeypatch.setattr(
        "hermes_cli.setup._bootstrap_agentic_business",
        lambda value: ("org_bootstrap", "objective_bootstrap"),
    )
    monkeypatch.setattr(
        "hermes_cli.config.save_config",
        lambda value, **kwargs: saved.append((value, kwargs)),
    )

    args = _parser().parse_args(
        ["business", "bootstrap", "--charter-file", str(charter_path)]
    )
    assert business.business_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "bootstrapped"
    assert output["organization_id"] == "org_bootstrap"
    assert saved == [({"agentic": charter}, {"merge_existing": True})]


def test_business_bootstrap_rejects_disabled_charter(tmp_path, monkeypatch):
    charter_path = tmp_path / "charter.json"
    charter_path.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.setup._bootstrap_agentic_business",
        lambda value: pytest.fail("disabled charter must not bootstrap"),
    )
    args = _parser().parse_args(
        ["business", "bootstrap", "--charter-file", str(charter_path)]
    )
    with pytest.raises(ValueError, match="enabled charter"):
        business.business_command(args)
