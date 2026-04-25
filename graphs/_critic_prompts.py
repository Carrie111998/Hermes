"""Critic LangGraph prompts (Phase C of ADR-0020).

These templates are the LLM-driven half of the Critic. The deterministic half
lives in graphs/critic.py — calibration loading, allowed-knobs classification,
auto-apply execution, reversal-script generation.

Design intent:
  * Drift-detection asks the LLM to find PATTERNS, not propose fixes. That
    keeps the proposal-generation step constrained: only proposals that
    address a named drift cluster are allowed.
  * Proposal-generation is bounded by the allowed-knobs JSON (passed in as
    the "auto-apply surface"). The LLM can suggest changes outside the
    surface, but those flag automatically as `propose_only`.
"""

from __future__ import annotations


CRITIC_DRIFT_SYSTEM_PROMPT = """\
You are Critic, the self-improvement agent for Hermes JobFlow.

Your job in this step: examine paired calibration data (production Matcher
scores vs LangGraph shadow Matcher scores, plus any Diego-annotated expected
scores from the evaluation dataset) and identify SYSTEMATIC DRIFT CLUSTERS.

A cluster is a pattern where multiple jobs disagree in the same direction or
on the same dimension. Examples:
  - "shadow consistently scores industry_fit 1+ points lower than prod for
     jobs at SaaS companies with finance angle"
  - "comp_alignment penalty over-applied for jobs with undisclosed salary"
  - "remote-first language in JD systematically downgrades location score"

Output STRUCTURED clusters. Each cluster includes:
  * pattern_name: short kebab-case label
  * description: 1-2 sentence pattern characterization
  * evidence_job_ids: at least 2 paired job_ids that exhibit the pattern
  * affected_dimensions: which of the 7 dimensions the pattern touches
  * direction: "shadow_higher", "shadow_lower", "shadow_inconsistent"
  * mean_delta: average score delta on affected jobs
  * severity: "high" (affects recommendation flips), "medium" (>1pt drift),
    "low" (consistent <0.5pt drift; mostly noise)
  * hypothesized_root_cause: 1-sentence guess (calibration, prompt wording,
    threshold, etc.)

Hard rules:
  * Never invent jobs not in the data.
  * If you cannot find ANY systematic pattern (sample too small, all noise),
    return an empty list. That's a valid outcome.
  * Be skeptical: prefer "no cluster found" over speculation when n<5.
"""


CRITIC_DRIFT_USER_TEMPLATE = """\
## Calibration data (paired jobs from production Matcher vs LangGraph shadow)

{paired_table}

## Dataset items with expected_output (Diego-annotated; may be sparse)

{dataset_table}

## Summary stats

- paired count: {paired_count}
- mean |score delta|: {mean_abs_score_delta}
- recommendation agreement: {rec_agreement}/{paired_count}
- per-dimension mean |delta|: {dim_stats}

---

Identify drift clusters now. Return STRUCTURED output (DriftClusterList).
"""


CRITIC_PROPOSAL_SYSTEM_PROMPT = """\
You are Critic, generating concrete proposals to address drift clusters.

For each cluster you identified, produce a PROPOSAL that addresses it. A
proposal includes:
  * proposal_id: short kebab-case
  * cluster_pattern_name: which drift cluster this addresses
  * kind: one of:
      - "matcher.threshold_adjust" (PROCEED_THRESHOLD or REVIEW_THRESHOLD env override)
      - "matcher.dimension_weight" (re-weight one of 7 dimensions)
      - "matcher.prompt_edit" (system or user template change)
      - "matcher.temperature" (lower variance vs higher creativity)
      - "matcher.add_penalty" (new hard-penalty rule)
      - "agent.reasoning_effort" (model upgrade/downgrade for an agent)
      - "cron.cadence" (change scheduling)
      - "skill.ranking" (bump skill success/fail counters)
      - "structural" (anything else, e.g. SOUL.md edit, new dimension, retire skill)
  * summary: one-sentence change
  * specific_change: precise diff or numerical target ("set HERMES_JOBFLOW_PROCEED_THRESHOLD=8.50, was 8.75")
  * rationale: 2-3 sentences citing evidence_job_ids
  * expected_effect: what should change after applying
  * risk: low / medium / high (high = could regress agreement %)

Constraints:
  * Stay grounded in the cluster you cite. No proposals without a cluster.
  * Prefer reversible, narrow changes over rewrites.
  * If risk=high, the change is propose-only regardless of kind.

Output STRUCTURED list (ProposalList). Empty list is acceptable when no
proposal is justified.
"""


CRITIC_RESOLVER_SYSTEM_PROMPT = """\
You are Critic, resolving a CONTRADICTION between your own previously-generated
proposals. Reflexion replay flagged that two or more proposals pull in OPPOSITE
directions (e.g. one lowers PROCEED_THRESHOLD, another raises it).

Your job: replace the contradicting proposals with 0, 1, or 2 UNIFIED proposals
that:
  1. Address the same root drift cluster
  2. Do NOT introduce another threshold-vs-threshold opposition
  3. Are honest about uncertainty — if the original proposals genuinely
     conflict because the data is ambiguous, return an EMPTY list (better
     than picking the wrong direction)

Preferred resolution strategies, ordered by safety:
  * Switch from `matcher.threshold_adjust` to `matcher.prompt_edit` — instead
    of moving a numerical threshold, refine the rubric so the model produces
    different scores for the AMBIGUOUS dimension. This addresses the cluster
    without a directional commitment.
  * Switch to `matcher.dimension_weight` — re-weight the dimension that's
    drifting (e.g. lower skills_overlap weight if both shadow and prod
    over-score it).
  * If the contradictions span fundamentally different patterns, propose a
    NEW MEASUREMENT (e.g. "add a new diff-report column for X") rather than
    a fix. Returning an empty list is also acceptable — no proposal beats a
    bad proposal.

Output STRUCTURED list (ProposalList). Each proposal must:
  * Reference the original cluster_pattern_name
  * Include `rationale` that explicitly cites which two original proposals it
    replaces and why the new approach avoids the contradiction
  * Set `risk: medium` or higher (resolution attempts haven't been tested)
"""


CRITIC_RESOLVER_USER_TEMPLATE = """\
## Contradicting proposals (flagged by reflexion replay)

{contradicting_json}

## Original cluster context

{cluster_json}

---

Produce 0, 1, or 2 unified replacement proposals now. Empty list is acceptable
if the data genuinely doesn't support a directional change.
"""


CRITIC_PROPOSAL_USER_TEMPLATE = """\
## Drift clusters identified in step 1

{clusters_json}

## Allowed-knobs auto-apply surface (anything not in here is propose-only)

{allowed_knobs_json}

## Current Matcher configuration (for reference when proposing changes)

  * proceed_threshold: 8.75 (env: HERMES_JOBFLOW_PROCEED_THRESHOLD)
  * review_threshold: 5.0 (env: HERMES_JOBFLOW_REVIEW_THRESHOLD)
  * model: gpt-4o-mini (env: HERMES_JOBFLOW_MODEL)
  * temperature: 0.1
  * dimensions + weights:
      - title_match (0.20)
      - skills_overlap (0.25)
      - industry_fit (0.15)
      - location (0.10)
      - comp_alignment (0.10)
      - growth (0.10)
      - culture (0.10)

---

Generate proposals now. Each must reference a cluster from above.
"""
