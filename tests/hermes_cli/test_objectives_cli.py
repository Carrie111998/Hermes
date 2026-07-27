from __future__ import annotations

import argparse
import json

from hermes_cli.objectives import build_parser


def _parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser(sub)
    return parser


def test_cli_create_list_and_show(tmp_path, capsys):
    db_path = tmp_path / "objectives.db"
    parser = _parser()
    args = parser.parse_args(
        [
            "objectives",
            "--db",
            str(db_path),
            "--json",
            "create",
            "Reach sustainable profitability",
            "--originator",
            "user:mike",
            "--success-criteria",
            '["positive operating cash flow"]',
        ]
    )
    assert args.func(args) == 0
    created = json.loads(capsys.readouterr().out)

    args = parser.parse_args(
        ["objective", "--db", str(db_path), "--json", "list"]
    )
    assert args.func(args) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in listed] == [created["id"]]

    args = parser.parse_args(
        ["objectives", "--db", str(db_path), "show", created["id"]]
    )
    assert args.func(args) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["desired_outcome"] == "Reach sustainable profitability"
    assert snapshot["events"][0]["kind"] == "created"


def test_cli_rejects_non_json_plan_payload(tmp_path):
    parser = _parser()
    args = parser.parse_args(
        [
            "objectives",
            "--db",
            str(tmp_path / "objectives.db"),
            "plan",
            "obj_missing",
            "--created-by",
            "ceo",
            "--tasks",
            "not-json",
        ]
    )
    try:
        args.func(args)
    except ValueError as exc:
        assert "tasks must be valid JSON" in str(exc)
    else:
        raise AssertionError("invalid JSON should fail closed")
