"""Turning a QC ``revise`` verdict into a tailor regeneration request.

The hazard here is not correctness, it is repetition. The applier ready sweep
runs every three hours and 7 of 380 packages currently score ``revise``; a
request emitted on every pass would fire roughly 56 premium generation calls a
day at the same seven packages, and not one of them would change anything.

So the idempotency key is derived from the **artifact hashes**, not the job.
Same bytes, same key, asked once. Regenerate the resume and the key changes, so
a genuinely new revision can be requested. The mailbox protocol already carries
``idempotency_key``, so this rides an existing field rather than inventing a
ledger.

``blocked`` deliberately produces nothing. A fabricated identity is a failure of
the generator's grip on ground truth; asking the same generator to try again is
not a fix, and that case is routed to a person instead.

Changes travel as bounded finding codes. A QC ``detail`` string can quote the
document, and this message lands in a mailbox.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping
import uuid

from .qc import QCFinding, QCResult, QCStatus

# What to ask the tailor to do about each deterministic finding. Kept as a
# fixed map so the request can never carry document text.
_CHANGE_FOR: dict[str, str] = {
    QCFinding.UNFILLED_PLACEHOLDER.value:
        "Replace the unfilled template marker with the real value for this application.",
    QCFinding.MISSING_ARTIFACT.value:
        "Regenerate the missing artifact using the canonical filename.",
    QCFinding.EMPTY_ARTIFACT.value:
        "Regenerate the artifact; the file on disk is empty.",
    QCFinding.STALE_RENDERING.value:
        "Re-render the PDF from the current markdown; the PDF is older than its source.",
}
_FALLBACK_CHANGE = "Re-check this artifact against the deterministic QC finding."


def revision_idempotency_key(job_id: str, artifact_hashes: Mapping[str, str]) -> str:
    """Stable per (job, exact artifact bytes).

    Sorted so dict ordering cannot produce two keys for one state, and the job
    id is folded in so an empty hash set does not collapse every job onto a
    single key.
    """
    payload = json.dumps(
        {"job_id": job_id, "artifacts": dict(sorted(artifact_hashes.items()))},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"qc-revision:{job_id}:{digest}"


def build_revision_request(
    job_id: Any,
    qc_result: QCResult,
    *,
    correlation_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a TAILOR_REVISION message, or None if none should be sent.

    Returns None for ``pass`` (nothing to fix), ``blocked`` (wants a person),
    a missing job id, and a ``revise`` carrying no findings — there is nothing
    to ask for in that last case, and an empty request would still cost a
    premium generation.
    """
    if not isinstance(job_id, str) or not job_id.strip():
        return None
    if qc_result.status is not QCStatus.REVISE:
        return None

    changes = [
        {
            "artifact": finding.artifact,
            "reason_code": finding.code.value,
            "change": _CHANGE_FOR.get(finding.code.value, _FALLBACK_CHANGE),
        }
        for finding in qc_result.findings
    ]
    if not changes:
        return None

    job_id = job_id.strip()
    return {
        "type": "TAILOR_REVISION",
        "protocol_version": "2.0",
        "message_id": str(uuid.uuid4()),
        "idempotency_key": revision_idempotency_key(job_id, qc_result.artifact_hashes),
        "from": "applier",
        "to": "tailor",
        "job_id": job_id,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "payload": {
            "job_id": job_id,
            # The live tailor cron prompt reads payload.changes ("make only the
            # requested edits from payload.changes"). The two historical
            # TAILOR_REVISION messages used feedback / what_to_change, which
            # that prompt does not read.
            "changes": changes,
            # Carried so the tailor — and any later reader — can tell whether
            # this request still describes the files on disk.
            "artifact_hashes": dict(qc_result.artifact_hashes),
            "policy_version": qc_result.policy_version,
            "source": "deterministic_qc",
        },
    }
