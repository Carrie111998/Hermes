"""Fail-closed artifact certification for Multica-launched ACP turns.

Multica supplies a deterministic contract in the first prompt. The ACP runtime
snapshots that contract before model execution, buffers model prose, writes the
artifact to a runtime-owned path, and emits prose only after certification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.artifact_certification import (
    ArtifactContract,
    CertificationResult,
    CertifiedArtifactWrapper,
    ExactCountCriterion,
)
from hermes_constants import get_hermes_home

_CONTRACT_RE = re.compile(
    r"<HERMES_ARTIFACT_CONTRACT>\s*(.*?)\s*</HERMES_ARTIFACT_CONTRACT>",
    re.DOTALL,
)
_MAX_CONTRACT_BYTES = 64 * 1024
_MAX_CRITERIA = 32
_MAX_NAME_CHARS = 200
_MAX_TEXT_CHARS = 4096
_MAX_EXPECTED_COUNT = 10_000


class CertificationContractError(ValueError):
    """The required client-owned artifact contract is absent or invalid."""


@dataclass(frozen=True)
class PreparedDispatchCertification:
    run_id: str
    wrapper: CertifiedArtifactWrapper


def certification_required() -> bool:
    """Return whether this ACP process is a fail-closed Multica runtime."""
    return os.environ.get("HERMES_MULTICA_ARTIFACT_CERTIFICATION", "").strip().lower() == "required"


def _contract_payload(user_text: str) -> dict[str, Any]:
    matches = _CONTRACT_RE.findall(user_text)
    if not matches:
        raise CertificationContractError(
            "required Multica artifact prompt is missing its HERMES_ARTIFACT_CONTRACT block"
        )
    if len(matches) != 1:
        raise CertificationContractError(
            "required Multica artifact prompt must contain exactly one "
            "HERMES_ARTIFACT_CONTRACT block"
        )
    raw = matches[0]
    if len(raw.encode("utf-8")) > _MAX_CONTRACT_BYTES:
        raise CertificationContractError("artifact contract is too long")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CertificationContractError(f"artifact contract is invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise CertificationContractError("artifact contract must be a JSON object")
    if payload.get("version") != 1:
        raise CertificationContractError("artifact contract version must be 1")
    unknown = set(payload) - {"version", "artifact_path", "criteria"}
    if unknown:
        raise CertificationContractError(
            f"artifact contract contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    artifact_path = payload.get("artifact_path")
    if not isinstance(artifact_path, str) or not artifact_path.strip():
        raise CertificationContractError(
            "artifact contract requires a non-empty artifact_path"
        )
    if "\x00" in artifact_path:
        raise CertificationContractError("artifact_path contains a NUL byte")
    return payload


def _criteria_from_payload(payload: dict[str, Any]) -> tuple[ExactCountCriterion, ...]:
    raw_criteria = payload.get("criteria")
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise CertificationContractError("artifact contract requires a non-empty criteria list")
    if len(raw_criteria) > _MAX_CRITERIA:
        raise CertificationContractError(f"artifact contract exceeds {_MAX_CRITERIA} criteria")

    criteria: list[ExactCountCriterion] = []
    names: set[str] = set()
    has_positive_requirement = False
    for index, item in enumerate(raw_criteria):
        if not isinstance(item, dict):
            raise CertificationContractError(f"criterion {index} must be a JSON object")
        if set(item) != {"name", "text", "expected_count"}:
            raise CertificationContractError(
                f"criterion {index} must contain only name, text, and expected_count"
            )
        name = item["name"]
        text = item["text"]
        expected_count = item["expected_count"]
        if not isinstance(name, str) or not name.strip() or len(name) > _MAX_NAME_CHARS:
            raise CertificationContractError(f"criterion {index} has an invalid name")
        if name in names:
            raise CertificationContractError(f"duplicate criterion name: {name}")
        if not isinstance(text, str) or not text:
            raise CertificationContractError(f"criterion {index} text must be non-empty")
        if len(text) > _MAX_TEXT_CHARS:
            raise CertificationContractError(f"criterion {index} text is too long")
        if (
            isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or not 0 <= expected_count <= _MAX_EXPECTED_COUNT
        ):
            raise CertificationContractError(f"criterion {index} expected_count is invalid")
        has_positive_requirement = has_positive_requirement or expected_count > 0
        names.add(name)
        criteria.append(ExactCountCriterion(name, text, expected_count))

    if not has_positive_requirement:
        raise CertificationContractError(
            "artifact contract must contain at least one positive exact-count requirement"
        )
    return tuple(criteria)


def prepare_dispatch_certification(
    *,
    user_text: str,
    session_id: str,
    history: list[dict[str, Any]] | None = None,
    workspace_root: str | Path | None = None,
    hermes_home: str | Path | None = None,
) -> PreparedDispatchCertification | None:
    """Snapshot a required Multica contract before any model call."""
    if not certification_required():
        return None
    if not isinstance(user_text, str):
        raise CertificationContractError("Multica artifact certification requires a text prompt")
    if not session_id.strip():
        raise CertificationContractError("Multica artifact certification requires a session id")

    payload = _contract_payload(user_text)
    criteria = _criteria_from_payload(payload)
    home = Path(hermes_home).expanduser().resolve() if hermes_home else get_hermes_home().resolve()
    root_source = workspace_root if workspace_root is not None else hermes_home or "."
    root = Path(root_source).expanduser().resolve()
    requested_artifact = Path(payload["artifact_path"]).expanduser()
    if requested_artifact.is_absolute():
        raise CertificationContractError("artifact_path must be relative to the workspace")
    artifact_path = (root / requested_artifact).resolve()
    if not artifact_path.is_relative_to(root):
        raise CertificationContractError("artifact_path escapes the workspace")
    # The identity is stable across a process crash before persistence, so a
    # retry can recover the wrapper's pending journal. Once a certified turn is
    # persisted, the changed safe history gives even an identical next prompt a
    # distinct identity.
    turn_payload = json.dumps(
        {
            "session_id": session_id,
            "user_text": user_text,
            "history": history or [],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    turn_id = hashlib.sha256(turn_payload.encode("utf-8")).hexdigest()[:32]
    digest = hashlib.sha256(f"{session_id}:{turn_id}".encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id).strip("._")[:80] or "session"
    output_path = home / "state" / "multica_artifacts" / f"{slug}-{turn_id}-{digest}.md"
    ledger_path = home / "state" / "artifact_certifications.db"
    run_id = f"multica:{session_id}:{turn_id}"
    wrapper = CertifiedArtifactWrapper(
        contract=ArtifactContract(
            output_path=output_path,
            workspace_root=root,
            artifact_path=requested_artifact,
            criteria=criteria,
        ),
        ledger_path=ledger_path,
    )
    return PreparedDispatchCertification(run_id=run_id, wrapper=wrapper)


def _failure_response(result: CertificationResult) -> str:
    failed = [
        f"{check.name}: expected {check.expected_count}, actual {check.actual_count}"
        for check in result.checks
        if not check.passed
    ]
    detail = "; ".join(failed) or "deterministic acceptance criteria failed"
    return (
        "ARTIFACT CERTIFICATION FAIL\n"
        f"Runtime-owned verification rejected the agent draft. {detail}\n"
        f"Certification run: {result.run_id}"
    )


def certify_dispatch_result(
    prepared: PreparedDispatchCertification | None,
    draft: str,
) -> tuple[str, CertificationResult | None]:
    """Certify buffered prose and return only runtime-approved client output."""
    if prepared is None:
        return draft, None
    result = prepared.wrapper.run(run_id=prepared.run_id, draft=draft)
    if result.status == "PASS":
        # The wrapper may have recovered an earlier immutable result after a
        # crash between ledger commit and ACP transcript persistence. Emit the
        # certified artifact bytes, not this retry's newly generated draft.
        artifact_bytes = Path(result.output_path).read_bytes()
        if hashlib.sha256(artifact_bytes).hexdigest() != result.artifact_hash:
            raise RuntimeError(
                f"certified artifact hash mismatch for recovered run {result.run_id}"
            )
        return artifact_bytes.decode("utf-8"), result
    return _failure_response(result), result


__all__ = [
    "CertificationContractError",
    "PreparedDispatchCertification",
    "certification_required",
    "certify_dispatch_result",
    "prepare_dispatch_certification",
]
