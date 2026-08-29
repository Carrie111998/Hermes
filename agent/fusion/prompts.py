"""Prompt templates for Fusion participants."""

from __future__ import annotations

from .briefing import brief_to_markdown
from .context import FusionContext
from .models import (
    FusionCandidate,
    FusionParticipantResult,
    FusionParticipantSpec,
    FusionRequest,
    FusionVerificationReport,
    MATERIAL_AXES,
)


def _runtime_line(spec: FusionParticipantSpec) -> str:
    return f"{spec.provider or 'inherit'}:{spec.model or 'inherit'} reasoning={spec.reasoning_effort or 'inherit'}"


def _brief_block(brief: dict | str | None, *, limit: int = 9000) -> str:
    if isinstance(brief, dict):
        text = str(brief.get("markdown") or brief_to_markdown(brief))
    else:
        text = str(brief or "")
    if len(text) > limit:
        return text[:limit].rstrip() + "\n...[brief truncated]"
    return text or "<no evidence brief>"


def build_participant_system_prompt(spec: FusionParticipantSpec, context: FusionContext, *, phase: str = "draft") -> str:
    repo_line = context.repo_root or "<no repo root resolved>"
    axes = "\n".join(f"- {axis}" for axis in MATERIAL_AXES)
    spike_mode = phase.startswith("spike")
    capability_line = (
        "Fusion spike mode: you MAY use write_file/patch only inside the isolated throwaway worktree registered for this task. Do not treat those edits as applied to the real repo; summarize the experiment and diff evidence. You still MUST NOT run shell commands, execute code, install packages, send messages, schedule jobs, or use external side-effect tools."
        if spike_mode
        else "Fusion normal mode is read-only. You MUST NOT modify files, run shell commands, execute code, install packages, send messages, schedule jobs, or use side-effect tools. Use only read/search/research tools that are available to you. If a tool is unavailable, state that limitation."
    )
    return f"""You are a Fusion v2 equal peer participant: {spec.slug}.

{capability_line}

Repository scope: {repo_line}
Participant label: {spec.role}
Equal-peer scope: {spec.focus}
Your runtime identity: {_runtime_line(spec)}
Current Fusion phase: {phase}

Equality rules:
- Every participant has the same status, rights, and responsibility.
- No participant is the chair, architect, critic, tester, verifier, or tie-breaker by default.
- Your model/runtime identity is used for diversity and attribution only; it must not narrow your task.
- Produce a full independent judgment, then challenge and answer peers as an equal.
- Final output is allowed only after unanimous approval across all successful peers; majority vote never overrides dissent.

Reference Fusion rules:
- Evidence before hypotheses: distinguish confirmed facts from hypotheses.
- Locate before plan for bug/unknown-root tasks.
- Treat the brief boundary as suspicious: name missing layers and probes needed to cover them.
- Cross-verify peer claims instrumentally with read/search evidence when possible.
- Do not launder unverified assumptions into confirmed facts.

Material axes to keep in mind:
{axes}
"""


def build_locate_prompt(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    brief: dict | str | None = None,
) -> str:
    return f"""Fusion mode: {request.mode}
Task:
{request.task}

Phase 0: LOCATE before planning.
You are an equal peer evidence gatherer. Do NOT propose the final plan yet. Gather and verify facts that locate the root/problem layer.

Shared evidence brief:
{_brief_block(brief)}

Instructions:
- Reproduce conceptually from available repo evidence; if actual reproduction/log access is unavailable, mark the missing probe explicitly.
- Trace the operation across layers and name the first layer where valid state likely becomes invalid.
- Search/read files as needed, but do not mutate anything.
- Separate confirmed facts from hypotheses.
- For every defensive/fallback path you notice, name the upstream condition that triggers it.

Return markdown with this exact structure:

## Located Layer Candidates
- <layer>: <confirmed|hypothesis> — <evidence path or missing probe>

## Verified Facts
- <path/source>: <fact>

## Missing Probes
- <probe needed, or 'none'>

## Layers Covered
- <layer>

## Layers NOT Covered
- <layer or 'none'>

## Planning Implications
- <what any later plan must respect>
"""


