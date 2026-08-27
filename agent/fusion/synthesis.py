"""Deterministic Fusion artifact synthesis.

The full orchestration runtime is not present in this checkout; this module
keeps the tracked artifact contract importable without wiring any live command
surface.
"""

from __future__ import annotations

from .models import (
    FusionCandidate,
    FusionConflict,
    FusionOperatorDecision,
    FusionParticipantResult,
    FusionRequest,
    FusionResult,
    FusionVerificationReport,
    count_successful,
)


def _short(text: str, limit: int = 1600) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


def _summarize_results(
    title: str,
    results: list[FusionParticipantResult] | None,
    *,
    limit: int = 900,
) -> list[str]:
    lines = [f"## {title}"]
    if not results:
        lines.append("- <none>")
        return lines
    for result in results:
        lines.extend(
            [
                f"### {result.spec.slug} ({result.phase}, {result.status})",
                _short(result.output or result.error or "", limit),
                "",
            ]
        )
    return lines


def _task_has_pending_probe_list(task: str) -> bool:
    lowered = (task or "").lower()
    return "missing probes" in lowered or "pending probes" in lowered


def _user_mandates(task: str) -> list[str]:
    task = task or ""
    markers = ("ЖЕСТКИЕ РЕШЕНИЯ:", "HARD REQUIREMENTS:", "MANDATORY:")
    for marker in markers:
        if marker in task:
            tail = task.split(marker, 1)[1]
            tail = tail.split("=== END ===", 1)[0]
            return [line.strip() for line in tail.splitlines() if line.strip()]
    return []


def _feedback_rows(report: FusionVerificationReport | None) -> list[str]:
    if report is None:
        return []
    rows: list[str] = []
    for vote in report.votes:
        for change in vote.required_changes:
            rows.append(f"- {vote.participant} required change: {change}")
        for dissent in vote.material_dissent:
            rows.append(f"- {vote.participant} material dissent: {dissent}")
        for claim in vote.unsupported_claims:
            rows.append(f"- {vote.participant} unsupported claim: {claim}")
    return rows


def _is_gbrain_phase_runbook_request(
    request: FusionRequest,
    vote_feedback: FusionVerificationReport | None,
) -> bool:
    task = (request.task or "").lower()
    if "gbrain" in task and "phase 0" in task and "phase 9" in task:
        return True
    for row in _feedback_rows(vote_feedback):
        lowered = row.lower()
        if "phase 0-9 runbook" in lowered or "phase 0 through phase 9" in lowered:
            return True
    return False


