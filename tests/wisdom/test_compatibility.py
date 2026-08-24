from hermes_wisdom.compatibility import LocalCapabilities, evaluate
from hermes_wisdom.contract import (
    HermesRequirement,
    PluginRequirement,
    RuntimeRequirement,
    SystemSpecification,
    ToolRequirement,
)


def spec(**updates):
    values = {"hermes": HermesRequirement(minimum_version="0.1.0")}
    values.update(updates)
    return SystemSpecification(**values)


def local(**updates):
    values = {
        "hermes_version": "1.0.0",
        "os": "darwin",
        "architecture": "arm64",
        "runtime": {"shell": True, "browser": False, "code": True, "sandbox": True},
    }
    values.update(updates)
    return LocalCapabilities(**values)


def test_all_four_compatibility_outcomes_are_deterministic():
    assert evaluate(spec(), local()).outcome == "compatible"
    assert (
        evaluate(
            spec(
                tools=[
                    ToolRequirement(name="git", minimum_version="2", auto_install=False)
                ]
            ),
            local(),
        ).outcome
        == "compatible_after_setup"
    )
    assert (
        evaluate(spec(known_limitations=["manual replay only"]), local()).outcome
        == "partial"
    )
    assert (
        evaluate(spec(runtime=RuntimeRequirement(browser=True)), local()).outcome
        == "blocked_pending_action"
    )


def test_skill_evaluator_is_not_a_compatibility_input():
    result = evaluate(spec(), local())
    assert not hasattr(result, "skill_evaluator")
