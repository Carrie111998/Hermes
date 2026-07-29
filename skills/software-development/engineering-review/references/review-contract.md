# Engineering Review Contract

Contract Version: `1.1`

This contract defines the stable output semantics for Hermes Engineering Review.
Keep it backward-compatible unless the skill major version changes.

## Finding Schema

Every finding MUST use this schema:

| Field | Required | Description |
|---|:---:|---|
| ID | Yes | Stable ID: `ER-<CATEGORY>-<NNN>` |
| Category | Yes | Architecture, Source Code, Runtime, Gateway, etc. |
| Severity | Yes | Critical, High, Medium, Low, Info, Unknown |
| Title | Yes | One-line finding title |
| Evidence | Yes | Command output, file path + line range, test result, or explicit unknown |
| Root Cause | Yes | Why the issue exists; if unknown, say so |
| Impact | Yes | Operational, security, maintainability, correctness, cost, or user impact |
| Recommendation | Yes | Concrete next action |
| Owner | Optional | Suggested owner or subsystem |
| Status | Optional | Open, Accepted, Mitigated, Resolved, Manual Review Required |

## Severity Taxonomy

| Severity | Meaning | Release posture |
|---|---|---|
| Critical | Evidence of data loss, secret exposure, broken core runtime, or unsafe destructive behavior | BLOCK |
| High | Major reliability/security/maintainability risk with likely user impact | BLOCK or REVIEW |
| Medium | Important issue that should be scheduled and tracked | REVIEW |
| Low | Localized improvement or minor maintainability issue | PASS with action |
| Info | Positive finding, observation, or non-risk context | PASS |
| Unknown | Evidence could not be safely or completely established | Manual Review Required |

## Engineering Health Score

Use a 0-100 score, but never present it as mathematically precise. It is a
repeatable engineering indicator, not a scientific measurement.

Deterministic category scoring rule:

1. Start each reviewed category at `100`.
2. Subtract for open findings in that category:
   - Critical: `-40`
   - High: `-25`
   - Medium: `-12`
   - Low: `-4`
   - Unknown: no numeric penalty, but reduce evidence quality and list separately.
3. Clamp the category score to `0-100`.
4. If the category has no direct source, test, docs, command, or collector evidence, set score to `Unknown` instead of guessing.
5. Calculate the overall score as the weighted average of non-Unknown categories.
6. Report `Unknown category count` next to the numeric score.

Suggested weighting:

| Category | Weight |
|---|---:|
| Architecture | 10 |
| Source Code | 10 |
| Configuration | 8 |
| Runtime | 8 |
| Prompt System | 8 |
| Profiles | 6 |
| Skills | 8 |
| Memory | 6 |
| AI-Vault / Knowledge Pipeline | 6 |
| Gateway | 8 |
| Scheduler / Cron | 6 |
| MCP | 6 |
| Docker / Deployment | 5 |
| Documentation | 5 |

Category scoring guide:

| Score | Meaning |
|---:|---|
| 90-100 | Strong controls, clear ownership, tested, observable |
| 75-89 | Healthy with manageable gaps |
| 60-74 | Functional but with notable technical debt or evidence gaps |
| 40-59 | Fragile or poorly controlled |
| 0-39 | Release-blocking or uncontrolled |
| Unknown | Evidence insufficient; exclude from numeric average and list separately |

## Risk Matrix

Classify each non-info finding by likelihood and impact:

| Likelihood | Definition |
|---|---|
| High | Current evidence suggests the issue is active or likely to recur |
| Medium | Plausible under common operating conditions |
| Low | Requires unusual conditions or manual misuse |
| Unknown | Evidence insufficient |

| Impact | Definition |
|---|---|
| High | User-visible outage, data loss, security exposure, or release blockage |
| Medium | Degraded reliability, confusing behavior, or growing maintenance cost |
| Low | Local inconvenience or cleanup work |
| Unknown | Evidence insufficient |

## Evidence Standard

A finding is valid only if at least one evidence item is present:

- File path and relevant line range
- Command run and output summary
- Test/lint/build result with exit code
- Git status/diff/revision information
- Official docs/source reference
- Runtime health check output
- Collector output field path, for example `evidence.inventory.test_files`
- Explicit `Unknown — Manual Review Required`

Evidence quality labels:

| Label | Meaning |
|---|---|
| High | Direct source/test/runtime evidence supports the finding |
| Medium | Multiple indirect signals support the finding, but no live runtime check was run |
| Low | Single indirect signal; recommendation should be conservative |
| Unknown | Evidence insufficient or unavailable |

## Unknown Handling

Unknowns are not failures by themselves. They are control gaps.

Format unknowns like this:

```text
Severity: Unknown
Evidence: Unknown — Manual Review Required. Attempted checks: <list>. Blocker: <why unavailable>.
Root Cause: Unknown — Manual Review Required.
Impact: Unknown; possible impact area: <area>.
Recommendation: Run <specific manual check> or add <specific observability/test>.
```
