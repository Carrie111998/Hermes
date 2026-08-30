"""Artifact synthesis for Fusion v2."""

from __future__ import annotations

from .models import (
    FusionCandidate,
    FusionConflict,
    FusionOperatorDecision,
    FusionParticipantResult,
    FusionRequest,
    FusionResult,
    FusionVerificationReport,
)


def _short(text: str, limit: int = 1600) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


def _summarize_results(title: str, results: list[FusionParticipantResult] | None, *, limit: int = 900) -> list[str]:
    lines = [f"## {title}"]
    if not results:
        lines.append("- <none>")
        return lines
    for result in results:
        status = result.status
        lines.extend([f"### {result.spec.slug} ({result.phase}, {status})", _short(result.output or result.error or "", limit), ""])
    return lines


def _coverage_line(result: FusionResult | None) -> str:
    if result is None:
        return "unknown"
    coverage = result.coverage or {}
    requested = coverage.get("requested") or result.request.participants
    successful = coverage.get("draft_successful") or len([p for p in result.participants if p.ok])
    degraded = bool(coverage.get("degraded"))
    suffix = " (degraded)" if degraded else ""
    return f"{successful}/{requested}{suffix}"


def _decision_for(result: FusionResult | None, status: str) -> str:
    if result and result.decision:
        return result.decision
    if status == "converged":
        return "consensus"
    if status == "operator_decision":
        return "operator_decision"
    return status


def _root_cause_marker(result: FusionResult | None) -> str:
    if not result:
        return "hypothesis"
    routing = result.routing or {}
    if routing.get("task_kind") == "bug_unknown_root":
        # LOCATE/spike evidence can raise confidence but should not be laundered into
        # a confirmed runtime root cause unless the model record explicitly says so.
        return "hypothesis"
    return "not-applicable"


