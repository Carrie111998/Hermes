"""Run the behavioral scenarios as ordinary pytest cases so CI enforces them."""
from __future__ import annotations

import pytest

from evals.behavioral.scenarios import SCENARIOS

AUTOMATED = [s for s in SCENARIOS if s.check is not None]
MODEL_DEPENDENT = [s for s in SCENARIOS if s.check is None]


@pytest.mark.parametrize("scenario", AUTOMATED, ids=lambda s: s.key)
def test_behavioral_scenario(scenario):
    scenario.check()


@pytest.mark.parametrize("scenario", MODEL_DEPENDENT, ids=lambda s: s.key)
def test_model_dependent_scenario_is_declared(scenario):
    """Model-dependent gaps must stay visible, with a stated reason.

    Recording them as skips-with-a-note keeps the coverage gap auditable
    instead of letting it vanish from the suite entirely.
    """
    assert scenario.requires_model
    assert scenario.notes, f"{scenario.key} needs a note explaining what it would take"
    pytest.skip(f"needs a model in the loop: {scenario.notes}")
