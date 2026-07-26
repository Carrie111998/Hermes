"""CS-02c lane-attribution enforcement tests."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hermes_cli.cost import ledger


def test_record_call_is_keyword_only_for_lane():
    with pytest.raises(TypeError):
        ledger.record_call(
            task_id="missing-lane",
            vendor="retell",
            voice_minutes=1,
        )


def test_no_bare_record_call_in_production_code():
    repo = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for root_name in ("agent", "tools", "hermes_cli"):
        for path in (repo / root_name).rglob("*.py"):
            if path.is_relative_to(repo / "hermes_cli" / "cost"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                is_record_call = (
                    isinstance(function, ast.Name)
                    and function.id == "record_call"
                ) or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "record_call"
                )
                if not is_record_call:
                    continue
                if not any(keyword.arg == "lane" for keyword in node.keywords):
                    offenders.append(f"{path.relative_to(repo)}:{node.lineno}")
    assert offenders == [], "bare record_call callsites: " + ", ".join(offenders)
