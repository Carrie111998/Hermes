"""Behavioral coverage for the pre-CS-01 WIP-debt quarantine."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

from hermes_cli import doctor as doctor_module


def _load_module(file_path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


suite_conftest = _load_module(
    Path(__file__).with_name("conftest.py"),
    "_cs18_suite_conftest",
)


EXPECTED_NODE_IDS = {
    "tests/gateway/test_telegram_restart_resume_policy.py::"
    "test_telegram_can_continue_interrupted_task_after_restart",
    "tests/run_agent/test_post_task_review_lifecycle.py::"
    "test_terminal_gate_rejects_active_todo",
    "tests/run_agent/test_post_task_review_lifecycle.py::"
    "test_terminal_gate_accepts_completed_task",
    "tests/run_agent/test_post_task_review_lifecycle.py::"
    "test_terminal_gate_rejects_active_process_and_delegation",
    "tests/run_agent/test_post_task_review_lifecycle.py::"
    "test_review_completion_is_claimed_once",
    "tests/run_agent/test_post_task_review_lifecycle.py::"
    "test_spawn_starts_once_for_duplicate_completion",
    "tests/run_agent/test_post_task_review_lifecycle.py::"
    "test_bounded_review_wait_refuses_active_turn",
}
DEBT_DOC = Path("docs/known_debt/PRE_CS01_WIP_DEBT.md")


class _CollectedItem:
    def __init__(self, nodeid: str):
        self.nodeid = nodeid
        self.markers = []

    def add_marker(self, marker) -> None:
        self.markers.append(marker)


def _marked_items(*node_ids: str) -> list[_CollectedItem]:
    items = [_CollectedItem(node_id) for node_id in node_ids]
    suite_conftest.pytest_collection_modifyitems(items)
    return items


def _load_test_module(file_path: Path):
    return _load_module(file_path, f"_cs18_{file_path.stem}")


def test_conftest_registers_exactly_seven_quarantined_node_ids():
    assert suite_conftest._PRE_CS01_WIP_DEBT_NODE_IDS == EXPECTED_NODE_IDS
    assert len(suite_conftest._PRE_CS01_WIP_DEBT_NODE_IDS) == 7


def test_all_seven_node_ids_resolve_to_real_test_functions_at_HEAD():
    modules = {}
    for node_id in EXPECTED_NODE_IDS:
        file_name, function_name = node_id.split("::", 1)
        module = modules.setdefault(file_name, _load_test_module(Path(file_name)))
        assert callable(getattr(module, function_name))


def test_conftest_uses_xfail_marker_not_skip():
    items = _marked_items(*EXPECTED_NODE_IDS)
    assert {marker.name for item in items for marker in item.markers} == {"xfail"}


def test_conftest_uses_strict_false_so_unexpected_pass_does_not_fail_suite():
    items = _marked_items(*EXPECTED_NODE_IDS)
    assert all(item.markers[0].kwargs["strict"] is False for item in items)


def test_conftest_reason_string_references_pre_cs01_wip_debt_doc():
    items = _marked_items(next(iter(EXPECTED_NODE_IDS)))
    reason = items[0].markers[0].kwargs["reason"]
    assert "docs/known_debt/PRE_CS01_WIP_DEBT.md" in reason


def test_quarantined_tests_report_XFAIL_not_FAIL_when_run():
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        *sorted(EXPECTED_NODE_IDS),
    ]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        command,
        cwd=Path(__file__).parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "7 xfailed" in output
    assert " failed" not in output


def test_no_other_test_is_marked_xfail_by_the_hook():
    unrelated = _marked_items("tests/test_programme_gate.py::test_initial_state")
    assert unrelated[0].markers == []


def test_docs_known_debt_pre_cs01_file_exists_and_names_the_seven_tests():
    assert DEBT_DOC.is_file()
    content = DEBT_DOC.read_text(encoding="utf-8")
    assert all(node_id in content for node_id in EXPECTED_NODE_IDS)


def test_docs_known_debt_pre_cs01_file_names_stash_ref_85cc4ddbe():
    content = DEBT_DOC.read_text(encoding="utf-8")
    assert "stash@{0}" in content
    assert "85cc4ddbe" in content
    assert "Do **not** run `git stash pop" in content


def test_hermes_doctor_output_contains_pre_cs01_wip_debt_line(capsys):
    doctor_module._report_pre_cs01_wip_debt()
    output = capsys.readouterr().out
    assert doctor_module._PRE_CS01_WIP_DEBT_LINE in output
    assert (
        "pre-CS01 WIP debt: 7 tests quarantined "
        "(docs/known_debt/PRE_CS01_WIP_DEBT.md)"
    ) in output
