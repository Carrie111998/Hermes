"""The premium review that runs only after the deterministic gate passes.

The deterministic half catches what a fact check can settle — a fabricated
surname, an unfilled `[Company Name]`, a PDF older than its source. What it
cannot catch is a resume bullet asserting something the master resume never
says. That is the claim most likely to reach an employer and the hardest to
walk back, and it is the reason this pass exists.

Everything here except the model call itself is pure: prompt construction and
response parsing are unit-tested, and the call is an injected seam. That keeps
the expensive part small and the testable part honest.

The governing rule matches the rest of the workstream: **a review that did not
happen is `unknown`, never `pass`.** A model timeout, a malformed response or a
refusal must never read as "no problems found" — that is precisely how an
unreviewed document would acquire an approval.
"""

from __future__ import annotations

import pytest

from jobflow_quality.semantic_qc import (
    SemanticFinding,
    SemanticStatus,
    build_qc_prompt,
    parse_qc_response,
    review,
)

MASTER = "# Diego De Aragao\nVP Data at Acme Bank 2019-2024. CFA charterholder."
JD = "Director of Analytics. Requires 10 years in financial services."
RESUME = "# Diego De Aragao\nVP Data at Acme Bank. Led a team of 12."
COVER = "Dear Hiring Manager,\nI led analytics at Acme Bank."


class TestPromptConstruction:
    def test_the_prompt_carries_all_four_sources(self):
        prompt = build_qc_prompt(master_resume=MASTER, job_description=JD,
                                 resume=RESUME, cover_letter=COVER)
        for fragment in ("CFA charterholder", "Director of Analytics",
                         "team of 12", "led analytics at Acme Bank"):
            assert fragment in prompt

    def test_the_master_resume_is_named_as_the_only_source_of_candidate_facts(self):
        """The plan's global constraint, stated in the prompt rather than hoped for."""
        prompt = build_qc_prompt(master_resume=MASTER, job_description=JD,
                                 resume=RESUME, cover_letter=COVER)
        assert "master resume" in prompt.lower()
        assert "only" in prompt.lower()

    def test_oversized_inputs_are_bounded(self):
        """A 200k-char JD must not become a 200k-char prompt."""
        prompt = build_qc_prompt(master_resume=MASTER, job_description="x" * 200_000,
                                 resume=RESUME, cover_letter=COVER)
        assert len(prompt) < 80_000

    def test_truncation_is_marked_not_silent(self):
        prompt = build_qc_prompt(master_resume=MASTER, job_description="x" * 200_000,
                                 resume=RESUME, cover_letter=COVER)
        assert "truncated" in prompt.lower()

    def test_a_missing_source_is_labelled_rather_than_omitted(self):
        """A blank section must read as absent, not as 'nothing to flag'.

        Assert on the SECTION, not the whole prompt: the instructions contain
        "missing_required_content", so a substring check over the prompt passed
        no matter what the section builder did.
        """
        prompt = build_qc_prompt(master_resume=MASTER, job_description="",
                                 resume=RESUME, cover_letter=COVER)
        section = prompt.split("## JOB DESCRIPTION", 1)[1].split("##", 1)[0]
        assert "not available" in section.lower(), section


class TestResponseParsing:
    def test_a_clean_verdict_parses_to_pass(self):
        result = parse_qc_response('{"findings": []}')
        assert result.status is SemanticStatus.PASS
        assert result.findings == ()

    def test_findings_parse_with_bounded_categories(self):
        raw = ('{"findings": [{"category": "unsupported_claim", '
               '"artifact": "resume.md", "detail": "team of 12 is not in the master resume"}]}')
        result = parse_qc_response(raw)
        assert result.status is SemanticStatus.FINDINGS
        assert result.findings[0].category == "unsupported_claim"
        assert result.findings[0].artifact == "resume.md"

    def test_an_unknown_category_is_kept_but_normalised(self):
        raw = '{"findings": [{"category": "WEIRD Thing", "artifact": "resume.md", "detail": "x"}]}'
        result = parse_qc_response(raw)
        assert result.findings[0].category == "other"

    def test_json_wrapped_in_prose_or_fences_still_parses(self):
        raw = 'Here you go:\n```json\n{"findings": []}\n```\nHope that helps.'
        assert parse_qc_response(raw).status is SemanticStatus.PASS


class TestAReviewThatDidNotHappenIsUnknown:
    """The invariant. Never let silence read as approval."""

    @pytest.mark.parametrize("raw", (
        "", "   ", None,
        "I cannot help with that.",   # no JSON at all
        "{not json",                   # opening brace, no closing — no span found
        '{"findings": [},}',           # braces present, contents malformed -> json.loads raises
        '{"wrong": "shape"}',          # parses, but no findings key
        '{"findings": "not a list"}',  # findings present, wrong type
        "[]",                          # a list where an object was required
    ))
    def test_unusable_responses_are_unknown_not_pass(self, raw):
        result = parse_qc_response(raw)
        assert result.status is SemanticStatus.UNKNOWN
        assert result.status is not SemanticStatus.PASS

    def test_a_model_failure_is_unknown(self):
        def _boom(prompt):
            raise RuntimeError("provider unavailable")

        result = review(master_resume=MASTER, job_description=JD, resume=RESUME,
                        cover_letter=COVER, invoke=_boom)
        assert result.status is SemanticStatus.UNKNOWN

    def test_the_failure_reason_carries_no_document_text(self):
        secret = "confidential comp band 450000"

        def _boom(prompt):
            raise RuntimeError(f"provider said: {secret}")

        result = review(master_resume=MASTER + secret, job_description=JD,
                        resume=RESUME, cover_letter=COVER, invoke=_boom)
        assert secret not in repr(result)


class TestReview:
    def test_a_clean_review_passes(self):
        result = review(master_resume=MASTER, job_description=JD, resume=RESUME,
                        cover_letter=COVER, invoke=lambda p: '{"findings": []}')
        assert result.status is SemanticStatus.PASS

    def test_findings_are_returned(self):
        raw = ('{"findings": [{"category": "unsupported_claim", '
               '"artifact": "resume.md", "detail": "no evidence of a team of 12"}]}')
        result = review(master_resume=MASTER, job_description=JD, resume=RESUME,
                        cover_letter=COVER, invoke=lambda p: raw)
        assert len(result.findings) == 1

    def test_the_prompt_actually_reaches_the_model(self):
        seen = {}

        def _capture(prompt):
            seen["prompt"] = prompt
            return '{"findings": []}'

        review(master_resume=MASTER, job_description=JD, resume=RESUME,
               cover_letter=COVER, invoke=_capture)
        assert "CFA charterholder" in seen["prompt"]

    def test_the_result_is_immutable(self):
        result = review(master_resume=MASTER, job_description=JD, resume=RESUME,
                        cover_letter=COVER, invoke=lambda p: '{"findings": []}')
        with pytest.raises(AttributeError):
            result.status = SemanticStatus.PASS


class TestFindingContract:
    def test_a_finding_is_frozen(self):
        f = SemanticFinding(category="unsupported_claim", artifact="resume.md", detail="x")
        with pytest.raises(AttributeError):
            f.category = "other"

    def test_detail_is_bounded(self):
        raw = ('{"findings": [{"category": "unsupported_claim", "artifact": "resume.md", '
               '"detail": "' + "y" * 5000 + '"}]}')
        result = parse_qc_response(raw)
        assert len(result.findings[0].detail) <= 500
