import pytest

from devflow_delegation.gate_report import (
    AGENT_TARGET,
    INELIGIBLE_TARGET,
    route_target,
    validate_report,
)


@pytest.mark.parametrize("failure_class", ["pytest", "ruff"])
def test_agent_capable_classes_route_to_the_agent_target(failure_class):
    assert route_target(failure_class) == AGENT_TARGET


@pytest.mark.parametrize("failure_class", [
    "script-drift", "twin-drift", "tracked-ignored", "pin", "bye",
    "unknown", "", "something-new-added-later",
])
def test_everything_else_routes_to_the_ineligible_target(failure_class):
    # Fail-safe by default: a class nobody has taught the router about must
    # never reach the agent. A new gate check added later lands here.
    assert route_target(failure_class) == INELIGIBLE_TARGET


def test_validate_report_accepts_a_complete_report():
    payload = {"culprit": "pytest failed", "failed_command": "pytest -q",
               "output": "boom", "failure_class": "pytest"}
    assert validate_report(payload) == payload


@pytest.mark.parametrize("payload", [
    {"failed_command": "pytest -q"},
    {"culprit": "pytest failed"},
    {"culprit": "", "failed_command": "pytest -q"},
    {"culprit": "pytest failed", "failed_command": "   "},
    [], None, "not a dict", 5,
])
def test_validate_report_rejects_anything_incomplete(payload):
    # The adapter declines without both fields; rejecting here keeps a
    # malformed report from consuming an emitter round-trip.
    assert validate_report(payload) is None
