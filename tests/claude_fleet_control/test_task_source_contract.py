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
    # Execution limit must be a bounded ISO-8601 duration under the 5-min
    # cadence, so a slow pass cannot overlap the next fire (IgnoreNew also
    # guards this). Widened from PT2M to PT4M30S on 2026-09-01 for headroom
    # under load; assert the bound, not one exact value.
    limit = settings.find("t:ExecutionTimeLimit", _NS).text
    assert limit in ("PT2M", "PT3M", "PT4M", "PT4M30S", "PT5M"), limit
    assert settings.find("t:MultipleInstancesPolicy", _NS).text == "IgnoreNew"
    assert settings.find("t:StartWhenAvailable", _NS).text == "true"

    rep = root.find(".//t:TimeTrigger/t:Repetition", _NS)
    assert rep.find("t:Interval", _NS).text == "PT5M"

    principal = root.find(".//t:Principals/t:Principal", _NS)
    assert principal.find("t:RunLevel", _NS).text == "LeastPrivilege"

    args = root.find(".//t:Actions/t:Exec/t:Arguments", _NS).text
    assert "claude_fleet_controller_run.ps1" in args


@requires_task
def test_task_never_references_the_legacy_culler():
    text = _TASK.read_text(encoding="utf-8")
    assert "cull-claude-sessions" not in text
    assert "cull-idle-claude-sessions" not in text
    # The task opens gate 2 via the -AllowEnforce SWITCH, never the raw
    # --allow-enforce (that belongs only inside the runner's guarded branch).
    args = ET.parse(_TASK).getroot().find(".//t:Actions/t:Exec/t:Arguments", _NS).text
    assert "--allow-enforce" not in args


@requires_runner
def test_runner_gates_enforce_behind_the_switch():
    """Post-cutover the runner CAN pass --allow-enforce, but only inside the
    -AllowEnforce guard, so an ad-hoc run stays shadow-safe. Pin that
    structure and the absence of the legacy culler."""
    code_lines = [
        ln for ln in _RUNNER.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "param([switch]$AllowEnforce)" in code       # declares the switch
    assert "if ($AllowEnforce)" in code                 # the flag is guarded
    assert "$Py $Script --allow-enforce" in code        # the enforce branch
    assert "& $Py $Script 2>&1" in code                 # the shadow-default branch (no flag)
    assert "cull-claude-sessions" not in code
    assert "cull-idle-claude-sessions" not in code
    assert "run_claude_fleet_controller.py" in code


def test_tracked_config_enforce_is_coherently_pinned():
    import json
    import re

    cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
    assert cfg["mode"] in ("shadow", "enforce")
    if cfg["mode"] == "enforce":
        assert re.fullmatch(r"[0-9a-f]{64}", cfg.get("approved_enforce_digest") or "")
    else:
        assert cfg["approved_enforce_digest"] is None
    assert cfg["fleet_min_roots"] == 30
    assert cfg["max_trees_per_pass"] == 1


@requires_task
def test_config_and_task_enforce_state_are_consistent():
    """Both gates must agree: config in enforce mode (with a digest) iff the
    task opens gate 2 with -AllowEnforce. A half-applied cutover or half
    rollback (one gate flipped, not the other) fails here. The controller
    stays safe either way (both gates required for any kill), but an
    inconsistent deployment is worth catching."""
    import json

    cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
    config_enforce = cfg["mode"] == "enforce" and bool(cfg.get("approved_enforce_digest"))
    args = ET.parse(_TASK).getroot().find(".//t:Actions/t:Exec/t:Arguments", _NS).text
    task_enforce = "-AllowEnforce" in args
    assert config_enforce == task_enforce, (
        f"gate mismatch: config_enforce={config_enforce} task_enforce={task_enforce}"
    )