def synthesize_candidate_plan(
    request: FusionRequest,
    drafts: list[FusionParticipantResult],
    debates: list[FusionParticipantResult],
    *,
    round_index: int,
    previous_candidate: FusionCandidate | None = None,
    vote_feedback: FusionVerificationReport | None = None,
    cross_verifications: list[FusionParticipantResult] | None = None,
    wrong_layer_results: list[FusionParticipantResult] | None = None,
    probe_results: list[FusionParticipantResult] | None = None,
    spike_results: list[FusionParticipantResult] | None = None,
    premortem_results: list[FusionParticipantResult] | None = None,
    brief: dict | None = None,
) -> FusionCandidate:
    candidate_id = f"candidate-r{round_index}"
    routing = (brief or {}).get("routing") or {}
    layers = (brief or {}).get("layers") or {}
    lines = [
        f"# Fusion Candidate Plan ({candidate_id})",
        "",
        "This candidate is mechanically synthesized from the shared evidence brief, independent drafts, cross-verification, wrong-layer challenge, debate, isolated spike worktree evidence, and read-only probe artifacts. It is not final until every successful participant approves it with no material dissent.",
        "",
        "## Decision State",
        f"- candidate_id: `{candidate_id}`",
        f"- round: `{round_index}`",
        f"- route: `{routing.get('task_kind', 'unknown')}`",
        f"- root-cause: `{ 'hypothesis' if routing.get('task_kind') == 'bug_unknown_root' else 'not-applicable' }`",
        "",
        "## Task",
        request.task,
        "",
        "## Evidence Snapshot",
        f"- git_head: `{(brief or {}).get('git_head', 'unknown')}`",
        "- layers covered: " + (", ".join(layers.get("covered") or []) or "none detected"),
        "- layers NOT covered: " + (", ".join(layers.get("not_covered") or []) or "none flagged"),
        "",
        "## Participant Runtimes",
    ]
    for result in drafts:
        lines.append(f"- {result.spec.slug}: `{result.spec.runtime_label}` reasoning=`{result.spec.reasoning_effort or request.reasoning_effort or 'inherit'}`")
    lines.extend(["", "## Draft Inputs"])
    for result in drafts:
        lines.extend([f"### {result.spec.slug}", _short(result.output), ""])
    lines.extend(_summarize_results("Cross-verification Findings", cross_verifications, limit=900))
    lines.extend([""])
    lines.extend(_summarize_results("Wrong-layer / Wrong-abstraction Findings", wrong_layer_results, limit=800))
    lines.extend([""])
    if spike_results:
        lines.extend(_summarize_results("Isolated Spike Worktree Results", spike_results, limit=1000))
        lines.append("")
    if probe_results:
        lines.extend(_summarize_results("Read-only Probe Results", probe_results, limit=800))
        lines.append("")
    if debates:
        lines.append("## Debate Findings")
        for result in debates:
            lines.extend([f"### {result.spec.slug}", _short(result.output, 1000), ""])
    if previous_candidate and vote_feedback:
        lines.append("## Revision Feedback")
        for vote in vote_feedback.votes:
            if vote.required_changes or vote.material_dissent or vote.unsupported_claims:
                lines.append(
                    f"- {vote.participant}: required_changes={vote.required_changes or []}; "
                    f"material_dissent={vote.material_dissent or []}; unsupported_claims={vote.unsupported_claims or []}"
                )
        lines.append("")
    if premortem_results:
        lines.extend(_summarize_results("Pre-mortem Residual Risks", premortem_results, limit=700))
        lines.append("")
    lines.extend([
        "## Proposed Plan",
        "- Use only claims supported by the evidence brief, participant drafts, cross-verification, debate, isolated spike/probe evidence, or explicitly listed hypotheses.",
        "- Preserve all repo read-only and write-leak safeguards named by the participants.",
        "- Prefer the implementation sequence that is supported by the strongest shared repo evidence and has no unresolved material dissent.",
        "- Treat every unresolved material dissent, unsupported claim, or live pre-mortem blocker as blocking until a later candidate resolves it or the operator decides.",
        "",
        "## Alternatives And Why Rejected",
        "- See draft and cross-verification sections above; no alternative is silently discarded without artifact support.",
        "",
        "## Ranked Assumptions",
        "- HIGH: claims explicitly backed by cited repo evidence or unanimous participant agreement.",
        "- MED: claims supported by multiple participant artifacts but without direct repo proof.",
        "- LOW: claims requiring operator/live-system probes; keep as hypotheses.",
        "",
        "## Operator Unknowns / Pending Probes",
        "- Any missing layer, missing live log/repro, unsupported claim, or unresolved pre-mortem blocker must be surfaced before execution.",
        "",
        "## Verification",
        "- Run the targeted tests named by participants.",
        "- Confirm artifacts expose routing, brief, participant model identities, cross-verification, debate outputs, pre-mortem, votes, and final gate status.",
    ])
    return FusionCandidate(
        id=candidate_id,
        round_index=round_index,
        content="\n".join(lines).rstrip() + "\n",
        source_phases=["brief", "draft", "cross-verify", "wrong-layer", "debate", "spike", "probe", "premortem"],
    )


