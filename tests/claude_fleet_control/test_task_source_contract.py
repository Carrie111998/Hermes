"""Source contracts for the deployed shadow lane.

These parse the operational files WITHOUT executing them and pin the
shadow-only guarantees: the runner never passes --allow-enforce and never
references the legacy culler; the task fires every 5 min with a 2-min limit,
IgnoreNew, StartWhenAvailable, and least privilege. The files live in the
outer ~/.hermes repo (ops/tasks + bin), so these skip cleanly on a checkout
that lacks them rather than failing.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_HERMES = Path.home() / ".hermes"
_TASK = _HERMES / "ops" / "tasks" / "Hermes-Claude-Fleet-Controller.xml"
_RUNNER = _HERMES / "bin" / "claude_fleet_controller_run.ps1"
_CONFIG = Path(__file__).resolve().parents[2] / "claude_fleet_control" / "config.json"
_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


requires_task = pytest.mark.skipif(not _TASK.exists(), reason="task XML not present in this checkout")
requires_runner = pytest.mark.skipif(not _RUNNER.exists(), reason="runner not present in this checkout")


@requires_task
def test_task_xml_is_well_formed_and_shadow_bounded():
    root = ET.parse(_TASK).getroot()
    settings = root.find("t:Settings", _NS)
    assert settings.find("t:ExecutionTimeLimit", _NS).text == "PT2M"
    assert settings.find("t:MultipleInstancesPolicy", _NS).text == "IgnoreNew"
    assert settings.find("t:StartWhenAvailable", _NS).text == "true"

    rep = root.find(".//t:TimeTrigger/t:Repetition", _NS)
    assert rep.find("t:Interval", _NS).text == "PT5M"

    principal = root.find(".//t:Principals/t:Principal", _NS)
    assert principal.find("t:RunLevel", _NS).text == "LeastPrivilege"

    args = root.find(".//t:Actions/t:Exec/t:Arguments", _NS).text
    assert "claude_fleet_controller_run.ps1" in args


@requires_task
def test_task_action_never_enables_enforcement_or_the_legacy_culler():
    text = _TASK.read_text(encoding="utf-8")
    args = ET.parse(_TASK).getroot().find(".//t:Actions/t:Exec/t:Arguments", _NS).text
    assert "--allow-enforce" not in args
    assert "cull-claude-sessions" not in text
    assert "cull-idle-claude-sessions" not in text


@requires_runner
def test_runner_omits_enforce_flag_and_legacy_culler():
    # Comment lines may EXPLAIN why the flag is absent; the contract is that no
    # executable line ever passes it. Strip comment-only lines before checking.
    code_lines = [
        ln for ln in _RUNNER.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "--allow-enforce" not in code
    assert "cull-claude-sessions" not in code
    assert "cull-idle-claude-sessions" not in code
    assert "run_claude_fleet_controller.py" in code


def test_tracked_config_is_shadow_with_no_enforce_approval():
    import json

    cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
    assert cfg["mode"] == "shadow"
    assert cfg["approved_enforce_digest"] is None
    assert cfg["fleet_min_roots"] == 30
    assert cfg["max_trees_per_pass"] == 1
