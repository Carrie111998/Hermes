"""Artifact writer for Fusion v2."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from .models import FusionResult


def _slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", text.strip().lower()).strip("-._")
    return (slug or "fusion-run")[:max_len]


def create_run_dir(task: str, output_root: str | None = None) -> Path:
    root = Path(output_root).expanduser() if output_root else get_hermes_home() / "fusion" / "runs"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / f"{stamp}-{_slugify(task)}"
    counter = 2
    candidate = run_dir
    while candidate.exists():
        candidate = root / f"{stamp}-{_slugify(task)}-{counter}"
        counter += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rel(run_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(run_dir))
    except ValueError:
        return str(path)


def _all_phase_results(result: FusionResult):
    seen: set[tuple[str, str]] = set()
    for phase, results in result.phases.items():
        for item in results:
            key = (phase, item.spec.slug)
            if key not in seen:
                seen.add(key)
                yield phase, item
    if not result.phases:
        for item in result.participants:
            yield item.phase or "draft", item


def _consensus_report_markdown(result: FusionResult) -> str:
    report = result.verification
    if report is None:
        return "# Fusion Consensus Report\n\nVerification did not run.\n"
    lines = ["# Fusion Consensus Report", ""]
    lines.append(f"Successful participants: {len(report.successful_participants)}/{report.total_participants}")
    if report.candidate_id:
        lines.append(f"Candidate: `{report.candidate_id}`")
    lines.append("")
    if report.consensus_items:
        lines.append("## Consensus")
        for item in report.consensus_items:
            lines.append(f"- **{item.axis}:** {item.summary}")
        lines.append("")
    if report.votes:
        lines.append("## Votes")
        for vote in report.votes:
            lines.append(f"- **{vote.participant}:** approved={vote.approved}; dissent={vote.material_dissent or []}; required_changes={vote.required_changes or []}")
        lines.append("")
    if report.unsupported_claims:
        lines.append("## Unsupported Claims")
        for claim in report.unsupported_claims:
            lines.append(f"- **{claim.participant}:** {claim.claim}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _conflict_report_markdown(result: FusionResult) -> str:
    report = result.verification
    if report is None:
        return "# Fusion Conflict Report\n\nVerification did not run.\n"
    lines = ["# Fusion Conflict Report", ""]
    if not report.conflicts:
        lines.append("No material conflicts detected.")
    for conflict in report.conflicts:
        lines.append(f"## {conflict.axis}")
        lines.append(conflict.summary)
        for participant, claim in conflict.claims.items():
            lines.append(f"- **{participant}:** {claim}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _phase_summary(result: FusionResult) -> dict[str, Any]:
    return {
        phase: {
            "successful": len([p for p in items if p.ok]),
            "total": len(items),
            "participants": [p.spec.slug for p in items],
        }
        for phase, items in result.phases.items()
    }


def write_fusion_artifacts(result: FusionResult, synthesis_docs: dict[str, str] | None = None) -> FusionResult:
    run_dir = Path(result.run_dir)
    artifacts: dict[str, str] = {}

    manifest = {
        "schema": "fusion-v2-run/v2",
        "request": result.request.to_dict(),
        "status": result.status,
        "decision": result.decision,
        "routing": result.routing,
        "model_diversity": result.model_diversity,
        "coverage": result.coverage or {
            "successful": len([p for p in result.participants if p.ok]),
            "total": len(result.participants),
            "degraded": False,
        },
        "phases": _phase_summary(result),
        "participants": [
            {
                "slug": p.spec.slug,
                "status": p.status,
                "phase": p.phase,
                "output_hash": p.output_hash,
                "duration_seconds": p.duration_seconds,
                "provider": p.provider or p.spec.provider,
                "model": p.model or p.spec.model,
                "runtime_label": p.spec.runtime_label,
            }
            for _phase, p in _all_phase_results(result)
        ],
        "candidates": [candidate.to_dict() for candidate in result.candidates],
        "spikes": [spike.to_dict() for spike in result.spikes],
    }
    manifest_path = run_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    artifacts["manifest"] = _rel(run_dir, manifest_path)

    routing_path = run_dir / "routing.json"
    _write_json(routing_path, result.routing or {})
    artifacts["routing"] = _rel(run_dir, routing_path)

    if result.brief:
        brief_json_path = run_dir / "brief" / "brief.json"
        brief_md_path = run_dir / "brief" / "brief.md"
        _write_json(brief_json_path, {k: v for k, v in result.brief.items() if k != "markdown"})
        _write_text(brief_md_path, str(result.brief.get("markdown") or ""))
        artifacts["brief_json"] = _rel(run_dir, brief_json_path)
        artifacts["brief"] = _rel(run_dir, brief_md_path)

    for phase, participant in _all_phase_results(result):
        p_dir = run_dir / "participants" / participant.spec.slug
        output_path = p_dir / f"{phase}.md"
        metadata_path = p_dir / f"{phase}.metadata.json"
        _write_text(output_path, participant.output or f"Participant status: {participant.status}\nError: {participant.error or ''}\n")
        _write_json(metadata_path, participant.to_dict())
        artifacts[f"participant:{participant.spec.slug}:{phase}:output"] = _rel(run_dir, output_path)
        artifacts[f"participant:{participant.spec.slug}:{phase}:metadata"] = _rel(run_dir, metadata_path)
        if phase == "draft":
            legacy_output = p_dir / "output.md"
            legacy_metadata = p_dir / "metadata.json"
            _write_text(legacy_output, participant.output or f"Participant status: {participant.status}\nError: {participant.error or ''}\n")
            _write_json(legacy_metadata, participant.to_dict())
            artifacts[f"participant:{participant.spec.slug}:output"] = _rel(run_dir, legacy_output)
            artifacts[f"participant:{participant.spec.slug}:metadata"] = _rel(run_dir, legacy_metadata)

    for candidate in result.candidates:
        path = run_dir / "synthesis" / f"{candidate.id}.md"
        _write_text(path, candidate.content)
        artifacts[f"candidate:{candidate.id}"] = _rel(run_dir, path)

    if result.spikes:
        spikes_path = run_dir / "spikes" / "spikes.json"
        _write_json(spikes_path, [spike.to_dict() for spike in result.spikes])
        artifacts["spikes"] = _rel(run_dir, spikes_path)
        for spike in result.spikes:
            phase_dir = run_dir / "spikes" / spike.phase
            if spike.diff_stat:
                stat_path = phase_dir / "diff.stat.txt"
                _write_text(stat_path, spike.diff_stat + "\n")
                artifacts[f"spike:{spike.phase}:diff_stat"] = _rel(run_dir, stat_path)
            if spike.diff:
                diff_path = phase_dir / "diff.patch"
                _write_text(diff_path, spike.diff + "\n")
                artifacts[f"spike:{spike.phase}:diff"] = _rel(run_dir, diff_path)

    votes_path = run_dir / "verification" / "votes.json"
    _write_json(votes_path, [vote.to_dict() for vote in result.votes])
    artifacts["votes"] = _rel(run_dir, votes_path)

    matrix_path = run_dir / "verification" / "verification_matrix.json"
    _write_json(matrix_path, result.verification.to_dict() if result.verification else {})
    artifacts["verification_matrix"] = _rel(run_dir, matrix_path)

    consensus_path = run_dir / "verification" / "consensus_report.md"
    conflict_path = run_dir / "verification" / "conflict_report.md"
    _write_text(consensus_path, _consensus_report_markdown(result))
    _write_text(conflict_path, _conflict_report_markdown(result))
    artifacts["consensus_report"] = _rel(run_dir, consensus_path)
    artifacts["conflict_report"] = _rel(run_dir, conflict_path)

    for rel_path, content in (synthesis_docs or {}).items():
        path = run_dir / rel_path
        _write_text(path, content)
        artifacts[rel_path.replace("/", ":").replace(".md", "")] = _rel(run_dir, path)

    status_payload = {
        "schema": "fusion-v2-status/v2",
        "status": result.status,
        "decision": result.decision,
        "write_leak": result.write_leak,
        "run_dir": str(run_dir),
        "artifacts": artifacts,
        "routing": result.routing,
        "coverage": result.coverage,
        "model_diversity": result.model_diversity,
        "phases": _phase_summary(result),
        "spikes": [spike.to_dict() for spike in result.spikes],
        "repo_guard": result.repo_guard.to_dict() if result.repo_guard else None,
        "gate": result.gate.to_dict() if result.gate else None,
        "error": result.error,
    }
    status_path = run_dir / "status.json"
    _write_json(status_path, status_payload)
    artifacts["status"] = _rel(run_dir, status_path)

    result.artifacts = artifacts
    return result
