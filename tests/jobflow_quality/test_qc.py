"""Deterministic quality control before an application is declared ready.

This is the last gate before a document reaches an employer, and the failure it
exists to stop is already in the historical data. Three of 428 packages carry a
fabricated surname across BOTH the resume and the cover letter — "Diego
Rodrigues", "Diego Resende" — two of them with placeholder emails
(`diego@email.com`). One cover letter is addressed to `[Company Name]`.

None of that needs a model to catch. The candidate's name and email are facts,
and a document that contradicts them is wrong however well written it is. So
the deterministic pass runs first and the expensive semantic review never runs
on a package that fails it.

The other half is staleness. A QC verdict authorizes the exact bytes it read;
regenerate the resume afterwards and the verdict is void. Otherwise "QC passed"
becomes a permanent property of a directory whose contents have since changed.
"""

from __future__ import annotations

import json

import pytest

from jobflow_quality.qc import (
    CandidateIdentity,
    QCFinding,
    QCStatus,
    check_application,
    is_ready,
)

IDENTITY = CandidateIdentity(
    full_name="Diego De Aragao",
    email="diegodearagao@gmail.com",
)

GOOD_RESUME = """# Diego De Aragao, CFA, CTP, FDP
> Contact: diegodearagao@gmail.com
## Professional Summary
Executive with a track record in financial technology.
"""

GOOD_COVER = """Diego De Aragao
diegodearagao@gmail.com

Hiring Manager
Acme Bank

I am writing to express interest in the Director role.
"""


def _package(tmp_path, resume=GOOD_RESUME, cover=GOOD_COVER, *, pdfs=True,
             metadata=None, qa=True):
    d = tmp_path / "pkg"
    d.mkdir(parents=True, exist_ok=True)
    (d / "resume.md").write_text(resume, encoding="utf-8")
    (d / "cover-letter.md").write_text(cover, encoding="utf-8")
    if pdfs:
        (d / "resume.pdf").write_bytes(b"%PDF-1.4 resume bytes")
        (d / "cover-letter.pdf").write_bytes(b"%PDF-1.4 cover bytes")
    if qa:
        (d / "qa-responses.json").write_text("{}", encoding="utf-8")
    (d / "metadata.json").write_text(
        json.dumps(metadata or {"company": "Acme Bank", "title": "Director"}),
        encoding="utf-8",
    )
    return d


class TestACleanPackagePasses:
    def test_a_complete_consistent_package_passes(self, tmp_path):
        r = check_application(_package(tmp_path), IDENTITY)
        assert r.status is QCStatus.PASS
        assert r.findings == ()

    def test_the_verdict_records_a_hash_per_artifact(self, tmp_path):
        r = check_application(_package(tmp_path), IDENTITY)
        assert set(r.artifact_hashes) >= {"resume.md", "cover-letter.md"}
        assert all(len(h) == 64 for h in r.artifact_hashes.values())


class TestFabricatedIdentityIsBlocked:
    """The defect actually found in production. Not revisable — blocked."""

    def test_a_wrong_surname_blocks(self, tmp_path):
        bad = GOOD_RESUME.replace("Diego De Aragao", "Diego Rodrigues")
        r = check_application(_package(tmp_path, resume=bad), IDENTITY)
        assert r.status is QCStatus.BLOCKED
        assert any(f.code is QCFinding.IDENTITY_MISMATCH for f in r.findings)

    def test_a_placeholder_email_blocks(self, tmp_path):
        bad = GOOD_RESUME.replace("diegodearagao@gmail.com", "diego@email.com")
        r = check_application(_package(tmp_path, resume=bad), IDENTITY)
        assert r.status is QCStatus.BLOCKED

    def test_a_wrong_identity_in_the_cover_letter_alone_still_blocks(self, tmp_path):
        bad = GOOD_COVER.replace("Diego De Aragao", "Diego Resende")
        r = check_application(_package(tmp_path, cover=bad), IDENTITY)
        assert r.status is QCStatus.BLOCKED

    def test_the_finding_names_the_offending_artifact(self, tmp_path):
        bad = GOOD_COVER.replace("Diego De Aragao", "Diego Resende")
        r = check_application(_package(tmp_path, cover=bad), IDENTITY)
        finding = next(f for f in r.findings if f.code is QCFinding.IDENTITY_MISMATCH)
        assert finding.artifact == "cover-letter.md"

    def test_identity_matching_tolerates_credentials_and_case(self, tmp_path):
        """`DIEGO DE ARAGAO, CFA` is the same person. Blocking that is a false alarm."""
        ok = GOOD_RESUME.replace("Diego De Aragao, CFA, CTP, FDP", "DIEGO DE ARAGAO, CFA")
        r = check_application(_package(tmp_path, resume=ok), IDENTITY)
        assert r.status is QCStatus.PASS

    def test_a_third_party_name_in_the_body_does_not_block(self, tmp_path):
        """Only the candidate's own identity block is authoritative."""
        ok = GOOD_COVER + "\nI worked with Maria Rodrigues at a prior firm.\n"
        r = check_application(_package(tmp_path, cover=ok), IDENTITY)
        assert r.status is QCStatus.PASS

    def test_a_third_party_email_below_the_identity_block_does_not_block(self, tmp_path):
        """Arms the identity window itself.

        A letter may legitimately quote a recruiter's address or a careers
        mailbox. Scanning the whole document for foreign emails would block
        every such letter, so the check is confined to the header where the
        candidate's own contact details live.
        """
        ok = GOOD_COVER + "\n" + ("Body paragraph. " * 200) + \
            "\nPlease copy careers@acmebank.com on any response.\n"
        r = check_application(_package(tmp_path, cover=ok), IDENTITY)
        assert r.status is QCStatus.PASS

    def test_a_foreign_email_inside_the_identity_block_still_blocks(self, tmp_path):
        """The other side of the same boundary — the header is authoritative."""
        bad = GOOD_COVER.replace("diegodearagao@gmail.com", "recruiter@agency.com")
        r = check_application(_package(tmp_path, cover=bad), IDENTITY)
        assert r.status is QCStatus.BLOCKED


