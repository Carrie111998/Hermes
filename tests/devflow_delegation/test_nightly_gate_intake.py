import json

import pytest

from devflow_delegation.gate_report import AGENT_TARGET, INELIGIBLE_TARGET
from devflow_delegation.nightly_gate_intake import consume_reports


class FakeEmitter:
    def __init__(self):
        self.calls = []

    def delegate(self, **kwargs):
        self.calls.append(kwargs)
        from devflow_delegation.emitter import DelegationResult
        return DelegationResult("queued", request_id="dwr_fake", fingerprint="fp", reason="queued")


def _report(tmp_path, name, **over):
    payload = {"culprit": "pytest failed: 1 failed", "failed_command": "python -m pytest -q",
               "output": "FAILED tests/test_x.py::test_y", "failure_class": "pytest",
               "subsystem": "nightly-gate"}
    payload.update(over)
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_a_pytest_report_is_emitted_to_the_agent_target(tmp_path):
    _report(tmp_path, "report-1.json")
    em = FakeEmitter()
    assert consume_reports(tmp_path, em) == {"found": 1, "emitted": 1, "skipped": 0}
    assert em.calls[0]["target"]["repo"] == AGENT_TARGET


def test_a_non_agent_class_is_emitted_to_the_ineligible_target(tmp_path):
    _report(tmp_path, "report-1.json", failure_class="script-drift",
            culprit="script mirror drift: scripts/x.py differs")
    em = FakeEmitter()
    consume_reports(tmp_path, em)
    # Recorded as visible work, but the executor can never act on it.
    assert em.calls[0]["target"]["repo"] == INELIGIBLE_TARGET


def test_a_processed_report_is_marked_consumed_and_not_re_emitted(tmp_path):
    _report(tmp_path, "report-1.json")
    em = FakeEmitter()
    consume_reports(tmp_path, em)
    assert (tmp_path / "report-1.json.consumed").exists()
    assert not (tmp_path / "report-1.json").exists()
    assert consume_reports(tmp_path, em) == {"found": 0, "emitted": 0, "skipped": 0}
    assert len(em.calls) == 1


@pytest.mark.parametrize("body", ["{not json", '{"culprit": ""}', "[]", "null"])
def test_a_malformed_report_is_skipped_and_consumed_without_raising(tmp_path, body):
    (tmp_path / "report-1.json").write_text(body, encoding="utf-8")
    em = FakeEmitter()
    assert consume_reports(tmp_path, em) == {"found": 1, "emitted": 0, "skipped": 1}
    assert em.calls == []
    # Consumed so a poison report cannot be retried forever.
    assert (tmp_path / "report-1.json.consumed").exists()


def test_a_missing_directory_is_a_clean_no_op(tmp_path):
    em = FakeEmitter()
    assert consume_reports(tmp_path / "nope", em) == {"found": 0, "emitted": 0, "skipped": 0}


def test_the_limit_bounds_how_many_are_processed(tmp_path):
    for index in range(5):
        _report(tmp_path, f"report-{index}.json")
    em = FakeEmitter()
    result = consume_reports(tmp_path, em, limit=2)
    assert result["found"] == 2 and result["emitted"] == 2
    assert len(list(tmp_path.glob("report-*.json"))) == 3
