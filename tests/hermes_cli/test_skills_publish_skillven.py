"""RED-first tests for the Skillven publish provider (Hermes Agent 18-B).

Covers: parser accepts ``--to skillven`` + ``--registry``, and ``do_publish``
dispatches to the skillven path without touching github/clawhub behavior.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

import hermes_cli.skillven_publish as skillven_publish_mod
import hermes_cli.skills_hub as skills_hub_mod
from hermes_cli.subcommands.skills import build_skills_parser


class _FakeConsole:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, msg=""):
        self.lines.append(str(msg))


def _write_frontmatter_skill(tmp_path, name="demo", description="A demo skill."):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nBody\n", encoding="utf-8"
    )
    return root


def _build_parser():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_skills_parser(sub, cmd_skills=lambda args: None)
    return parser


def test_publish_parser_accepts_skillven_target_and_registry():
    parser = _build_parser()
    ns = parser.parse_args([
        "skills",
        "publish",
        "/tmp/my-skill",
        "--to",
        "skillven",
        "--registry",
        "https://staging.skillven.example",
    ])
    assert ns.to == "skillven"
    assert ns.registry == "https://staging.skillven.example"


def test_publish_parser_registry_defaults_empty():
    parser = _build_parser()
    ns = parser.parse_args(["skills", "publish", "/tmp/my-skill", "--to", "github"])
    assert ns.registry == ""


def test_publish_parser_still_rejects_unknown_target():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["skills", "publish", "/tmp/my-skill", "--to", "bogus"])


# ---------------------------------------------------------------------------
# do_publish dispatch: skillven runs the shared scan first, always (spec item 2)
# ---------------------------------------------------------------------------


def test_do_publish_skillven_runs_common_scan_then_delegates(tmp_path, monkeypatch):
    from tools import skills_guard

    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("# Demo\n\nNo frontmatter.\n", encoding="utf-8")

    scanned = []
    monkeypatch.setattr(
        skills_guard,
        "scan_skill",
        lambda path, source: (
            scanned.append((path, source)) or SimpleNamespace(verdict="safe")
        ),
    )
    monkeypatch.setattr(skills_guard, "format_scan_report", lambda result: "scan ok")

    delegated = []
    monkeypatch.setattr(
        skillven_publish_mod,
        "do_skillven_publish",
        lambda path, **kw: delegated.append((path, kw)),
    )

    console = _FakeConsole()
    skills_hub_mod.do_publish(
        str(root), target="skillven", registry="https://example.test", console=console
    )

    assert scanned and scanned[0][1] == "self"
    assert len(delegated) == 1
    delegated_path, kwargs = delegated[0]
    assert delegated_path == root
    assert kwargs["registry"] == "https://example.test"
    assert kwargs["scan_result"].verdict == "safe"


def test_do_publish_skillven_dangerous_scan_blocks_without_delegating(
    tmp_path, monkeypatch
):
    from tools import skills_guard

    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("# Demo\n\nNo frontmatter.\n", encoding="utf-8")

    monkeypatch.setattr(
        skills_guard,
        "scan_skill",
        lambda path, source: SimpleNamespace(verdict="dangerous"),
    )
    monkeypatch.setattr(
        skills_guard, "format_scan_report", lambda result: "scan dangerous"
    )

    delegated = []
    monkeypatch.setattr(
        skillven_publish_mod,
        "do_skillven_publish",
        lambda path, **kw: delegated.append((path, kw)),
    )

    console = _FakeConsole()
    skills_hub_mod.do_publish(str(root), target="skillven", console=console)

    assert delegated == []
    assert any("DANGEROUS" in line for line in console.lines)


# ---------------------------------------------------------------------------
# skills_command dispatch forwards --registry (spec item 1)
# ---------------------------------------------------------------------------


def test_skills_command_publish_forwards_registry(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        skills_hub_mod,
        "do_publish",
        lambda *a, **kw: captured.update(kw) or captured.setdefault("args", a),
    )
    ns = argparse.Namespace(
        skills_action="publish",
        skill_path="/tmp/x",
        to="skillven",
        repo="",
        registry="https://staging.example",
    )
    skills_hub_mod.skills_command(ns)

    assert captured["registry"] == "https://staging.example"
    assert captured["target"] == "skillven"


# ---------------------------------------------------------------------------
# github/clawhub regression: unchanged behavior (spec item 9)
# ---------------------------------------------------------------------------


def test_do_publish_clawhub_message_unchanged(tmp_path, monkeypatch):
    from tools import skills_guard

    root = _write_frontmatter_skill(tmp_path)
    monkeypatch.setattr(
        skills_guard,
        "scan_skill",
        lambda path, source: SimpleNamespace(verdict="safe"),
    )
    monkeypatch.setattr(skills_guard, "format_scan_report", lambda result: "scan ok")

    console = _FakeConsole()
    skills_hub_mod.do_publish(str(root), target="clawhub", console=console)

    assert any("not yet supported" in line for line in console.lines)


def test_do_publish_github_still_requires_repo(tmp_path, monkeypatch):
    from tools import skills_guard

    root = _write_frontmatter_skill(tmp_path)
    monkeypatch.setattr(
        skills_guard,
        "scan_skill",
        lambda path, source: SimpleNamespace(verdict="safe"),
    )
    monkeypatch.setattr(skills_guard, "format_scan_report", lambda result: "scan ok")

    console = _FakeConsole()
    skills_hub_mod.do_publish(str(root), target="github", console=console)

    assert any("--repo required" in line for line in console.lines)


def test_do_publish_still_rejects_missing_description_for_github(tmp_path, monkeypatch):
    from tools import skills_guard

    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: demo\n---\nBody\n", encoding="utf-8")
    monkeypatch.setattr(
        skills_guard,
        "scan_skill",
        lambda path, source: SimpleNamespace(verdict="safe"),
    )
    monkeypatch.setattr(skills_guard, "format_scan_report", lambda result: "scan ok")

    console = _FakeConsole()
    skills_hub_mod.do_publish(
        str(root), target="github", repo="owner/repo", console=console
    )

    assert any("must have a 'description'" in line for line in console.lines)