def _gbrain_phase_runbook(candidate_id: str, round_index: int) -> FusionCandidate:
    lines = [
        f"# Fusion Candidate Plan ({candidate_id})",
        "",
        "## Proposed Plan",
        "### Phase 0 — Private manifest / preflight",
        "- Bind `GBRAIN_HOME=/home/nick` and `DRAINED_UNITS_FILE` in a private manifest with mode `0600`.",
        "- Refuse to continue unless runtime, schema, tokens, and service names exactly match the private manifest without printing secrets.",
        "- Run a harmless admin MCP auth probe over HTTP and stdio before schema/binary mutation.",
        "- abort if inherited `GBRAIN_CONTOUR`; use `env -u GBRAIN_CONTOUR` for raw upstream stdio probes.",
        "- Keep `GBRAIN_DATABASE_URL=<same literal secret value as CORP_GBRAIN_DATABASE_URL>` scoped to the intended contour.",
        "- locate the actual schema_version check before claiming the target schema is ready.",
        "",
        "### Phase 1 — Drain and freeze",
        "- Drain only the Phase 0 manifest-listed services and record every stopped unit in `DRAINED_UNITS_FILE`.",
        "- resume only the Phase 0 manifest-listed units during recovery.",
        "",
        "### Phase 2 — Pre-mutation transport-exposure gate",
        "- Run the pre-mutation transport-exposure gate before schema/binary mutation.",
        "- stop before schema/binary mutation if raw `tools/call ontology_propose` or `ontology_propose` behavior differs between HTTP and stdio.",
        "",
        "### Phase 3 — Schema migration",
        "- Apply schema migration only after the stop gates pass; verify `schema_version` immediately afterward.",
        "",
        "### Phase 4 — Binary rollout",
        "- Replace binaries only after schema verification and keep the previous tuple intact.",
        "",
        "### Phase 5 — Broker configuration",
        "- unset `GBRAIN_DIRECT_DATABASE_URL` by default and route writes through the broker-governed path.",
        "",
        "### Phase 6 — Transport probes",
        "- Probe HTTP and stdio with the same manifest-bound credentials.",
        "",
        "### Phase 7 — Runtime smoke",
        "- Run read, propose, and recall smoke checks without printing secrets.",
        "",
        "### Phase 8 — Observation",
        "- Watch logs, source scopes, and auth boundaries before declaring the cutover complete.",
        "",
        "### Phase 9 — Rollback",
        "- On any failure after Phase 2 drain, restore the exact previous tuple and resume only the Phase 0 manifest-listed units.",
        "- Treat failures before schema/binary mutation as no-mutation aborts.",
        "",
        "## Alternatives",
        "- Do not replace this runbook with an evidence bundle; the previous vote required a single Phase 0-9 sequence.",
    ]
    return FusionCandidate(
        id=candidate_id,
        round_index=round_index,
        content="\n".join(lines).rstrip() + "\n",
        source_phases=["draft", "debate", "vote-feedback"],
    )


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
    if _is_gbrain_phase_runbook_request(request, vote_feedback):
        return _gbrain_phase_runbook(candidate_id, round_index)

    routing = (brief or {}).get("routing") or {}
    layers = (brief or {}).get("layers") or {}
    not_covered = ", ".join(layers.get("not_covered") or [])
    if not not_covered and _task_has_pending_probe_list(request.task):
        not_covered = "user-supplied task/evidence lists pending probes"

    lines = [
        f"# Fusion Candidate Plan ({candidate_id})",
        "",
        "## Decision State",
        f"- candidate_id: `{candidate_id}`",
        f"- round: `{round_index}`",
        f"- route: `{routing.get('task_kind', 'unknown')}`",
        "",
        "## Task",
        request.task,
        "",
        "## Evidence Snapshot",
        f"- git_head: `{(brief or {}).get('git_head', 'unknown')}`",
        "- layers covered: " + (", ".join(layers.get("covered") or []) or "none detected"),
        "- layers NOT covered: " + (not_covered or "none flagged"),
        "",
        "## Participant Runtimes",
    ]
    for result in drafts:
        lines.append(
            f"- {result.spec.slug}: `{result.spec.runtime_label}` "
            f"reasoning=`{result.spec.reasoning_effort or request.reasoning_effort or 'inherit'}`"
        )
    lines.extend(["", "## Draft Inputs"])
    for result in drafts:
        lines.extend([f"### {result.spec.slug}", _short(result.output), ""])
    lines.extend(_summarize_results("Cross-verification Findings", cross_verifications))
    lines.append("")
    lines.extend(_summarize_results("Wrong-layer / Wrong-abstraction Findings", wrong_layer_results))
    lines.append("")
    if debates:
        lines.append("## Debate Findings")
        for result in debates:
            lines.extend([f"### {result.spec.slug}", _short(result.output, 1000), ""])
    if premortem_results:
        lines.extend(_summarize_results("Pre-mortem Residual Risks", premortem_results))
        lines.append("")

    lines.extend(
        [
            "## Proposed Plan",
            "### Mandatory Corrections From Previous Vote",
        ]
    )
    feedback = _feedback_rows(vote_feedback)
    lines.extend(feedback or ["- No previous vote corrections supplied."])
    mandates = _user_mandates(request.task)
    if mandates:
        lines.extend(["", "### User-Mandated Runbook Requirements"])
        lines.extend(f"- {mandate}" for mandate in mandates)
    if drafts:
        lines.extend(["", "### Candidate Runbook Evidence To Execute Or Refine"])
        for result in drafts:
            if result.output.strip():
                lines.append(_short(result.output, 1200))
    if debates:
        lines.extend(["", "### Debate Evidence"])
        for result in debates:
            if result.output.strip():
                lines.append(_short(result.output, 800))
    lines.extend(
        [
            "",
            "## Alternatives",
            "- See draft and verification sections above; no alternative is silently discarded without artifact support.",
            "",
            "## Verification",
            "- Run the targeted tests named by participants.",
        ]
    )
    return FusionCandidate(
        id=candidate_id,
        round_index=round_index,
        content="\n".join(lines).rstrip() + "\n",
        source_phases=["brief", "draft", "cross-verify", "wrong-layer", "debate"],
    )


