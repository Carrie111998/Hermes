"""Deterministic cross-verification for Fusion v2."""

from __future__ import annotations

import json
import re
import string
from typing import Any

from .models import (
    MATERIAL_AXES,
    FusionConflict,
    FusionConsensusItem,
    FusionConvergenceVote,
    FusionFinding,
    FusionParticipantResult,
    FusionVerificationReport,
)

_AXIS_ALIASES = {axis.replace("_", " "): axis for axis in MATERIAL_AXES}
_AXIS_ALIASES.update({axis.replace("_", "-"): axis for axis in MATERIAL_AXES})
_AXIS_ALIASES.update({axis: axis for axis in MATERIAL_AXES})
_AXIS_RE = re.compile(r"^\s*[-*]?\s*([A-Za-z0-9_ -]+)\s*:\s*(.+?)\s*$")
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _canonical_axis(raw: str) -> str | None:
    key = raw.strip().lower().replace("/", " ")
    key = re.sub(r"\s+", " ", key)
    key = key.replace("-", "_").replace(" ", "_")
    return key if key in MATERIAL_AXES else _AXIS_ALIASES.get(raw.strip().lower())


def _normalize_claim(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[`*_#>\[\]()]", " ", text)
    text = text.translate(str.maketrans("", "", string.punctuation.replace("/", "")))
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _json_blocks(output: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for match in _JSON_BLOCK_RE.finditer(output or ""):
        try:
            parsed = json.loads(match.group(1))
        except Exception:
            continue
        if isinstance(parsed, dict):
            blocks.append(parsed)
    stripped = (output or "").strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                blocks.append(parsed)
        except Exception:
            pass
    return blocks


def _extract_json_axes(output: str) -> dict[str, str]:
    for parsed in _json_blocks(output):
        axes = parsed.get("material_axes") if isinstance(parsed, dict) else None
        if isinstance(axes, dict):
            result: dict[str, str] = {}
            for raw_axis, claim in axes.items():
                axis = _canonical_axis(str(raw_axis))
                if axis and claim is not None:
                    result[axis] = str(claim).strip()
            if result:
                return result
    return {}


def extract_material_axes(output: str) -> dict[str, str]:
    axes = _extract_json_axes(output)
    if axes:
        return axes
    found: dict[str, str] = {}
    in_material_section = False
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("## material axes"):
            in_material_section = True
            continue
        if in_material_section and stripped.startswith("## "):
            in_material_section = False
        match = _AXIS_RE.match(line)
        if not match:
            continue
        axis = _canonical_axis(match.group(1))
        if axis:
            found[axis] = match.group(2).strip()
    return found


def _listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip() and str(v).strip().lower() != "none"]
    text = str(value).strip()
    if not text or text.lower() == "none":
        return []
    return [text]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "approved", "approve"}
    return bool(value)


def parse_convergence_vote(participant: FusionParticipantResult, candidate_id: str) -> FusionConvergenceVote:
    for parsed in _json_blocks(participant.output):
        if "approved" in parsed or "candidate_id" in parsed:
            return FusionConvergenceVote(
                participant=participant.spec.slug,
                candidate_id=str(parsed.get("candidate_id") or candidate_id),
                approved=_truthy(parsed.get("approved")),
                material_dissent=_listify(parsed.get("material_dissent")),
                required_changes=_listify(parsed.get("required_changes")),
                unsupported_claims=_listify(parsed.get("unsupported_claims")),
                confidence=str(parsed.get("confidence") or "").strip(),
                summary=str(parsed.get("summary") or "").strip(),
            )

    approved = False
    material_dissent: list[str] = []
    required_changes: list[str] = []
    for line in (participant.output or "").splitlines():
        lower = line.strip().lower()
        if lower.startswith("approved:"):
            approved = _truthy(line.split(":", 1)[1])
        elif lower.startswith("material_dissent:") or lower.startswith("material dissent:"):
            material_dissent.extend(_listify(line.split(":", 1)[1]))
        elif lower.startswith("required_changes:") or lower.startswith("required changes:"):
            required_changes.extend(_listify(line.split(":", 1)[1]))

    if not approved and not material_dissent and not required_changes:
        material_dissent = ["missing structured convergence vote"]
    return FusionConvergenceVote(
        participant=participant.spec.slug,
        candidate_id=candidate_id,
        approved=approved,
        material_dissent=material_dissent,
        required_changes=required_changes,
        confidence="",
        summary="fallback vote parse" if approved else "missing or rejecting vote",
    )


def _unsupported_claims(participant: FusionParticipantResult) -> list[FusionFinding]:
    claims: list[FusionFinding] = []
    for line in (participant.output or "").splitlines():
        lower = line.lower()
        if "unsupported:" in lower or "[unsupported]" in lower:
            claims.append(
                FusionFinding(
                    axis="unsupported",
                    participant=participant.spec.slug,
                    claim=line.strip(" -*"),
                )
            )
    return claims


def verify_participant_outputs(participants: list[FusionParticipantResult]) -> FusionVerificationReport:
    successful = [p for p in participants if p.ok]
    participant_axes = {p.spec.slug: extract_material_axes(p.output) for p in successful}
    matrix: dict[str, dict[str, str]] = {axis: {} for axis in MATERIAL_AXES}
    consensus: list[FusionConsensusItem] = []
    conflicts: list[FusionConflict] = []

    for axis in MATERIAL_AXES:
        claims = {slug: axes.get(axis, "").strip() for slug, axes in participant_axes.items()}
        for slug, claim in claims.items():
            matrix[axis][slug] = claim
        normalized = {slug: _normalize_claim(claim) for slug, claim in claims.items() if claim}
        missing = [slug for slug, claim in claims.items() if not claim]
        unique = {claim for claim in normalized.values() if claim}
        if successful and not missing and len(unique) == 1:
            summary = next(iter(claims.values()))
            consensus.append(FusionConsensusItem(axis=axis, agreed=True, summary=summary, participants=list(claims.keys())))
        else:
            reason = "missing material-axis claim" if missing else "material disagreement"
            conflict_claims = {slug: (claim or "<missing>") for slug, claim in claims.items()}
            conflicts.append(
                FusionConflict(
                    axis=axis,
                    summary=f"{reason} on {axis}",
                    claims=conflict_claims,
                    participants=list(claims.keys()),
                    material=True,
                )
            )

    unsupported: list[FusionFinding] = []
    for participant in successful:
        unsupported.extend(_unsupported_claims(participant))

    return FusionVerificationReport(
        matrix=matrix,
        consensus_items=consensus,
        conflicts=conflicts,
        unsupported_claims=unsupported,
        successful_participants=[p.spec.slug for p in successful],
        total_participants=len(participants),
    )


def verify_convergence_votes(
    vote_results: list[FusionParticipantResult],
    *,
    candidate_id: str,
    total_participants: int,
    model_diversity: dict[str, Any] | None = None,
) -> FusionVerificationReport:
    successful = [p for p in vote_results if p.ok]
    votes = [parse_convergence_vote(p, candidate_id) for p in successful]
    approved = [v.participant for v in votes if v.approved and not v.material_dissent]
    rejected = [v.participant for v in votes if not v.approved or v.material_dissent]
    conflicts: list[FusionConflict] = []
    matrix = {"convergence_vote": {}}
    for vote in votes:
        matrix["convergence_vote"][vote.participant] = "approved" if vote.approved else "rejected"
        claims: dict[str, str] = {}
        if vote.material_dissent:
            claims[vote.participant] = "; ".join(vote.material_dissent)
        elif not vote.approved:
            claims[vote.participant] = vote.summary or "participant rejected candidate"
        if claims:
            conflicts.append(
                FusionConflict(
                    axis="convergence_vote",
                    summary=f"{vote.participant} did not approve candidate {candidate_id}",
                    claims=claims,
                    participants=[vote.participant],
                    material=True,
                )
            )
        if vote.unsupported_claims:
            conflicts.append(
                FusionConflict(
                    axis="unsupported_claims",
                    summary=f"{vote.participant} found unsupported candidate claims",
                    claims={vote.participant: "; ".join(vote.unsupported_claims)},
                    participants=[vote.participant],
                    material=True,
                )
            )
    missing = [p.spec.slug for p in vote_results if not p.ok]
    if missing:
        conflicts.append(
            FusionConflict(
                axis="convergence_vote",
                summary="missing convergence vote output",
                claims={slug: "<missing vote>" for slug in missing},
                participants=missing,
                material=True,
            )
        )
    consensus = []
    if votes and not conflicts and len(approved) == len(votes):
        consensus.append(
            FusionConsensusItem(
                axis="convergence_vote",
                agreed=True,
                summary=f"All successful participants approved candidate {candidate_id} with no material dissent.",
                participants=approved,
            )
        )
    unsupported = [
        FusionFinding(axis="unsupported_claims", participant=v.participant, claim=claim)
        for v in votes
        for claim in v.unsupported_claims
    ]
    return FusionVerificationReport(
        matrix=matrix,
        consensus_items=consensus,
        conflicts=conflicts,
        unsupported_claims=unsupported,
        successful_participants=[p.spec.slug for p in successful],
        total_participants=total_participants,
        candidate_id=candidate_id,
        votes=votes,
        approved_participants=approved,
        rejected_participants=rejected,
        model_diversity=model_diversity or {},
    )