class TestUnfilledPlaceholders:
    @pytest.mark.parametrize("marker", ("[Company Name]", "[INSERT ROLE]", "{{company}}", "TBD"))
    def test_a_template_marker_requires_revision(self, tmp_path, marker):
        bad = GOOD_COVER.replace("Acme Bank", marker)
        r = check_application(_package(tmp_path, cover=bad), IDENTITY)
        assert r.status is QCStatus.REVISE
        assert any(f.code is QCFinding.UNFILLED_PLACEHOLDER for f in r.findings)

    def test_ordinary_bracketed_text_is_not_a_placeholder(self, tmp_path):
        ok = GOOD_COVER + "\nSee [my portfolio](https://example.com) for detail.\n"
        r = check_application(_package(tmp_path, cover=ok), IDENTITY)
        assert r.status is QCStatus.PASS


class TestCompleteness:
    @pytest.mark.parametrize("missing", ("resume.md", "cover-letter.md", "resume.pdf"))
    def test_a_missing_required_artifact_requires_revision(self, tmp_path, missing):
        d = _package(tmp_path)
        (d / missing).unlink()
        r = check_application(d, IDENTITY)
        assert r.status is QCStatus.REVISE
        assert any(f.code is QCFinding.MISSING_ARTIFACT for f in r.findings)

    def test_an_empty_artifact_requires_revision(self, tmp_path):
        d = _package(tmp_path)
        (d / "resume.pdf").write_bytes(b"")
        r = check_application(d, IDENTITY)
        assert r.status is QCStatus.REVISE
        assert any(f.code is QCFinding.EMPTY_ARTIFACT for f in r.findings)

    def test_a_missing_package_directory_is_blocked_not_passed(self, tmp_path):
        r = check_application(tmp_path / "nope", IDENTITY)
        assert r.status is QCStatus.BLOCKED


class TestStaleRendering:
    def test_a_pdf_older_than_its_source_requires_revision(self, tmp_path):
        """The PDF is what gets sent. An outdated one sends the wrong document."""
        import os
        d = _package(tmp_path)
        pdf = d / "resume.pdf"
        os.utime(pdf, (1, 1))  # rendered long before the markdown
        r = check_application(d, IDENTITY)
        assert r.status is QCStatus.REVISE
        assert any(f.code is QCFinding.STALE_RENDERING for f in r.findings)


class TestSeverityOrdering:
    def test_a_block_outranks_a_revise(self, tmp_path):
        bad_resume = GOOD_RESUME.replace("Diego De Aragao", "Diego Rodrigues")
        bad_cover = GOOD_COVER.replace("Acme Bank", "[Company Name]")
        r = check_application(_package(tmp_path, resume=bad_resume, cover=bad_cover),
                              IDENTITY)
        assert r.status is QCStatus.BLOCKED
        assert len(r.findings) >= 2


