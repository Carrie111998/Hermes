# Engineering Review Execution Workflow

Use this workflow every time so reports remain comparable across runs.

## Phase 0 — Scope and Baseline

1. Confirm the target repository path.
2. Record timestamp, platform, reviewer, repo root, branch, commit, and dirty status.
3. Identify whether the review is:
   - Full project review
   - Release readiness review
   - Subsystem review
   - Regression comparison against a previous report
4. Load the output template and review contract.

## Phase 1 — Evidence Collection

Run the collector if present:

```bash
python3 skills/software-development/engineering-review/scripts/collect_hermes_review_evidence.py --repo .
```

Consume stdout for the default read-only review. Only when the user asks for a
saved artifact, add `--output .hermes/reviews/evidence.json`. Use
`.hermes/reviews/` as the artifact directory so saved reviews follow the
existing Hermes workspace convention without creating a parallel registry.

Then gather targeted evidence:

- Git status and recent history
- Project tree and package layout
- Tests, linters, and build commands when feasible
- Key source files for agent loop, prompt builder, tool registry, gateway, cron, MCP, memory, skills, and config
- Documentation paths and generated docs status
- Runtime health commands if safe and available

## Phase 2 — Category Review

For each category in `engineering-checklist.md`:

1. Check existence of source files, tests, docs, and runtime controls.
2. Note positive controls first.
3. Identify findings only when evidence supports them.
4. Mark unverifiable controls as `Unknown — Manual Review Required`.
5. Assign category score or `Unknown`.

## Phase 3 — Cross-Cutting Analysis

Analyze issues across categories:

- Duplicated mechanisms
- Missing ownership or documentation
- Weak boundaries between runtime/config/profile/skills
- Observability and recovery gaps
- Release-risk clusters
- Technical debt that compounds over time

## Phase 4 — Scoring

1. Score each category using the deterministic scoring rule in the review contract.
2. Exclude unknown categories from the numeric weighted score.
3. Show unknown category count separately.
4. Record evidence quality for every category.
5. Assign release posture:
   - `PASS` — no Critical/High blockers and unknowns are acceptable
   - `REVIEW` — no Critical blockers, but High/Medium/Unknown items need tracking
   - `BLOCK` — Critical issue or severe High issue with release impact

## Phase 5 — Report Generation

Use `templates/engineering-review-report.md` exactly as the top-level shape.
Stable headings matter for historical comparison.

Every finding must include:

- Severity
- Evidence
- Root Cause
- Impact
- Recommendation

## Phase 6 — Verification

Before delivering:

- Re-check that each finding has evidence or explicit unknown status.
- Verify no raw secrets appear in the report.
- Verify the report separates positive findings from risks.
- Verify immediate actions are executable in 1-7 days.
- Verify medium-term roadmap is 2-8 weeks.
- Verify long-term roadmap is 1-2 quarters.
- If saved to disk, report the path and git status impact.

## Phase 7 — Optional Follow-Up

Only if the user asks for implementation:

- Convert top findings into a tracked implementation plan.
- Use `plan`, `test-driven-development`, or `requesting-code-review` as appropriate.
- Do not silently perform fixes as part of the audit.
