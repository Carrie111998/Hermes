"""Verify the async batch formatter surfaces per-task model overrides.

Regression guard for a real gap: the batch completion block prints a single
batch-level "Model:" line, which silently misreports a MIXED-tier batch. The
per-task override must appear on the task that carries it.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes_fmt_test_")

import tools.process_registry as pr  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


# Locate the formatter that renders a completed async delegation event.
fmt = None
for cand in ("_format_async_delegation", "_format_delegation_completion",
             "format_delegation_completion", "_format_completion_event"):
    if hasattr(pr, cand):
        fmt = getattr(pr, cand)
        print(f"(using formatter: {cand})")
        break

if fmt is None:
    names = [n for n in dir(pr) if "format" in n.lower()]
    print("Could not find formatter. Candidates:", names)
    sys.exit(2)

evt = {
    "delegation_id": "deleg_test",
    "is_batch": True,
    "role": "leaf",
    "model": "anthropic/claude-sonnet-5",
    "status": "completed",
    "total_duration_seconds": 4.75,
    "goals": ["mechanical task", "judgment task", "default task"],
    "results": [
        {
            "task_index": 0,
            "status": "completed",
            "summary": "alpha",
            "api_calls": 1,
            "duration_seconds": 1.8,
            "model_override": "anthropic/claude-haiku-4.5",
        },
        {
            "task_index": 1,
            "status": "completed",
            "summary": "gamma",
            "api_calls": 1,
            "duration_seconds": 2.0,
            "model_override": "anthropic/claude-opus-5",
            "provider_override": "nous",
        },
        {
            "task_index": 2,
            "status": "completed",
            "summary": "beta",
            "api_calls": 1,
            "duration_seconds": 4.3,
        },
    ],
}

try:
    out = fmt(evt)
except TypeError:
    out = fmt(evt, None)

print("\n----- rendered -----")
print(out)
print("--------------------\n")

check("haiku override shown on task 1", "anthropic/claude-haiku-4.5" in out)
check("opus override shown on task 2", "anthropic/claude-opus-5" in out)
check("provider override shown", "@nous" in out)
check("task 3 (no override) not mislabelled",
      out.count("model=") == 2, f"model= count {out.count('model=')}")
check("all three summaries present",
      all(w in out for w in ("alpha", "gamma", "beta")))

# Failure surfacing: an override that could not resolve must be visible.
evt_err = {
    "delegation_id": "deleg_test2",
    "is_batch": True,
    "role": "leaf",
    "model": "anthropic/claude-sonnet-5",
    "status": "completed",
    "goals": ["t"],
    "results": [{
        "task_index": 0, "status": "completed", "summary": "ok", "api_calls": 1,
        "model_override": "bogus/model",
        "model_override_error": "per-task model override 'bogus/model' failed to resolve (nope); used the configured delegation model instead",
    }],
}
try:
    out2 = fmt(evt_err)
except TypeError:
    out2 = fmt(evt_err, None)
check("override failure surfaced to the caller", "failed to resolve" in out2)

# Backward compatibility: a batch with no overrides must render unchanged.
evt_plain = {
    "delegation_id": "deleg_test3",
    "is_batch": True,
    "role": "leaf",
    "model": "anthropic/claude-sonnet-5",
    "status": "completed",
    "goals": ["a", "b"],
    "results": [
        {"task_index": 0, "status": "completed", "summary": "one", "api_calls": 1},
        {"task_index": 1, "status": "completed", "summary": "two", "api_calls": 1},
    ],
}
try:
    out3 = fmt(evt_plain)
except TypeError:
    out3 = fmt(evt_plain, None)
check("no-override batch has no model= noise", "model=" not in out3)
check("no-override batch still renders summaries",
      "one" in out3 and "two" in out3)

print()
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL FORMATTER TESTS PASSED")
