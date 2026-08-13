"""Turning a QC `revise` verdict into a tailor regeneration request.

Authorised 2026-08-13. The hazard is not correctness, it is repetition: the
applier ready sweep runs every 3 hours, and 7 of 380 packages currently score
`revise`. Emitting on every pass would fire ~56 premium regenerations a day for
the same seven packages, each one a P3 generation call, and none of them would
change anything.

So a request is keyed to the ARTIFACT STATE, not the job. Same bytes, same key,
emitted once. Regenerate the resume and the key changes, so a genuinely new
revision can be asked for. That is the whole design.

`blocked` deliberately produces nothing: a fabricated identity is not something
the generator should be asked to have another go at.
"""

from __future__ import annotations

import pytest

from jobflow_quality.qc import Finding, QCFinding, QCResult, QCStatus
from jobflow_quality.revision import (
    build_revision_request,
    revision_idempotency_key,
)

HASHES = {"resume.md": "a" * 64, "cover-letter.md": "b" * 64}


def _result(status, findings=(), hashes=None):
    return QCResult(status=status, findings=tuple(findings),
                    artifact_hashes=hashes if hashes is not None else HASHES,
                    policy_version=1)


def _revise(*codes):
    return _result(QCStatus.REVISE, [
        Finding(code, "resume.md", "detail text") for code in codes
    ])


class TestOnlyReviseRequestsRegeneration:
    def test_a_revise_verdict_produces_a_request(self):
        msg = build_revision_request("job-1", _revise(QCFinding.UNFILLED_PLACEHOLDER))
        assert msg is not None
        assert msg["type"] == "TAILOR_REVISION"

    def test_a_pass_produces_nothing(self):
        assert build_revision_request("job-1", _result(QCStatus.PASS)) is None

    def test_a_blocked_verdict_produces_nothing(self):
        """A fabricated identity is not a regeneration request — it wants a person."""
        result = _result(QCStatus.BLOCKED,
                         [Finding(QCFinding.IDENTITY_MISMATCH, "resume.md", "d")])
        assert build_revision_request("job-1", result) is None


class TestIdempotencyIsKeyedToArtifactState:
    def test_the_same_artifacts_yield_the_same_key(self):
        assert revision_idempotency_key("job-1", HASHES) == revision_idempotency_key("job-1", HASHES)

    def test_changed_artifacts_yield_a_different_key(self):
        changed = dict(HASHES, **{"resume.md": "c" * 64})
        assert revision_idempotency_key("job-1", HASHES) != revision_idempotency_key("job-1", changed)

    def test_different_jobs_yield_different_keys(self):
        assert revision_idempotency_key("job-1", HASHES) != revision_idempotency_key("job-2", HASHES)

    def test_key_order_does_not_matter(self):
        reordered = {"cover-letter.md": HASHES["cover-letter.md"],
                     "resume.md": HASHES["resume.md"]}
        assert revision_idempotency_key("job-1", HASHES) == revision_idempotency_key("job-1", reordered)

    def test_the_message_carries_the_key(self):
        msg = build_revision_request("job-1", _revise(QCFinding.UNFILLED_PLACEHOLDER))
        assert msg["idempotency_key"] == revision_idempotency_key("job-1", HASHES)

    def test_an_empty_hash_set_still_keys_deterministically(self):
        """No hashes must not collapse every job onto one key."""
        assert revision_idempotency_key("job-1", {}) != revision_idempotency_key("job-2", {})


class TestMessageShape:
    def test_changes_is_where_the_tailor_prompt_reads(self):
        """The live cron prompt says "make only the requested edits from
        payload.changes". The two historical messages used feedback /
        what_to_change instead, which that prompt does not read."""
        msg = build_revision_request("job-1", _revise(QCFinding.UNFILLED_PLACEHOLDER))
        assert isinstance(msg["payload"]["changes"], list)
        assert msg["payload"]["changes"]

    def test_the_envelope_matches_the_protocol(self):
        msg = build_revision_request("job-1", _revise(QCFinding.UNFILLED_PLACEHOLDER))
        for field in ("type", "from", "to", "job_id", "timestamp",
                      "correlation_id", "payload", "idempotency_key"):
            assert field in msg, field
        assert msg["to"] == "tailor"

    def test_each_actionable_finding_becomes_one_change(self):
        """Two content findings, two changes. stale_rendering is carved out
        separately — see TestStaleRenderingDoesNotWarrantRegeneration."""
        msg = build_revision_request("job-1", _revise(
            QCFinding.UNFILLED_PLACEHOLDER, QCFinding.MISSING_ARTIFACT))
        assert len(msg["payload"]["changes"]) == 2

    def test_changes_are_bounded_codes_not_document_text(self):
        secret = "confidential comp band 450000"
        result = _result(QCStatus.REVISE,
                         [Finding(QCFinding.UNFILLED_PLACEHOLDER, "resume.md", secret)])
        msg = build_revision_request("job-1", result)
        assert secret not in str(msg)

    def test_the_artifact_hashes_travel_so_staleness_is_checkable(self):
        msg = build_revision_request("job-1", _revise(QCFinding.UNFILLED_PLACEHOLDER))
        assert msg["payload"]["artifact_hashes"] == HASHES

    def test_a_supplied_correlation_id_is_preserved(self):
        msg = build_revision_request("job-1", _revise(QCFinding.UNFILLED_PLACEHOLDER),
                                     correlation_id="corr-7")
        assert msg["correlation_id"] == "corr-7"


class TestMalformedInput:
    @pytest.mark.parametrize("job_id", ("", "   ", None))
    def test_a_missing_job_id_produces_nothing(self, job_id):
        assert build_revision_request(job_id, _revise(QCFinding.UNFILLED_PLACEHOLDER)) is None

    def test_a_revise_with_no_findings_produces_nothing(self):
        """Nothing to ask for means nothing to send."""
        assert build_revision_request("job-1", _result(QCStatus.REVISE)) is None


class TestStaleRenderingDoesNotWarrantRegeneration:
    """Measured carve-out: 6 of the 7 revise verdicts today are a stale PDF.

    `stale_rendering` means the PDF is older than the markdown it was rendered
    from. Re-rendering is deterministic — several packages already ship a
    generate_pdf.py — so asking a premium model to rewrite the whole package is
    the wrong tool at roughly 100x the price of the right one.

    The finding still BLOCKS submission, which is correct: a stale PDF is the
    wrong document to send. It simply does not buy a regeneration.
    """

    def test_a_stale_pdf_alone_requests_nothing(self):
        assert build_revision_request("job-1", _revise(QCFinding.STALE_RENDERING)) is None

    def test_a_content_finding_still_requests(self):
        msg = build_revision_request("job-1", _revise(QCFinding.UNFILLED_PLACEHOLDER))
        assert msg is not None

    def test_a_mixed_verdict_requests_only_the_content_changes(self):
        """Regenerating re-renders the PDF anyway, so asking for it is noise."""
        msg = build_revision_request("job-1", _revise(
            QCFinding.UNFILLED_PLACEHOLDER, QCFinding.STALE_RENDERING))
        codes = [c["reason_code"] for c in msg["payload"]["changes"]]
        assert codes == ["unfilled_placeholder"]

    def test_the_carve_out_is_explicit_not_incidental(self):
        from jobflow_quality.revision import NO_REGENERATION_CODES

        assert QCFinding.STALE_RENDERING.value in NO_REGENERATION_CODES
        assert QCFinding.UNFILLED_PLACEHOLDER.value not in NO_REGENERATION_CODES