class TestReadinessIsBoundToTheBytesThatWereChecked:
    """A pass authorizes those exact artifacts and nothing else."""

    def test_a_passing_verdict_makes_an_unchanged_package_ready(self, tmp_path):
        d = _package(tmp_path)
        assert is_ready(check_application(d, IDENTITY), d) is True

    def test_editing_an_artifact_after_the_pass_voids_readiness(self, tmp_path):
        d = _package(tmp_path)
        result = check_application(d, IDENTITY)
        (d / "resume.md").write_text(GOOD_RESUME + "\nAdded later.\n", encoding="utf-8")
        assert is_ready(result, d) is False

    def test_deleting_an_artifact_after_the_pass_voids_readiness(self, tmp_path):
        d = _package(tmp_path)
        result = check_application(d, IDENTITY)
        (d / "resume.pdf").unlink()
        assert is_ready(result, d) is False

    def test_a_failing_verdict_is_never_ready(self, tmp_path):
        bad = GOOD_RESUME.replace("Diego De Aragao", "Diego Rodrigues")
        d = _package(tmp_path, resume=bad)
        assert is_ready(check_application(d, IDENTITY), d) is False

    def test_readiness_against_a_different_package_is_false(self, tmp_path):
        d = _package(tmp_path)
        result = check_application(d, IDENTITY)
        other = _package(tmp_path / "other", resume=GOOD_RESUME + "\ndifferent\n")
        assert is_ready(result, other) is False


class TestContract:
    def test_findings_are_bounded_codes(self, tmp_path):
        bad = GOOD_RESUME.replace("Diego De Aragao", "Diego Rodrigues")
        r = check_application(_package(tmp_path, resume=bad), IDENTITY)
        assert all(isinstance(f.code, QCFinding) for f in r.findings)

    def test_the_result_is_immutable(self, tmp_path):
        r = check_application(_package(tmp_path), IDENTITY)
        with pytest.raises(AttributeError):
            r.status = QCStatus.PASS

    def test_the_policy_version_is_recorded(self, tmp_path):
        r = check_application(_package(tmp_path), IDENTITY, policy_version=7)
        assert r.policy_version == 7

    def test_no_artifact_body_leaks_into_a_finding(self, tmp_path):
        """Findings travel in messages; the resume must not travel with them."""
        secret = "Diego Rodrigues negotiated a confidential 450000 package"
        r = check_application(_package(tmp_path, resume=GOOD_RESUME.replace(
            "Diego De Aragao", secret)), IDENTITY)
        assert all("450000" not in f.detail for f in r.findings)


class TestNameMayLiveInTheSignOff:
    """Measured: 98 of 760 real artifacts name the candidate only at the bottom.

    A letter that opens with a contact line and signs off underneath is normal.
    Requiring the name in the header blocked 27% of a known-good corpus, so the
    name check spans the whole document while the foreign-email check stays in
    the header where the candidate's own details live.
    """

    def test_a_name_only_in_the_sign_off_passes(self, tmp_path):
        cover = (
            "St. Petersburg, FL | diegodearagao@gmail.com\n\n"
            "Hiring Manager\nAcme Bank\n\n"
            + ("I am a strong fit for this role. " * 60)
            + "\n\nSincerely,\nDiego De Aragao\n"
        )
        r = check_application(_package(tmp_path, cover=cover), IDENTITY)
        assert r.status is QCStatus.PASS

    def test_a_name_absent_everywhere_still_blocks(self, tmp_path):
        cover = (
            "St. Petersburg, FL | diegodearagao@gmail.com\n\n"
            "Hiring Manager\nAcme Bank\n\nSincerely,\nDiego Rodrigues\n"
        )
        r = check_application(_package(tmp_path, cover=cover), IDENTITY)
        assert r.status is QCStatus.BLOCKED


class TestStalenessThresholdSeparatesWriteOrderFromRealStaleness:
    """The 1-second slack was too tight and produced mostly false positives.

    Measured across all 428 real packages, `md newer than pdf` is bimodal:

        38.4s  39.5s  40.0s  55.0s  151.6s   <- one generation pass, write order
        73414.0s (20.4h)  x2                 <- genuinely regenerated afterwards

    Three orders of magnitude separate them, so any threshold inside that gap
    is safe. 15 minutes sits well above the observed 152s spread and far below
    20 hours. At 1 second, 5 of 6 flagged packages were the markdown simply
    landing a minute after its PDF in the same run — and each one blocked a
    submission and would have bought a regeneration.
    """

    def _aged(self, tmp_path, seconds):
        import os
        d = _package(tmp_path)
        pdf = d / "resume.pdf"
        md_time = (d / "resume.md").stat().st_mtime
        os.utime(pdf, (md_time - seconds, md_time - seconds))
        return d

    def test_a_pdf_written_seconds_before_its_source_is_not_stale(self, tmp_path):
        r = check_application(self._aged(tmp_path, 152), IDENTITY)
        assert r.status is QCStatus.PASS, [f.code for f in r.findings]

    def test_a_pdf_a_day_older_than_its_source_is_stale(self, tmp_path):
        r = check_application(self._aged(tmp_path, 73414), IDENTITY)
        assert r.status is QCStatus.REVISE
        assert any(f.code is QCFinding.STALE_RENDERING for f in r.findings)

    def test_the_threshold_is_explicit_and_inside_the_observed_gap(self):
        from jobflow_quality.qc import RENDER_SLACK_SECONDS

        assert 152 < RENDER_SLACK_SECONDS < 73414
