"""The gate the applier consults immediately before submitting.

`material_files()` in the ready sweep checks only that files EXIST. That is how
four packages carrying a fabricated surname reached `ready`, and how one
addressed to `[Company Name]` entered `applying`. Existence is not fitness.

This returns a bounded skip reason or None, shaped to drop straight into the
sweep's existing `submitSkipped` list alongside `missing_materials_resume_pdf`.

It fails CLOSED. A package that cannot be checked is not submitted, because the
errors are not symmetric: skipping delays an application by one cron tick,
while submitting an unchecked document cannot be recalled.
"""

from __future__ import annotations

import json

import pytest

from jobflow_quality.qc import CandidateIdentity
from jobflow_quality.readiness import submission_block_reason

IDENTITY = CandidateIdentity(
    full_name="Diego De Aragao", email="diegodearagao@gmail.com"
)

RESUME = """# Diego De Aragao, CFA
> Contact: diegodearagao@gmail.com
Executive summary.
"""
COVER = """Diego De Aragao
diegodearagao@gmail.com

Hiring Manager
Acme Bank

Interested in the role.
"""


def _pkg(tmp_path, resume=RESUME, cover=COVER):
    d = tmp_path / "pkg"
    d.mkdir(parents=True, exist_ok=True)
    (d / "resume.md").write_text(resume, encoding="utf-8")
    (d / "cover-letter.md").write_text(cover, encoding="utf-8")
    (d / "resume.pdf").write_bytes(b"%PDF-1.4 r")
    (d / "cover-letter.pdf").write_bytes(b"%PDF-1.4 c")
    (d / "metadata.json").write_text(json.dumps({"company": "Acme Bank"}), encoding="utf-8")
    return d


class TestCleanPackagesSubmit:
    def test_a_clean_package_has_no_block_reason(self, tmp_path):
        assert submission_block_reason(_pkg(tmp_path), IDENTITY) is None


class TestDefectsBlockSubmission:
    def test_a_fabricated_name_blocks(self, tmp_path):
        d = _pkg(tmp_path, resume=RESUME.replace("Diego De Aragao", "Diego Rodrigues"))
        reason = submission_block_reason(d, IDENTITY)
        assert reason is not None
        assert "identity_mismatch" in reason

    def test_an_unfilled_placeholder_blocks(self, tmp_path):
        d = _pkg(tmp_path, cover=COVER.replace("Acme Bank", "[Company Name]"))
        reason = submission_block_reason(d, IDENTITY)
        assert reason is not None
        assert "unfilled_placeholder" in reason

    def test_the_reason_distinguishes_blocked_from_revise(self, tmp_path):
        blocked = submission_block_reason(
            _pkg(tmp_path, resume=RESUME.replace("Diego De Aragao", "Diego Rodrigues")),
            IDENTITY)
        revise = submission_block_reason(
            _pkg(tmp_path / "b", cover=COVER.replace("Acme Bank", "[Company Name]")),
            IDENTITY)
        assert blocked.startswith("qc_blocked:")
        assert revise.startswith("qc_revise:")


class TestFailsClosed:
    def test_a_missing_directory_blocks(self, tmp_path):
        assert submission_block_reason(tmp_path / "nope", IDENTITY) is not None

    def test_an_unreadable_package_blocks_rather_than_passing(self, tmp_path, monkeypatch):
        """If QC itself raises, nothing is submitted."""
        import jobflow_quality.readiness as mod

        def _boom(*a, **k):
            raise RuntimeError("qc exploded")

        monkeypatch.setattr(mod, "check_application", _boom)
        reason = submission_block_reason(_pkg(tmp_path), IDENTITY)
        assert reason is not None
        assert "qc_error" in reason

    def test_a_missing_identity_blocks(self, tmp_path):
        assert submission_block_reason(_pkg(tmp_path), None) is not None


class TestReasonShape:
    def test_the_reason_is_short_enough_for_a_summary_row(self, tmp_path):
        d = _pkg(tmp_path, resume=RESUME.replace("Diego De Aragao", "Diego Rodrigues"))
        assert len(submission_block_reason(d, IDENTITY)) <= 80

    def test_the_reason_carries_no_document_text(self, tmp_path):
        secret = "Diego Rodrigues confidential 450000 package"
        d = _pkg(tmp_path, resume=RESUME.replace("Diego De Aragao", secret))
        assert "450000" not in submission_block_reason(d, IDENTITY)

    def test_multiple_defects_report_the_blocking_one_first(self, tmp_path):
        d = _pkg(tmp_path,
                 resume=RESUME.replace("Diego De Aragao", "Diego Rodrigues"),
                 cover=COVER.replace("Acme Bank", "[Company Name]"))
        assert submission_block_reason(d, IDENTITY).startswith("qc_blocked:")


class TestIdentityLoading:
    def test_identity_is_read_from_the_master_resume(self, tmp_path):
        from jobflow_quality.readiness import load_default_identity

        master = tmp_path / "master-resume.md"
        master.write_text(
            "# Master Resume — Diego De Aragao, CFA\n"
            "> **Contact:** diegodearagao@gmail.com • +1 (929) 381-8907\n",
            encoding="utf-8")
        ident = load_default_identity(master)
        assert ident.full_name == "Diego De Aragao"
        assert ident.email == "diegodearagao@gmail.com"

    def test_a_missing_master_resume_returns_none_rather_than_guessing(self, tmp_path):
        from jobflow_quality.readiness import load_default_identity

        assert load_default_identity(tmp_path / "absent.md") is None
