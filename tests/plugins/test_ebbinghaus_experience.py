"""Tests for Ebbinghaus AGIASI experiential-memory extensions.

All store I/O uses tmp_path / temporary HERMES_HOME. Never open the live
operator database under ~/.hermes.
"""

from __future__ import annotations

import pytest

from plugins.memory.ebbinghaus.models import (
    AccessState,
    BeliefStatus,
    InsightStatus,
    RetrievalOutcome,
)
from plugins.memory.ebbinghaus.policies import EbbinghausPolicies, PolicyConfigError


def test_experience_policy_defaults_are_backward_compatible():
    policies = EbbinghausPolicies()
    assert policies.experience.enabled is False
    assert policies.experience.functional_forgetting is True
    assert policies.revision.enabled is True
    assert policies.insight.require_validation is True


def test_experience_policy_rejects_invalid_threshold_order():
    with pytest.raises(PolicyConfigError):
        EbbinghausPolicies.from_config(
            {
                "experience": {
                    "latent_retention_threshold": 0.05,
                    "archive_retention_threshold": 0.10,
                }
            }
        )


def test_experience_enums_have_stable_wire_values():
    assert AccessState.LATENT.value == "latent"
    assert BeliefStatus.SUPERSEDED.value == "superseded"
    assert RetrievalOutcome.RESCUED.value == "rescued"
    assert InsightStatus.REJECTED.value == "rejected"