def build_draft_prompt(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    brief: dict | str | None = None,
) -> str:
    return f"""Fusion mode: {request.mode}
Task:
{request.task}

Context packet:
- repo_root: {context.repo_root or '<unavailable>'}
- repo_guard_available: {context.repo_guard_available}
- notes: {', '.join(context.notes) if context.notes else 'none'}

Shared raw evidence brief:
{_brief_block(brief)}

Phase 1: blind draft.
You cannot see sibling outputs. Analyze independently and ground repo claims in files you actually read or in the shared evidence brief. Challenge the task from multiple angles: don't build it, build it simpler, dependency on future work, and alternative scenarios.
You are not filling a specialized role; write a complete plan/review across every material axis, including risks and tests.

Return markdown with this exact structure:

## Summary
Brief equal-peer conclusion.

## Material Axes
- architecture: <claim>
- approach: <claim>
- key_assumptions: <claim>
- repo_facts: <claim>
- api_flag_config_claims: <claim>
- risks_blockers: <claim>
- implementation_sequence: <claim>
- test_strategy: <claim>
- migration_backcompat_claims: <claim>

## Evidence
- <file/path or source>: <what it proves>

## Alternatives Considered
- <alternative and why rejected or still viable>

## Operator Unknowns
- <unknown or 'none'>

## Recommendations
- <recommendation>

## Open Questions
- <question or 'none'>
"""


def build_participant_user_prompt(spec: FusionParticipantSpec, request: FusionRequest, context: FusionContext) -> str:
    return build_draft_prompt(spec, request, context)


def _summarize_outputs(results: list[FusionParticipantResult], *, exclude_slug: str | None = None, limit: int = 5000) -> str:
    chunks: list[str] = []
    for result in results:
        if exclude_slug and result.spec.slug == exclude_slug:
            continue
        text = (result.output or result.error or "").strip()
        if len(text) > limit:
            text = text[:limit].rstrip() + "\n...[truncated]"
        chunks.append(f"### {result.spec.slug} ({result.spec.runtime_label})\n{text or '<no output>'}")
    return "\n\n".join(chunks) or "<no sibling outputs>"


def _summarize_vote_feedback(report: FusionVerificationReport | None) -> str:
    if report is None or not report.votes:
        return ""
    lines: list[str] = []
    for vote in report.votes:
        state = "approved" if vote.approved and not vote.material_dissent else "not approved"
        details: list[str] = []
        if vote.material_dissent:
            details.append("material_dissent=" + repr(vote.material_dissent))
        if vote.required_changes:
            details.append("required_changes=" + repr(vote.required_changes))
        if vote.unsupported_claims:
            details.append("unsupported_claims=" + repr(vote.unsupported_claims))
        if vote.summary:
            details.append("summary=" + vote.summary)
        suffix = "; " + "; ".join(details) if details else ""
        lines.append(f"- {vote.participant}: {state}{suffix}")
    return "\n".join(lines)


def _short_candidate(candidate: FusionCandidate | None, limit: int = 2500) -> str:
    if candidate is None:
        return "<no previous candidate>"
    text = candidate.content.strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n...[truncated]"
    return text or "<empty previous candidate>"


def build_cross_verify_prompt(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    target: FusionParticipantResult | None,
    brief: dict | str | None = None,
) -> str:
    target_label = target.spec.slug if target else "<missing target>"
    target_text = (target.output if target else "") or "<missing target draft>"
    return f"""Fusion mode: {request.mode}
Task:
{request.task}

Phase 2: cross-verify rotation.
You are still an equal peer. Your job is to instrumentally verify another participant's blind draft. Nobody grades themselves.

Shared evidence brief:
{_brief_block(brief, limit=7000)}

Target draft author: {target_label}

--- TARGET DRAFT ---
{target_text}
--- END TARGET DRAFT ---

Assume the target draft may be wrong. Check correctness, completeness, assumptions, contradictions, missed risks, unsupported repo/API/config claims, and whether it planned from the wrong layer.

Return markdown with findings using this format:

## Cross Verification Findings
- [BLOCKER|MAJOR|MINOR] <axis> @<section> — <finding> — <evidence path/probe or unavailable>

## Questions For Author
- <question or 'none'>

## Verdict
VERDICT: verified|issues-found|blocked (blockers=<N>)
"""


def build_wrong_layer_prompt(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    drafts: list[FusionParticipantResult],
    cross_verifications: list[FusionParticipantResult],
    brief: dict | str | None = None,
) -> str:
    return f"""Fusion mode: {request.mode}
Task:
{request.task}

Phase 2b: wrong-layer / wrong-abstraction adversary.
You are an equal peer. Assume the apparent root cause or proposed solution layer in the drafts is wrong. Find where else the true problem or better solution could live, especially outside the shown or over-discussed layer.

Shared evidence brief:
{_brief_block(brief, limit=6500)}

## Drafts
{_summarize_outputs(drafts, limit=2500)}

## Cross-verification record
{_summarize_outputs(cross_verifications, limit=2500)}

Return markdown with this exact structure:

## Wrong-Layer Challenges
- <alternate layer/abstraction>: <why it could be the true root or better solution>

## Required Probes
- <probe/read/search needed to confirm or refute, or 'none'>

## Blocking Concern
- <material concern that must block convergence, or 'none'>
"""