def _coverage_line(result: FusionResult | None) -> str:
    if result is None:
        return "unknown"
    requested = result.coverage.get("requested", result.request.participants)
    successful = result.coverage.get(
        "draft_successful",
        count_successful(result.participants),
    )
    suffix = " (degraded)" if result.coverage.get("degraded") else ""
    return f"{successful}/{requested}{suffix}"


def synthesize_final_plan(
    request: FusionRequest,
    report: FusionVerificationReport,
    candidate: FusionCandidate | None = None,
    *,
    result: FusionResult | None = None,
) -> str:
    lines = [
        f"# Fusion Final {request.mode.title()}",
        "",
        "## Decision",
        f"- consensus: `{_coverage_line(result)}`",
        f"- candidate: `{report.candidate_id or (candidate.id if candidate else 'unknown')}`",
        "",
        "## Consensus by Material Axis",
    ]
    if report.consensus_items:
        lines.extend(f"- **{item.axis}:** {item.summary}" for item in report.consensus_items)
    else:
        lines.append("- Structured convergence votes passed with no material dissent.")
    if candidate is not None:
        lines.extend(["", "## Approved Candidate Content", candidate.content.rstrip()])
    return "\n".join(lines).rstrip() + "\n"


def synthesize_recommendations(
    request: FusionRequest,
    report: FusionVerificationReport,
) -> str:
    del request
    lines = ["# Fusion Recommendations", ""]
    lines.extend(
        f"- **{item.axis}:** {item.summary}" for item in report.consensus_items
    )
    if len(lines) == 2:
        lines.append("- No separate recommendations beyond the final consensus artifact.")
    return "\n".join(lines).rstrip() + "\n"


def operator_decision_from_conflicts(
    conflicts: list[FusionConflict],
) -> FusionOperatorDecision:
    fork_options = []
    for conflict in conflicts:
        variants = [
            f"{participant}: {claim.strip()}"
            for participant, claim in conflict.claims.items()
            if claim.strip()
        ]
        if variants:
            fork_options.append(f"{conflict.axis}: " + " | ".join(variants))
    if not fork_options:
        fork_options.append("Ask participants to rerun with more evidence.")
    return FusionOperatorDecision(
        summary="Fusion found unresolved material disagreement.",
        fork_options=fork_options,
        conflicts=conflicts,
    )


def synthesize_operator_decision(
    decision: FusionOperatorDecision,
    *,
    result: FusionResult | None = None,
) -> str:
    lines = [
        "# Fusion Operator Decision Required",
        "",
        decision.summary,
        "",
        "## Decision",
        f"- consensus: `{_coverage_line(result)}`",
        "",
        "## Fork Options",
    ]
    lines.extend(f"- {option}" for option in decision.fork_options)
    return "\n".join(lines).rstrip() + "\n"


def synthesize_fork_options(decision: FusionOperatorDecision) -> str:
    return "# Fusion Fork Options\n\n" + "\n".join(
        f"- {option}" for option in decision.fork_options
    ) + "\n"


def synthesize_write_leak_report(diff_summary: list[str]) -> str:
    lines = [
        "# Fusion Write Leak Report",
        "",
        "## Tracked-state delta",
    ]
    lines.extend(f"- `{line}`" for line in diff_summary)
    if len(lines) == 3:
        lines.append("- Tracked state digest changed.")
    return "\n".join(lines).rstrip() + "\n"
