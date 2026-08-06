---
title: Privileged model policy
sidebar_label: Privileged model policy
---

# Privileged model policy

Hermes can mechanically block sensitive legal and finance workloads from using model routes that are not approved for privileged content.

System prompts are not enough for this class of risk. A legal or finance prompt can be routed by a cron model override, a gateway/session override, a profile default, or a fallback route. The policy must fail closed at the routing boundary.

## Protected workload tags

The current enforced path covers cron jobs tagged or inferred as:

- `legal` — legal, Zheng, HHS, CBCA, settlement, mediation, privileged, attorney, counsel.
- `finance` — finance, accounting, QuickBooks/QBO, books, reconciliation, journal entries, USTC, USTI, JHJ, PII.

Cron detection looks at job name, prompt, skills, workdir, and profile. It intentionally errs toward false positives: restricting a benign cron job is cheaper than leaking privileged content.

## Allowlist

Privileged agent-driven jobs may use only reviewed routes:

- `openrouter` + `nvidia/nemotron-3-ultra-550b-a55b:free`
- `openrouter` + `nvidia/nemotron-3-super-120b-a12b:free`
- `nvidia` + `nvidia/nemotron-3-ultra-550b-a55b:free`
- `nvidia` + `nvidia/nemotron-3-super-120b-a12b:free`
- `ollama` + `local`
- `local` + `local`

Privileged no-agent cron jobs are allowed because they run scripts directly and do not send prompts to an LLM.

Privileged cron jobs with no explicit `model`, `provider`, or `base_url` override are allowed to inherit the deployment default. Operators should still pin high-sensitivity jobs explicitly to an allowlisted route.

## Denylist

These are explicitly blocked for privileged cron jobs:

- `openrouter/owl-alpha`
- `owl-alpha`

The denylist is substring-based after normalization so aliases and prefixed OpenRouter model identifiers are still caught.

## Enforced cron path

Cron creation and update call `cron.model_policy.enforce_privileged_model_route()` before persisting changes.

Examples:

```python
from cron.jobs import create_job

# Fails closed: legal prompt + denied Owl model.
create_job(
    prompt="Review Zheng settlement posture",
    schedule="30m",
    provider="openrouter",
    model="openrouter/owl-alpha",
)

# Allowed: accounting skill + allowlisted Nemotron route.
create_job(
    prompt="Run USTC QBO check",
    schedule="30m",
    skills=["accounting-department"],
    provider="openrouter",
    model="nvidia/nemotron-3-ultra-550b-a55b:free",
)
```

## Dry-run evidence

The regression tests demonstrate the enforced path:

```bash
python -m pytest tests/cron/test_jobs.py::TestPrivilegedModelPolicy -q -o 'addopts='
```

Expected result:

```text
5 passed
```

The test cases verify:

1. Legal and finance tags are detected from job metadata.
2. A Zheng legal job using `openrouter/owl-alpha` raises `ValueError` and is not saved.
3. An accounting job using Nemotron free is saved.
4. Updating a privileged job to a non-allowlisted custom provider raises `ValueError` and preserves the previous route.
5. A non-privileged public coding job can still use Owl.

## Recommended profile pattern

For higher assurance, run legal/accounting workers under dedicated profiles with defaults set to allowlisted routes:

```yaml
model:
  provider: openrouter
  default: nvidia/nemotron-3-ultra-550b-a55b:free
```

Then assign legal/finance kanban lanes and scheduled jobs to those profiles. This avoids changing the interactive CEO default while still keeping privileged lanes inside reviewed model routes.

## Remaining gaps

This first implementation enforces cron job creation/update. Additional routing boundaries should apply the same `cron.model_policy` logic or an equivalent shared helper:

- gateway channel/session model overrides for legal/accounting channels;
- kanban per-task model overrides for legal/accounting profiles or skills;
- fallback provider chains when a privileged tag is active;
- profile template lint before gateway restart.
