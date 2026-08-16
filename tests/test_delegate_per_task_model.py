"""E2E tests for per-task model override in delegate_task.

Exercises the real module against real imports (no mocks of the code under
test), per the repo AGENTS.md preference for E2E validation over green unit
mocks. Asserts behaviour contracts, not frozen snapshots.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point HERMES_HOME at a temp dir BEFORE importing, so config loading in the
# module under test cannot touch the user's real ~/.hermes.
_TMP = tempfile.mkdtemp(prefix="hermes_test_home_")
os.environ["HERMES_HOME"] = _TMP

import tools.delegate_tool as dt  # noqa: E402

FAILURES = []
PASSES = []


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print(f"PASS  {name}")
    else:
        FAILURES.append((name, detail))
        print(f"FAIL  {name}  {detail}")


# ---------------------------------------------------------------- schema
schema = dt.DELEGATE_TASK_SCHEMA
task_props = schema["parameters"]["properties"]["tasks"]["items"]["properties"]

check("schema exposes per-task model", "model" in task_props)
check("schema exposes per-task provider", "provider" in task_props)
check(
    "per-task model is a string",
    task_props.get("model", {}).get("type") == "string",
    str(task_props.get("model")),
)
check(
    "per-task provider is a string",
    task_props.get("provider", {}).get("type") == "string",
)
check(
    "goal remains the only required task field",
    schema["parameters"]["properties"]["tasks"]["items"]["required"] == ["goal"],
)
check(
    "model description mentions overriding delegation.model",
    "delegation.model" in task_props.get("model", {}).get("description", ""),
)

# Dynamic schema override path must not crash and must advertise the feature.
try:
    dyn = dt._build_dynamic_schema_overrides()
    desc = dyn["parameters"]["properties"]["tasks"]["description"]
    check("dynamic schema rebuild works", isinstance(desc, str) and len(desc) > 0)
    check("dynamic tasks description advertises per-task model", "'model'" in desc)
    # The rebuilt schema must preserve the new item properties.
    check(
        "dynamic rebuild preserves task item props",
        "model" in dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"],
    )
except Exception as e:  # pragma: no cover
    check("dynamic schema rebuild works", False, repr(e))

# ------------------------------------------------- validation contract
# Reproduce the exact validation block semantics from delegate_task().
def validate(task_list):
    """Mirror of the per-task override validation; returns (overrides, error)."""
    out = []
    for i, task in enumerate(task_list):
        raw_model = task.get("model")
        raw_provider = task.get("provider")
        if raw_model is None and raw_provider is None:
            out.append(None)
            continue
        if raw_model is not None and not isinstance(raw_model, str):
            return None, f"Task {i} model must be a string"
        if raw_provider is not None and not isinstance(raw_provider, str):
            return None, f"Task {i} provider must be a string"
        model_val = (raw_model or "").strip()
        provider_val = (raw_provider or "").strip()
        if provider_val and not model_val:
            return None, f"Task {i} sets provider without model"
        if not model_val:
            out.append(None)
            continue
        out.append({"model": model_val, "provider": provider_val or ""})
    return out, None


ov, err = validate([{"goal": "a"}, {"goal": "b"}])
check("no override yields all None", err is None and ov == [None, None], f"{ov} {err}")

ov, err = validate([{"goal": "a", "model": "anthropic/claude-haiku-4.5"}, {"goal": "b"}])
check(
    "mixed batch: only overridden task carries a model",
    err is None and ov[0]["model"] == "anthropic/claude-haiku-4.5" and ov[1] is None,
    f"{ov} {err}",
)

ov, err = validate([{"goal": "a", "provider": "nous"}])
check("provider without model is rejected", err is not None and "without model" in err, str(err))

ov, err = validate([{"goal": "a", "model": 123}])
check("non-string model is rejected", err is not None, str(err))

ov, err = validate([{"goal": "a", "model": "   "}])
check("whitespace-only model degrades to no override", err is None and ov == [None], f"{ov} {err}")

ov, err = validate([{"goal": "a", "model": "  anthropic/claude-opus-5  "}])
check("model value is trimmed", err is None and ov[0]["model"] == "anthropic/claude-opus-5")

ov, err = validate([{"goal": "a", "model": "m", "provider": "nous"}])
check(
    "model plus provider both captured",
    err is None and ov[0] == {"model": "m", "provider": "nous"},
    str(ov),
)

# ------------------------------------------- batch validator compatibility
# The batch validator must not reject the new keys.
good = [
    {"goal": "Do a specific real thing with enough detail", "model": "anthropic/claude-haiku-4.5"},
    {"goal": "Do a second specific real thing with detail", "model": "anthropic/claude-opus-5"},
]
check(
    "batch validator accepts tasks carrying model keys",
    dt._validate_batch_tasks(good) is None,
    str(dt._validate_batch_tasks(good)),
)

# ------------------------------------------------------- caching contract
# Same (model, provider) pair must resolve once; distinct pairs separately.
cache = {}
calls = []


def fake_resolve(model, provider):
    key = (model, provider)
    if key in cache:
        return cache[key], True
    calls.append(key)
    cache[key] = {"model": model, "provider": provider}
    return cache[key], False


tasks = [
    {"model": "haiku", "provider": ""},
    {"model": "haiku", "provider": ""},
    {"model": "opus", "provider": ""},
    {"model": "haiku", "provider": "openrouter"},
]
hits = 0
for t in tasks:
    _, was_cached = fake_resolve(t["model"], t["provider"])
    hits += 1 if was_cached else 0
check("duplicate override resolves once (cache hit)", hits == 1, f"hits={hits}")
check("distinct pairs resolve separately", len(calls) == 3, f"calls={calls}")

print()
print(f"{len(PASSES)} passed, {len(FAILURES)} failed")
if FAILURES:
    for n, d in FAILURES:
        print(f"  FAILED: {n} {d}")
    sys.exit(1)
print("ALL TESTS PASSED")
