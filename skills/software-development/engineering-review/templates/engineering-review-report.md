# Engineering Review Report

Report Schema Version: `1.1`
Review Contract Version: `<contract version>`
Collector Version: `<collector version or not run>`

Review ID: `<YYYY-MM-DD>-<repo>-engineering-review`
Reviewer: `<agent/user>`
Date: `<YYYY-MM-DD>`
Repository: `<path>`
Branch: `<branch>`
Commit: `<sha>`
Dirty Worktree: `<yes/no + summary>`
Review Type: `<full/release/subsystem/regression>`
Release Posture: `PASS / REVIEW / BLOCK / UNKNOWN`

## 1. Executive Summary

- Overall verdict:
- Most important strength:
- Most important risk:
- Release decision:
- Unknowns requiring manual review:

## 2. Engineering Health Score

Overall Score: `<0-100 or Unknown>`
Confidence: `<High/Medium/Low>`
Scoring Notes:
- Scoring method: deterministic severity subtraction from `references/review-contract.md`
- Unknown categories excluded from weighted average:
- Commands/tests used:
- Key evidence limitations:

## 3. Category Scores

| Category | Score | Status | Evidence Quality | Key Evidence | Notes |
|---|---:|---|---|---|---|
| Architecture |  |  |  |  |  |
| Source Code |  |  |  |  |  |
| Configuration |  |  |  |  |  |
| Runtime |  |  |  |  |  |
| Prompt System |  |  |  |  |  |
| Profiles |  |  |  |  |  |
| Skills |  |  |  |  |  |
| Memory |  |  |  |  |  |
| AI-Vault / Knowledge Pipeline |  |  |  |  |  |
| Gateway |  |  |  |  |  |
| Scheduler / Cron |  |  |  |  |  |
| MCP |  |  |  |  |  |
| Docker / Deployment |  |  |  |  |  |
| Documentation |  |  |  |  |  |

## 4. Technical Debt Assessment

| Debt Area | Evidence | Severity | Compounding Risk | Recommended Handling |
|---|---|---|---|---|
|  |  |  |  |  |

## 5. Risk Matrix

| Finding ID | Severity | Likelihood | Impact | Risk Level | Release Effect |
|---|---|---|---|---|---|
|  |  |  |  |  |  |

## 6. Positive Findings

| ID | Category | Evidence | Why It Matters |
|---|---|---|---|
|  |  |  |  |

## 7. Findings

### ER-<CATEGORY>-<NNN> — <Title>

- Severity:
- Category:
- Evidence:
- Root Cause:
- Impact:
- Recommendation:
- Owner:
- Status:

## 8. Unknowns — Manual Review Required

| ID | Category | Attempted Evidence | Blocker | Recommended Manual Check |
|---|---|---|---|---|
|  |  |  |  |  |

## 9. Top Priority Improvements

| Rank | Improvement | Why Now | Evidence | Expected Outcome |
|---:|---|---|---|---|
| 1 |  |  |  |  |
| 2 |  |  |  |  |
| 3 |  |  |  |  |

## 10. Immediate Actions (1-7 days)

- [ ] Action:
  - Owner:
  - Evidence:
  - Done when:

## 11. Medium-Term Roadmap (2-8 weeks)

- [ ] Initiative:
  - Rationale:
  - Dependencies:
  - Success metric:

## 12. Long-Term Roadmap (1-2 quarters)

- [ ] Theme:
  - Strategic value:
  - Risks reduced:
  - Milestones:

## 13. Evidence Appendix

### Commands Run

```text
<command + exit code + short output>
```

### Files Reviewed

| File | Lines / Scope | Purpose |
|---|---|---|
|  |  |  |

### Tests / Builds

| Command | Exit Code | Result | Notes |
|---|---:|---|---|
|  |  |  |  |

### Evidence Snapshot

- Evidence JSON path:
- Collector schema version:
- Collector version:
- Collection timestamp:
- Suggested review commands considered:
