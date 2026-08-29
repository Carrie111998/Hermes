"""Fusion v2 heterogeneous reference-style orchestrator."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable

from .artifacts import create_run_dir, write_fusion_artifacts
from .briefing import build_reference_brief, classify_fusion_task
from .config import ModelDiversityError, model_diversity_summary, normalize_request, participant_specs_for_request
from .consensus_gate import evaluate_consensus_gate
from .context import build_context_packet
from .models import FusionCandidate, FusionParticipantResult, FusionParticipantSpec, FusionRequest, FusionResult
from .participant_runner import run_participant as default_run_participant
from .prompts import (
    build_cross_verify_prompt,
    build_debate_prompt,
    build_draft_prompt,
    build_locate_prompt,
    build_premortem_prompt,
    build_probe_prompt,
    build_spike_prompt,
    build_vote_prompt,
    build_wrong_layer_prompt,
)
from .repo_guard import RepoMutationGuard
from .spikes import capture_spike_diff, cleanup_spike_worktree, create_spike_worktree
from .synthesis import (
    operator_decision_from_conflicts,
    synthesize_candidate_plan,
    synthesize_final_plan,
    synthesize_fork_options,
    synthesize_operator_decision,
    synthesize_recommendations,
    synthesize_write_leak_report,
)
from .verification import verify_convergence_votes, verify_participant_outputs

ParticipantRunner = Callable[..., FusionParticipantResult]


def _run_phase(
    phase: str,
    specs: list[FusionParticipantSpec],
    request: FusionRequest,
    context,
    runner: ParticipantRunner,
    *,
    prompt_builder,
    parent_agent=None,
    progress_callback=None,
    toolset: str = "fusion_readonly",
    write_root: str | None = None,
) -> list[FusionParticipantResult]:
    results: list[FusionParticipantResult] = []
    executor = ThreadPoolExecutor(max_workers=max(1, len(specs)))
    futures = {
        executor.submit(
            runner,
            spec,
            request,
            context,
            parent_agent=parent_agent,
            progress_callback=progress_callback,
            phase=phase,
            phase_prompt=prompt_builder(spec),
            toolset=toolset,
            write_root=write_root,
        ): spec
        for spec in specs
    }
    done, pending = wait(set(futures), timeout=request.timeout_seconds + 5)
    for future in done:
        spec = futures[future]
        try:
            result = future.result()
            result.phase = phase
            results.append(result)
        except Exception as exc:
            results.append(FusionParticipantResult(spec=spec, status="error", phase=phase, error=str(exc)))
    for future in pending:
        spec = futures[future]
        future.cancel()
        results.append(
            FusionParticipantResult(
                spec=spec,
                status="timeout",
                phase=phase,
                error=f"Participant did not finish within {request.timeout_seconds}s",
            )
        )
    executor.shutdown(wait=False, cancel_futures=True)
    order = {spec.slug: idx for idx, spec in enumerate(specs)}
    results.sort(key=lambda p: order.get(p.spec.slug, 9999))
    return results


def _phase_summary(result: FusionResult) -> dict[str, dict[str, int]]:
    return {
        phase: {"successful": len([p for p in items if p.ok]), "total": len(items)}
        for phase, items in result.phases.items()
    }


def _refresh_coverage(result: FusionResult, specs: list[FusionParticipantSpec]) -> None:
    requested = len(specs) or result.request.participants
    draft_successful = len([p for p in result.participants if p.ok])
    phase_summary = _phase_summary(result)
    degraded = bool(requested and draft_successful and draft_successful < requested)
    any_phase_incomplete = any(item["successful"] < item["total"] for item in phase_summary.values() if item["total"])
    result.coverage = {
        "requested": requested,
        "draft_successful": draft_successful,
        "total": requested,
        "degraded": degraded or any_phase_incomplete,
        "phases": phase_summary,
    }


def _set_decision(result: FusionResult) -> None:
    if result.status == "converged":
        result.decision = "degraded_consensus" if (result.coverage or {}).get("degraded") else "consensus"
    elif result.status == "operator_decision":
        result.decision = "operator_decision"
    elif result.status == "model_diversity_error":
        result.decision = "blocked_model_diversity"
    elif result.status == "degraded_insufficient_participants":
        result.decision = "blocked_degraded_insufficient_participants"
    elif result.status == "write_leak":
        result.decision = "blocked_write_leak"
    elif result.status == "failed":
        result.decision = "failed"
    else:
        result.decision = result.status


def _write_result(
    result: FusionResult,
    specs: list[FusionParticipantSpec],
    synthesis_docs: dict[str, str] | None = None,
) -> FusionResult:
    _refresh_coverage(result, specs)
    _set_decision(result)
    return write_fusion_artifacts(result, synthesis_docs)


def _write_leak_result(result: FusionResult, repo_guard, specs: list[FusionParticipantSpec]) -> FusionResult:
    result.repo_guard = repo_guard
    result.status = "write_leak"
    return _write_result(
        result,
        specs,
        {"synthesis/write_leak_report.md": synthesize_write_leak_report(repo_guard.diff_summary)},
    )


def _check_write_leak(result: FusionResult, guard: RepoMutationGuard, before, specs: list[FusionParticipantSpec]) -> FusionResult | None:
    repo_guard = guard.run_after(before)
    result.repo_guard = repo_guard
    if repo_guard.write_leak:
        return _write_leak_result(result, repo_guard, specs)
    return None


def _target_for_cross_verify(
    spec: FusionParticipantSpec,
    specs: list[FusionParticipantSpec],
    drafts: list[FusionParticipantResult],
) -> FusionParticipantResult | None:
    by_slug = {draft.spec.slug: draft for draft in drafts}
    if not specs:
        return None
    try:
        idx = [s.slug for s in specs].index(spec.slug)
    except ValueError:
        idx = 0
    for offset in range(1, len(specs) + 1):
        target_spec = specs[(idx + offset) % len(specs)]
        target = by_slug.get(target_spec.slug)
        if target is not None and target.spec.slug != spec.slug:
            return target
    return None


def _attach_spike_diff(result: FusionParticipantResult, spike) -> FusionParticipantResult:
    result.metadata["spike"] = spike.to_dict()
    if spike.diff_stat or spike.diff:
        sections = [result.output.rstrip(), "", "## Captured Spike Diff"]
        if spike.diff_stat:
            sections.extend(["", "### Diff Stat", "```", spike.diff_stat, "```"])
        if spike.diff:
            sections.extend(["", "### Diff", "```diff", spike.diff, "```"])
        result.output = "\n".join(sections).rstrip() + "\n"
    return result


def _run_spike_phase(
    round_index: int,
    specs: list[FusionParticipantSpec],
    request: FusionRequest,
    context,
    runner: ParticipantRunner,
    *,
    report,
    candidate: FusionCandidate,
    result: FusionResult,
    parent_agent=None,
    progress_callback=None,
) -> list[FusionParticipantResult]:
    phase = f"spike-{round_index}"
    outputs: list[FusionParticipantResult] = []
    for spec in specs:
        spike = create_spike_worktree(context.repo_root, result.run_dir, round_index, spec.slug)
        result.spikes.append(spike)
        if not spike.available or not spike.worktree_path:
            continue
        spike_context = build_context_packet(spike.worktree_path)
        try:
            phase_results = _run_phase(
                phase,
                [spec],
                request,
                spike_context,
                runner,
                prompt_builder=lambda participant, worktree=spike.worktree_path: build_spike_prompt(
                    participant,
                    request,
                    spike_context,
                    report,
                    candidate,
                    worktree_root=worktree,
                    brief=result.brief,
                ),
                parent_agent=parent_agent,
                progress_callback=progress_callback,
                toolset="fusion_spike",
                write_root=spike.worktree_path,
            )
            spike = capture_spike_diff(spike)
            phase_results = [_attach_spike_diff(item, spike) for item in phase_results]
            outputs.extend(phase_results)
        finally:
            spike = cleanup_spike_worktree(context.repo_root, spike)
            result.spikes[-1] = spike
            for item in outputs:
                if item.metadata.get("spike", {}).get("phase") == spike.phase and item.spec.slug == spec.slug:
                    item.metadata["spike"] = spike.to_dict()
    return outputs


def _operator_decision_result(
    result: FusionResult,
    specs: list[FusionParticipantSpec],
    conflicts,
) -> FusionResult:
    result.status = "operator_decision"
    decision = operator_decision_from_conflicts(conflicts)
    result.operator_decision = decision
    _refresh_coverage(result, specs)
    _set_decision(result)
    return write_fusion_artifacts(
        result,
        {
            "synthesis/operator_decision.md": synthesize_operator_decision(decision, result=result),
            "synthesis/fork_options.md": synthesize_fork_options(decision),
        },
    )


def run_fusion(
    request: FusionRequest,
    *,
    parent_agent=None,
    progress_callback=None,
    participant_runner: ParticipantRunner | None = None,
    config: dict | None = None,
) -> FusionResult:
    request = normalize_request(request, config=config)
    run_dir = create_run_dir(request.task, request.output_root)
    result = FusionResult(status="failed", request=request, run_dir=str(run_dir))
    runner = participant_runner or default_run_participant
    specs: list[FusionParticipantSpec] = []

    if not request.task:
        result.error = "Fusion task is required."
        return write_fusion_artifacts(result)

    try:
        context = build_context_packet(request.repo_path)
        specs = participant_specs_for_request(request, config=config)
        result.model_diversity = model_diversity_summary(specs, request.min_distinct_models)
        result.routing = classify_fusion_task(request.task)
        result.brief = build_reference_brief(request, context, routing=result.routing)
    except ModelDiversityError as exc:
        result.status = "model_diversity_error"
        result.error = str(exc)
        _set_decision(result)
        return write_fusion_artifacts(result)
    except Exception as exc:
        result.error = str(exc)
        _set_decision(result)
        return write_fusion_artifacts(result)

    guard = RepoMutationGuard(context.repo_root)
    before = guard.snapshot()

    if result.routing.get("locate_required"):
        locate = _run_phase(
            "locate",
            specs,
            request,
            context,
            runner,
            prompt_builder=lambda spec: build_locate_prompt(spec, request, context, result.brief),
            parent_agent=parent_agent,
            progress_callback=progress_callback,
        )
        result.phases["locate"] = locate
        leaked = _check_write_leak(result, guard, before, specs)
        if leaked:
            return leaked
        result.brief = build_reference_brief(request, context, routing=result.routing, locate_results=locate)

    drafts = _run_phase(
        "draft",
        specs,
        request,
        context,
        runner,
        prompt_builder=lambda spec: build_draft_prompt(spec, request, context, result.brief),
        parent_agent=parent_agent,
        progress_callback=progress_callback,
    )
    result.phases["draft"] = drafts
    result.participants = drafts
    leaked = _check_write_leak(result, guard, before, specs)
    if leaked:
        return leaked

    draft_report = verify_participant_outputs(drafts)
    if len(draft_report.successful_participants) < request.min_successful_participants:
        result.verification = draft_report
        gate = evaluate_consensus_gate(draft_report, request)
        result.gate = gate
        result.status = gate.status
        result.error = "; ".join(gate.reasons)
        return _write_result(result, specs)

    cross_verifications = _run_phase(
        "cross-verify-1",
        specs,
        request,
        context,
        runner,
        prompt_builder=lambda spec: build_cross_verify_prompt(
            spec,
            request,
            context,
            _target_for_cross_verify(spec, specs, drafts),
            result.brief,
        ),
        parent_agent=parent_agent,
        progress_callback=progress_callback,
    )
    result.phases["cross-verify-1"] = cross_verifications
    leaked = _check_write_leak(result, guard, before, specs)
    if leaked:
        return leaked

    wrong_layer_results = _run_phase(
        "wrong-layer",
        specs,
        request,
        context,
        runner,
        prompt_builder=lambda spec: build_wrong_layer_prompt(
            spec,
            request,
            context,
            drafts,
            cross_verifications,
            result.brief,
        ),
        parent_agent=parent_agent,
        progress_callback=progress_callback,
    )
    result.phases["wrong-layer"] = wrong_layer_results
    leaked = _check_write_leak(result, guard, before, specs)
    if leaked:
        return leaked

    all_debates: list[FusionParticipantResult] = []
    all_probes: list[FusionParticipantResult] = []
    all_spikes: list[FusionParticipantResult] = []
    all_premortems: list[FusionParticipantResult] = []
    candidate: FusionCandidate | None = None
    report = None
    max_rounds = max(request.debate_rounds, request.convergence_rounds)
    for round_index in range(1, max_rounds + 1):
        if round_index <= request.debate_rounds:
            phase = "debate" if request.debate_rounds == 1 else f"debate-{round_index}"
            debates = _run_phase(
                phase,
                specs,
                request,
                context,
                runner,
                prompt_builder=lambda spec, r=round_index, previous=tuple(all_debates), probes=tuple(all_probes), spikes=tuple(all_spikes), cand=candidate, feedback=report: build_debate_prompt(
                    spec,
                    request,
                    context,
                    drafts,
                    round_index=r,
                    previous_debates=list(previous),
                    previous_candidate=cand,
                    vote_feedback=feedback,
                    cross_verifications=cross_verifications,
                    wrong_layer_results=wrong_layer_results,
                    probe_results=list(probes),
                    spike_results=list(spikes),
                    brief=result.brief,
                ),
                parent_agent=parent_agent,
                progress_callback=progress_callback,
            )
            result.phases[phase] = debates
            all_debates.extend(debates)
            leaked = _check_write_leak(result, guard, before, specs)
            if leaked:
                return leaked

        if round_index > request.convergence_rounds:
            continue

        candidate = synthesize_candidate_plan(
            request,
            drafts,
            all_debates,
            round_index=round_index,
            previous_candidate=candidate,
            vote_feedback=report,
            cross_verifications=cross_verifications,
            wrong_layer_results=wrong_layer_results,
            probe_results=all_probes,
            spike_results=all_spikes,
            premortem_results=all_premortems,
            brief=result.brief,
        )
        result.candidates.append(candidate)

        premortem_phase = f"premortem-{round_index}"
        premortems = _run_phase(
            premortem_phase,
            specs,
            request,
            context,
            runner,
            prompt_builder=lambda spec, cand=candidate: build_premortem_prompt(
                spec,
                request,
                context,
                cand,
                all_debates,
                result.brief,
            ),
            parent_agent=parent_agent,
            progress_callback=progress_callback,
        )
        result.phases[premortem_phase] = premortems
        all_premortems.extend(premortems)
        leaked = _check_write_leak(result, guard, before, specs)
        if leaked:
            return leaked

        phase = f"vote-{round_index}"
        vote_results = _run_phase(
            phase,
            specs,
            request,
            context,
            runner,
            prompt_builder=lambda spec, cand=candidate, prem=tuple(premortems): build_vote_prompt(
                spec,
                request,
                context,
                cand,
                drafts,
                all_debates,
                cross_verifications=cross_verifications,
                wrong_layer_results=wrong_layer_results,
                premortem_results=list(prem),
            ),
            parent_agent=parent_agent,
            progress_callback=progress_callback,
        )
        result.phases[phase] = vote_results
        leaked = _check_write_leak(result, guard, before, specs)
        if leaked:
            return leaked

        report = verify_convergence_votes(
            vote_results,
            candidate_id=candidate.id,
            total_participants=len(specs),
            model_diversity=result.model_diversity,
        )
        result.verification = report
        result.votes.extend(report.votes)
        gate = evaluate_consensus_gate(report, request)
        result.gate = gate
        if gate.passed:
            result.status = "converged"
            _refresh_coverage(result, specs)
            _set_decision(result)
            return write_fusion_artifacts(
                result,
                {
                    "synthesis/final_plan.md": synthesize_final_plan(request, report, candidate, result=result),
                    "synthesis/recommendations.md": synthesize_recommendations(request, report),
                },
            )
        if gate.status != "operator_decision":
            result.status = gate.status
            result.error = "; ".join(gate.reasons)
            return _write_result(result, specs)
        has_more_rounds = round_index < max_rounds and (
            round_index < request.debate_rounds or round_index < request.convergence_rounds
        )
        if not has_more_rounds:
            return _operator_decision_result(result, specs, gate.conflicts)

        spike_results: list[FusionParticipantResult] = []
        if request.spike_worktrees:
            spike_phase = f"spike-{round_index}"
            spike_results = _run_spike_phase(
                round_index,
                specs,
                request,
                context,
                runner,
                report=report,
                candidate=candidate,
                result=result,
                parent_agent=parent_agent,
                progress_callback=progress_callback,
            )
            if spike_results:
                result.phases[spike_phase] = spike_results
                all_spikes.extend(spike_results)
                leaked = _check_write_leak(result, guard, before, specs)
                if leaked:
                    return leaked

        if not spike_results:
            probe_phase = f"probe-{round_index}"
            probes = _run_phase(
                probe_phase,
                specs,
                request,
                context,
                runner,
                prompt_builder=lambda spec, rep=report, cand=candidate: build_probe_prompt(
                    spec,
                    request,
                    context,
                    rep,
                    cand,
                    result.brief,
                ),
                parent_agent=parent_agent,
                progress_callback=progress_callback,
            )
            result.phases[probe_phase] = probes
            all_probes.extend(probes)
            leaked = _check_write_leak(result, guard, before, specs)
            if leaked:
                return leaked

    return _operator_decision_result(result, specs, result.gate.conflicts if result.gate else [])