def synthesize_final_plan(
    request: FusionRequest,
    report: FusionVerificationReport,
    candidate: FusionCandidate | None = None,
    *,
    result: FusionResult | None = None,
) -> str:
    decision = _decision_for(result, "converged")
    lines = [
        f"# Fusion Final {request.mode.title()}",
        "",
        "Fusion emitted this final artifact because the hard consensus gate passed for the current candidate.",
        "",
        "## Decision",
        f"- decision: `{decision}`",
        f"- consensus: `{_coverage_line(result)}`",
        f"- root-cause: `{_root_cause_marker(result)}`",
        f"- candidate: `{report.candidate_id or (candidate.id if candidate else 'unknown')}`",
        f"- approved_participants: {', '.join(report.approved_participants or report.successful_participants) or 'none'}",
        "",
        "## Task",
        request.task,
        "",
        "## Evidence And Coverage",
    ]
    if result:
        routing = result.routing or {}
        brief = result.brief or {}
        layers = brief.get("layers") or {}
        lines.extend([
            f"- route: `{routing.get('task_kind', 'unknown')}`",
            f"- git_head: `{brief.get('git_head', 'unknown')}`",
            "- layers covered: " + (", ".join(layers.get("covered") or []) or "none detected"),
            "- layers NOT covered: " + (", ".join(layers.get("not_covered") or []) or "none flagged"),
        ])
    else:
        lines.append("- <coverage unavailable>")
    lines.extend(["", "## Consensus by Material Axis"])
    if report.consensus_items:
        for item in report.consensus_items:
            lines.append(f"- **{item.axis}:** {item.summary}")
    else:
        lines.append("- Structured convergence votes passed with no material dissent.")
    if candidate is not None:
        lines.extend(["", "## Approved Candidate Content", candidate.content.rstrip()])
    lines.extend([
        "",
        "## Gate",
        "- Passed: unanimous approval across all successful participants on the current candidate.",
        "- Majority voting was not used; any material dissent would have blocked final emission.",
        "",
        "## Handoff Boundary",
        "- This is a plan artifact, not an implementation. Execute with normal repo tests and stop if assumptions marked LOW/HYPOTHESIS are invalidated.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def synthesize_recommendations(request: FusionRequest, report: FusionVerificationReport) -> str:
    lines = [
        "# Fusion Recommendations",
        "",
        "These recommendations are derived only from the candidate approved by every successful participant.",
        "",
    ]
    for item in report.consensus_items:
        lines.append(f"- **{item.axis}:** {item.summary}")
    if len(lines) == 4:
        lines.append("- No separate recommendations beyond the final consensus artifact.")
    return "\n".join(lines).rstrip() + "\n"


def operator_decision_from_conflicts(conflicts: list[FusionConflict]) -> FusionOperatorDecision:
    fork_options: list[str] = []
    for conflict in conflicts:
        variants = []
        seen: set[str] = set()
        for participant, claim in conflict.claims.items():
            normalized = claim.strip()
            if normalized and normalized not in seen:
                variants.append(f"{participant}: {normalized}")
                seen.add(normalized)
        if variants:
            fork_options.append(f"{conflict.axis}: " + " | ".join(variants))
    if not fork_options:
        fork_options.append("Ask participants to rerun with more evidence; no concrete fork options were extracted.")
    return FusionOperatorDecision(
        summary="Fusion found unresolved material disagreement. No final plan was emitted.",
        fork_options=fork_options,
        conflicts=conflicts,
    )


def synthesize_operator_decision(decision: FusionOperatorDecision, *, result: FusionResult | None = None) -> str:
    lines = [
        "# Fusion Operator Decision Required",
        "",
        decision.summary,
        "",
        "## Decision",
        f"- decision: `operator_decision`",
        f"- consensus: `{_coverage_line(result)}`",
        f"- root-cause: `{_root_cause_marker(result)}`",
        "",
        "## Fork Options",
    ]
    lines.extend(f"- {option}" for option in decision.fork_options)
    lines.extend(["", "## Material Conflicts"])
    for conflict in decision.conflicts:
        lines.append(f"### {conflict.axis}")
        lines.append(conflict.summary)
        for participant, claim in conflict.claims.items():
            lines.append(f"- **{participant}:** {claim}")
        lines.append("")
    lines.extend([
        "",
        "## Handoff Boundary",
        "- No final plan was emitted. The operator must decide, provide missing evidence, or rerun Fusion with additional context.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def synthesize_fork_options(decision: FusionOperatorDecision) -> str:
    lines = ["# Fusion Fork Options", ""]
    lines.extend(f"- {option}" for option in decision.fork_options)
    return "\n".join(lines).rstrip() + "\n"


def synthesize_write_leak_report(diff_summary: list[str]) -> str:
    lines = [
        "# Fusion Write Leak Report",
        "",
        "Fusion detected tracked repository mutation during the participant workflow. The run stopped immediately and no final plan was emitted.",
        "",
        "## Tracked-state delta",
    ]
    if diff_summary:
        lines.extend(f"- `{line}`" for line in diff_summary)
    else:
        lines.append("- Tracked state digest changed, but no line-level summary was available.")
    return "\n".join(lines).rstrip() + "\n"
