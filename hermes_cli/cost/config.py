"""Fixed CS-02 cost-tracking threshold values.

Routing-doctrine configuration is intentionally deferred to CS-05.  These
constants are kept in one module so tests and future configuration plumbing
can replace values without changing the ledger or threshold evaluator.

Normal runtime behavior treats these values as advisory. Explicit operator
kill controls and opt-in hard enforcement remain separate mechanisms.
"""

from hermes_cli.cost.vendors import ALLOWED_LANES, VENDORS

FX_RATE = 1.52
OPENROUTER_SURCHARGE = 0.055

GLOBAL_DAILY_CAP_AUD = 20.00
PER_TASK_CAP_AUD = 2.50
LANE_DAILY_CAPS_AUD = {
    "green_captains": 6.00,
    "dayroute": 5.00,
    "tihna": 4.00,
    "platform": 2.00,
    "reserve": 3.00,
}
ESCALATION_DAILY_CAP_AUD = 3.00

VALID_LANES = frozenset(ALLOWED_LANES)
VALID_VENDORS = frozenset(VENDORS)

__all__ = [
    "ESCALATION_DAILY_CAP_AUD",
    "FX_RATE",
    "GLOBAL_DAILY_CAP_AUD",
    "LANE_DAILY_CAPS_AUD",
    "OPENROUTER_SURCHARGE",
    "PER_TASK_CAP_AUD",
    "VALID_LANES",
    "VALID_VENDORS",
]
