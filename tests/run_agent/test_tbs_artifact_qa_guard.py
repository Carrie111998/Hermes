import pytest

from agent.tbs_artifact_qa_guard import (
    SYNTHETIC_FLAG,
    build_artifact_qa_nudge,
    has_passing_workbook_validator_evidence,
    response_needs_artifact_qa,
)
from agent.turn_finalizer import _drop_verification_continuation_scaffolding


def test_tbs_cfo_xlsx_completion_without_validator_continues():
    response = "Done — CFO cash-flow forecast workbook created: C:/work/forecast.xlsx"
    assert response_needs_artifact_qa(response, user_message="TBS client-facing finance forecast") is True
    assert build_artifact_qa_nudge(response, user_message="TBS client-facing finance forecast")


def test_tbs_cfo_xlsx_completion_with_validator_pass_can_finalize():
    response = "Done — CFO cash-flow forecast workbook created: C:/work/forecast.xlsx"
    recent = "python tbs_workbook_qa_validator.py C:/work/forecast.xlsx --project-forecast returned status: PASS"
    assert has_passing_workbook_validator_evidence(recent) is True
    assert response_needs_artifact_qa(response, user_message="TBS client-facing finance forecast", recent_messages=[{"role": "tool", "content": recent}]) is False


def test_not_ready_workbook_label_can_finalize():
    response = "C:/work/forecast.xlsx is NOT CLIENT-READY / MODEL INCOMPLETE because project rows are missing."
    assert response_needs_artifact_qa(response, user_message="TBS CFO forecast") is False


def test_generic_spreadsheet_is_not_blocked():
    response = "Done — created the grocery spreadsheet: C:/work/list.xlsx"
    assert response_needs_artifact_qa(response, user_message="personal spreadsheet") is False


def test_tbs_artifact_qa_scaffolding_is_dropped_from_live_history():
    messages = [
        {"role": "user", "content": "Build TBS CFO workbook"},
        {"role": "assistant", "content": "interim", SYNTHETIC_FLAG: True},
        {"role": "user", "content": "run validator", SYNTHETIC_FLAG: True},
        {"role": "assistant", "content": "final validator passed"},
    ]

    _drop_verification_continuation_scaffolding(messages)

    assert messages == [
        {"role": "user", "content": "Build TBS CFO workbook"},
        {"role": "assistant", "content": "final validator passed"},
    ]