def build_debate_prompt(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    drafts: list[FusionParticipantResult],
    *,
    round_index: int = 1,
    previous_debates: list[FusionParticipantResult] | None = None,
    previous_candidate: FusionCandidate | None = None,
    vote_feedback: FusionVerificationReport | None = None,
    cross_verifications: list[FusionParticipantResult] | None = None,
    wrong_layer_results: list[FusionParticipantResult] | None = None,
    probe_results: list[FusionParticipantResult] | None = None,
    spike_results: list[FusionParticipantResult] | None = None,
    brief: dict | str | None = None,
) -> str:
    siblings = _summarize_outputs(drafts, exclude_slug=spec.slug)
    own = next((r.output for r in drafts if r.spec.slug == spec.slug), "")
    previous = _summarize_outputs(previous_debates or [], exclude_slug=None, limit=3500)
    prior_block = (
        "\n## Previous debate rounds\n"
        f"{previous}\n"
        if previous_debates
        else ""
    )
    vote_summary = _summarize_vote_feedback(vote_feedback)
    vote_block = (
        "\n## Previous candidate and convergence feedback\n"
        f"Candidate: `{previous_candidate.id if previous_candidate else 'unknown'}`\n\n"
        f"{_short_candidate(previous_candidate)}\n\n"
        "### Prior votes\n"
        f"{vote_summary}\n"
        if vote_summary
        else ""
    )
    cross_block = _summarize_outputs(cross_verifications or [], limit=3500)
    wrong_block = _summarize_outputs(wrong_layer_results or [], limit=2500)
    probe_block = _summarize_outputs(probe_results or [], limit=2500)
    spike_block = _summarize_outputs(spike_results or [], limit=3500)
    return f"""Fusion mode: {request.mode}
Task:
{request.task}

Phase 3: equal-peer debate round {round_index} of up to {request.debate_rounds}.
You may now inspect sibling blind drafts and verification outputs. Every participant has equal status and equal responsibility for the whole task.
Challenge sibling claims with evidence, answer questions raised by siblings, and update your stance if their evidence is stronger. Do not act as a preassigned skeptic/tester/architect.
If prior convergence feedback exists, focus this round on resolving the listed dissent, required changes, and unsupported claims.

## Shared evidence brief
{_brief_block(brief, limit=5500)}

## Your blind draft
{own or '<missing>'}

## Sibling blind drafts
{siblings}

## Cross-verification rotation findings
{cross_block}

## Wrong-layer / wrong-abstraction findings
{wrong_block}

## Spike worktree evidence from prior unresolved dissent
{spike_block or '<none yet>'}

## Read-only probe evidence from prior unresolved dissent
{probe_block or '<none yet>'}
{prior_block}{vote_block}
Return markdown with this exact structure:

## Debate Summary
- <what you now agree with>
- <what you still dispute>

## Questions To Peers
- <participant>: <specific question that must be answered before convergence, or 'none'>

## Answers To Peer Questions
- <question/participant>: <answer based on evidence, or 'none'>

## Agreements
- <agreement grounded in draft or debate evidence>

## Objections
- <material objection or 'none'>

## Required Candidate Changes
- <change required before you approve a final candidate, or 'none'>

## Material Dissent
- <remaining dissent that must block convergence, or 'none'>
"""


def build_probe_prompt(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    report: FusionVerificationReport,
    candidate: FusionCandidate,
    brief: dict | str | None = None,
) -> str:
    return f"""Fusion mode: {request.mode}
Task:
{request.task}

Phase 4: read-only probe to resolve material dissent.
The previous candidate did not reach unanimous consensus. Use only read/search/research tools. Do not write code, run shell commands, or mutate files.

Shared evidence brief:
{_brief_block(brief, limit=6000)}

Candidate needing revision:
{_short_candidate(candidate, limit=3500)}

Vote feedback:
{_summarize_vote_feedback(report) or '<no structured feedback>'}

Return markdown with this exact structure:

## Probe Results
- <dissent/change/unsupported claim>: <confirmed|refuted|inconclusive> — <evidence path/probe limitation>

## Candidate Revision Guidance
- <specific change needed, or 'none'>

## Remaining Operator Unknowns
- <unknown requiring human/live-system input, or 'none'>
"""


