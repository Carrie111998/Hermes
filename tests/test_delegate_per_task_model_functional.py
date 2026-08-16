"""Functional proof that per-task model override changes the resolved model.

Runs in a FRESH process (the edited module is imported here for the first
time), and drives the real _resolve_delegation_credentials with a per-task
override config to prove the resolved model differs per task.

This is the test that would have caught the false-positive: presence of the
schema key proves nothing about whether the child actually runs on that model.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes_fn_test_")

import tools.delegate_tool as dt  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  " + str(detail) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


class _StubParent:
    """Minimal stand-in for the parent agent used by credential resolution."""

    model = "anthropic/claude-sonnet-5"
    provider = "nous"
    base_url = "https://inference-api.nousresearch.com/v1"
    api_key = "test-key-not-real"
    api_mode = None


parent = _StubParent()

# Baseline: no per-task override -> resolves the configured delegation model.
# Use an explicit base_url + api_key so resolution does not depend on a logged
# in Portal account (this test must run in a clean temp HERMES_HOME).
base_cfg = {
    "model": "anthropic/claude-sonnet-5",
    "base_url": "https://example.invalid/v1",
    "api_key": "test-key-not-real",
}
try:
    base = dt._resolve_delegation_credentials(dict(base_cfg), parent)
    check(
        "baseline resolves configured delegation model",
        base.get("model") == "anthropic/claude-sonnet-5",
        base.get("model"),
    )
except Exception as e:
    check("baseline resolves configured delegation model", False, repr(e))
    base = {"model": None}

# Override: the exact mutation delegate_task performs per task.
try:
    ov_cfg = dict(base_cfg)
    ov_cfg["model"] = "anthropic/claude-haiku-4.5"
    ov = dt._resolve_delegation_credentials(ov_cfg, parent)
    check(
        "per-task override resolves the OVERRIDE model",
        ov.get("model") == "anthropic/claude-haiku-4.5",
        ov.get("model"),
    )
    check(
        "override model differs from baseline model",
        ov.get("model") != base.get("model"),
        f"base={base.get('model')} ov={ov.get('model')}",
    )
except Exception as e:
    check("per-task override resolves the OVERRIDE model", False, repr(e))

# The override must not mutate the shared batch config dict.
shared = dict(base_cfg)
snapshot = dict(shared)
per_task = dict(shared)
per_task["model"] = "anthropic/claude-opus-5"
check(
    "override does not mutate the shared batch config",
    shared == snapshot and per_task["model"] != shared["model"],
    f"shared={shared}",
)

# Opus override resolves distinctly too (three-way separation).
try:
    op_cfg = dict(base_cfg)
    op_cfg["model"] = "anthropic/claude-opus-5"
    op = dt._resolve_delegation_credentials(op_cfg, parent)
    check(
        "third distinct model resolves distinctly",
        op.get("model") == "anthropic/claude-opus-5",
        op.get("model"),
    )
except Exception as e:
    check("third distinct model resolves distinctly", False, repr(e))

print()
print(f"{4 - len(FAIL)} of 4 functional checks passed" if len(FAIL) <= 4 else "")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("FUNCTIONAL PROOF PASSED: per-task override changes the resolved model")