def build_spike_prompt(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    report: FusionVerificationReport,
    candidate: FusionCandidate,
    *,
    worktree_root: str,
    brief: dict | str | None = None,
) -> str:
    return f"""Fusion mode: {request.mode}
Task:
{request.task}

Phase 4a: isolated spike worktree to resolve material dissent.
The previous candidate did not reach unanimous consensus. You may use read_file/search_files/write_file/patch, but writes are allowed ONLY inside this isolated throwaway worktree:

`{worktree_root}`

Do not run shell commands. Do not install packages. Do not touch the operator's main repo. Treat any edits you make as an experiment only: they will be diffed, summarized, and discarded. The final Fusion output remains a plan, not applied code.

Shared evidence brief:
{_brief_block(brief, limit=6000)}

Candidate needing revision:
{_short_candidate(candidate, limit=3500)}

Vote feedback:
{_summarize_vote_feedback(report) or '<no structured feedback>'}

Instructions:
- If a small file edit would clarify or validate a disputed approach, make it inside the worktree.
- Keep edits minimal and targeted to the dissent/required changes.
- If no write experiment is useful, say so and use read/search evidence instead.
- Report what you changed and why. The orchestrator will capture the git diff; do not paste huge diffs.

Return markdown with this exact structure:

## Spike Hypothesis
- <what this experiment tried to validate>

## Spike Edits
- <file path>: <change made, or 'none'>

## Spike Findings
- <dissent/change/unsupported claim>: <confirmed|refuted|inconclusive> — <evidence>

## Candidate Revision Guidance
- <specific change needed, or 'none'>

## Remaining Operator Unknowns
- <unknown requiring human/live-system input, or 'none'>
"""


def build_premortem_prompt(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    candidate: FusionCandidate,
    debates: list[FusionParticipantResult],
    brief: dict | str | None = None,
) -> str:
    return f"""Fusion mode: {request.mode}
Task:
{request.task}

Phase 5: pre-mortem before convergence vote.
It is a week later and the operator says the approved plan still failed or the implementation got stuck. As an equal peer, name the most likely reason before synthesis proceeds.

Shared evidence brief:
{_brief_block(brief, limit=5000)}

Candidate:
{_short_candidate(candidate, limit=4000)}

Debate record:
{_summarize_outputs(debates, limit=2500)}

Return markdown with this exact structure:

## Pre-mortem Failure Modes
- <failure mode>: <why it could happen>

## Missing Evidence Or Layer
- <missing evidence/layer/probe, or 'none'>

## Blocks Convergence?
- yes|no — <reason>
"""


def build_vote_prompt(
    spec: FusionParticipantSpec,
    request: FusionRequest,
    context: FusionContext,
    candidate: FusionCandidate,
    drafts: list[FusionParticipantResult],
    debates: list[FusionParticipantResult],
    *,
    cross_verifications: list[FusionParticipantResult] | None = None,
    wrong_layer_results: list[FusionParticipantResult] | None = None,
    premortem_results: list[FusionParticipantResult] | None = None,
) -> str:
    sibling_debate = _summarize_outputs(debates, exclude_slug=None, limit=3000)
    cross_block = _summarize_outputs(cross_verifications or [], limit=2200)
    wrong_block = _summarize_outputs(wrong_layer_results or [], limit=1800)
    premortem_block = _summarize_outputs(premortem_results or [], limit=2200)
    return f"""Fusion mode: {request.mode}
Task:
{request.task}

Phase 6: equal-peer convergence vote.
Review the candidate final plan below. You must approve only if it resolves your material concerns, addresses cross-verification findings, survives the wrong-layer challenge, and answers the peer questions needed for convergence. A 2-of-3 majority is not enough; one material dissent blocks final emission.

## Candidate {candidate.id}
{candidate.content}

## Debate record
{sibling_debate}

## Cross-verification record
{cross_block}

## Wrong-layer / wrong-abstraction record
{wrong_block}

## Pre-mortem record
{premortem_block}

Return a short explanation and exactly one fenced JSON object with this schema:

```json
{{
  "candidate_id": "{candidate.id}",
  "approved": true,
  "material_dissent": [],
  "required_changes": [],
  "unsupported_claims": [],
  "confidence": "high",
  "summary": "why you approve or reject"
}}
```

Rules:
- Set approved=false if any material issue remains.
- material_dissent must list blocking disagreements only.
- required_changes should be actionable edits for the next candidate round.
- unsupported_claims should list candidate claims you could not verify.
"""
