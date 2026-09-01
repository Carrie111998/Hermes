"""Deterministic exclusions ahead of the premium seven-dimension ranker.

The matcher is the fleet's most expensive activity. Every job it scores costs a
premium model call, and a share of them are disqualified on facts already
printed in the posting. Deciding those without a model is the cheapest saving
available.

The asymmetry drives every design choice here: a job wrongly kept costs one
model call, a job wrongly excluded costs Diego a real opportunity and does so
silently. So this filter only ever excludes on evidence PRESENT in the posting.
Absent data is never grounds for exclusion, and no judgement call — domain fit,
seniority nuance, location desirability — belongs here. Those are what the
ranker is for.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from jobflow_quality import matcher_filter
from jobflow_quality.matcher_filter import (
    DEFAULT_CRITERIA,
    Criteria,
    ExclusionReason,
    hard_filter,
)

# DELIBERATELY NOT the operator's real bail line. The real figure lives in
# matcher-criteria.json outside the repository, so DEFAULT_CRITERIA's floor is
# None on any machine without that file -- asserting comp behaviour against the
# ambient default would make this suite pass only on the operator's box. Every
# salary fixture below keeps its meaning at this value.
_TEST_FLOOR_USD = 175_000
TEST_CRITERIA = replace(DEFAULT_CRITERIA, compensation_floor_usd=_TEST_FLOOR_USD)


def _job(**over):
    base = {
        "id": "j1",
        "title": "VP of Data Engineering",
        "company": "Acme Bank",
        "location": "Remote",
        "description": "Lead the data platform for a fintech. 10+ years experience.",
        "salary_range": None,
    }
    base.update(over)
    return base


class TestEligibleByDefault:
    def test_a_normal_posting_reaches_the_ranker(self):
        d = hard_filter(_job(), TEST_CRITERIA)
        assert d.eligible is True
        assert d.reasons == ()

    def test_an_empty_posting_is_eligible_not_excluded(self):
        """Missing evidence is not evidence. A thin JD is a data problem."""
        d = hard_filter(_job(description="", title="", company="", location=""),
                        TEST_CRITERIA)
        assert d.eligible is True

    def test_a_job_with_no_recognisable_fields_is_eligible(self):
        assert hard_filter({}, TEST_CRITERIA).eligible is True


_CREDENTIALS = Criteria(hard_requirement_phrases=("security clearance", "series 7"))


class TestUnmetHardRequirements:
    """The mechanism works; it is the DEFAULT phrase list that is empty."""

    @pytest.mark.parametrize("phrase", (
        "Must hold an active security clearance",
        "Series 7 license required",
    ))
    def test_stated_hard_requirements_exclude(self, phrase):
        d = hard_filter(_job(description=phrase), _CREDENTIALS)
        assert d.eligible is False
        assert ExclusionReason.UNMET_HARD_REQUIREMENT in d.reasons

    def test_matching_is_case_insensitive(self):
        d = hard_filter(_job(description="SECURITY CLEARANCE REQUIRED"), _CREDENTIALS)
        assert d.eligible is False

    def test_a_substring_inside_a_longer_word_does_not_match(self):
        """`series 700` is not `series 7`; a naive `in` check would exclude it."""
        d = hard_filter(_job(description="Ships series 700 hardware"), _CREDENTIALS)
        assert d.eligible is True

    def test_a_requirement_named_only_in_the_title_still_counts(self):
        d = hard_filter(_job(title="VP Engineering (Security Clearance Required)",
                             description=""), _CREDENTIALS)
        assert d.eligible is False


class TestCredentialPhrasesAreNotShippedByDefault:
    """Measured 1/10 precision against 925 real postings — see the module note.

    Nine of ten matches were obtainability language ("or apply upon arrival
    for", "or ability to obtain", "may be required to obtain", "clearance
    eligibility"), archiving target-profile roles at Citi, Fifth Third and
    Draper. Encoded as tests so re-adding the phrases is a deliberate act that
    has to defeat this evidence, not a tidy-up.
    """

    def test_the_default_ships_no_credential_phrases(self):
        assert DEFAULT_CRITERIA.hard_requirement_phrases == ()

    @pytest.mark.parametrize("posting", (
        "already possess or apply upon arrival for Series 7 and 63 licenses",
        "Series 7 and Series 63 licenses (or ability to obtain within company policy)",
        "may be required to obtain and maintain a government security clearance",
        "Security clearance eligibility. U.S. citizenship with the ability to obtain",
        "ability to obtain a U.S. Government security clearance, if required preferred",
    ))
    def test_obtainability_language_reaches_the_ranker(self, posting):
        assert hard_filter(_job(description=posting), TEST_CRITERIA).eligible is True

    def test_a_genuine_requirement_also_reaches_the_ranker_by_default(self):
        """The cost of the above: the one true positive goes through too.

        One wasted model call is the correct price for not archiving nine real
        opportunities. Stated plainly so the trade is visible, not discovered.
        """
        genuine = "FINRA Series 7, 24, and 65 licenses required"
        assert hard_filter(_job(description=genuine), TEST_CRITERIA).eligible is True
        assert hard_filter(_job(description=genuine), _CREDENTIALS).eligible is False


class TestCompensationFloor:
    """Only a stated MAXIMUM below the floor is disqualifying.

    A range is an invitation to negotiate at its top. Excluding on the minimum
    would archive every wide band, which is most senior postings.
    """

    def test_a_ceiling_below_the_floor_excludes(self):
        d = hard_filter(_job(salary_range="$90,000 - $120,000"), TEST_CRITERIA)
        assert d.eligible is False
        assert ExclusionReason.BELOW_COMPENSATION_FLOOR in d.reasons

    def test_a_range_straddling_the_floor_is_eligible(self):
        d = hard_filter(_job(salary_range="$150,000 - $250,000"), TEST_CRITERIA)
        assert d.eligible is True

    def test_an_undisclosed_salary_is_eligible(self):
        assert hard_filter(_job(salary_range=None), TEST_CRITERIA).eligible is True
        assert hard_filter(_job(salary_range="Competitive"), TEST_CRITERIA).eligible is True

    def test_a_small_bare_figure_is_not_read_as_an_annual_salary(self):
        """`$85 - $95` is a unit the parser cannot identify, not a $95 salary.

        Guarded by the magnitude check rather than the hourly rule — this input
        never reaches the hourly rule, which is why the case below exists.
        """
        d = hard_filter(_job(salary_range="$85 - $95 per hour"), TEST_CRITERIA)
        assert d.eligible is True

    def test_an_hourly_rate_above_the_magnitude_floor_is_still_not_annual(self):
        """Isolates the hourly rule: $1,200/hr is ~$2.5M a year, not $1,200.

        The magnitude check cannot save this one — 1200 reads as a plausible
        annual figure and would exclude a job paying an order of magnitude
        above the floor. Without this case the hourly rule is dead code that
        no test would notice losing.
        """
        d = hard_filter(_job(salary_range="$1,200 per hour"), TEST_CRITERIA)
        assert d.eligible is True

    def test_an_hourly_rate_written_with_a_slash_is_recognised(self):
        d = hard_filter(_job(salary_range="$1,500/hr"), TEST_CRITERIA)
        assert d.eligible is True

    def test_a_k_suffix_is_understood(self):
        assert hard_filter(_job(salary_range="$90k-$120k"), TEST_CRITERIA).eligible is False
        assert hard_filter(_job(salary_range="$200k-$260k"), TEST_CRITERIA).eligible is True

    def test_an_unparseable_salary_string_is_eligible(self):
        d = hard_filter(_job(salary_range="DOE, plus equity"), TEST_CRITERIA)
        assert d.eligible is True


class TestStructuredSalary:
    """Scout writes salary as a dict, not a string.

    Real shape, from mailbox/tracker/processed/*SCOUT_DISCOVERY*.json:
    ``{'min': 165000.0, 'max': 205000.0, 'currency': 'USD',
    'period': 'annual', 'source': 'posted'}``. Handling only strings left the
    compensation rule dead against every real posting while the string tests
    stayed green.
    """

    def test_a_structured_ceiling_below_the_floor_excludes(self):
        d = hard_filter(_job(salary_range={
            "min": 90000.0, "max": 120000.0, "currency": "USD", "period": "annual",
        }), TEST_CRITERIA)
        assert d.eligible is False
        assert ExclusionReason.BELOW_COMPENSATION_FLOOR in d.reasons

    def test_a_structured_range_straddling_the_floor_is_eligible(self):
        d = hard_filter(_job(salary_range={
            "min": 165000.0, "max": 205000.0, "currency": "USD", "period": "annual",
        }), TEST_CRITERIA)
        assert d.eligible is True

    def test_an_hourly_period_is_never_compared_to_an_annual_floor(self):
        d = hard_filter(_job(salary_range={
            "min": 80.0, "max": 95.0, "currency": "USD", "period": "hourly",
        }), TEST_CRITERIA)
        assert d.eligible is True

    def test_a_non_usd_currency_is_not_compared(self):
        """A foreign-currency figure is not commensurable without a rate."""
        d = hard_filter(_job(salary_range={
            "min": 90000.0, "max": 120000.0, "currency": "EUR", "period": "annual",
        }), TEST_CRITERIA)
        assert d.eligible is True

    def test_a_missing_max_is_eligible(self):
        """Excluding on `min` alone would archive every open-topped band."""
        d = hard_filter(_job(salary_range={
            "min": 90000.0, "max": None, "currency": "USD", "period": "annual",
        }), TEST_CRITERIA)
        assert d.eligible is True

    def test_an_empty_dict_is_eligible(self):
        assert hard_filter(_job(salary_range={}), TEST_CRITERIA).eligible is True


class TestExcludedCompanies:
    def test_a_blocklisted_company_excludes(self):
        d = hard_filter(_job(company="DataAnnotation"), TEST_CRITERIA)
        assert d.eligible is False
        assert ExclusionReason.EXCLUDED_COMPANY in d.reasons

    def test_company_matching_is_case_insensitive(self):
        assert hard_filter(_job(company="dataannotation"), TEST_CRITERIA).eligible is False

    def test_a_company_merely_containing_the_name_is_not_excluded(self):
        """Substring matching on company names archives innocent employers."""
        d = hard_filter(_job(company="DataAnnotation Partners Group"), TEST_CRITERIA)
        assert d.eligible is True


class TestVipIsNeverExcluded:
    """SOUL.md: VIP jobs are never auto-archived regardless of score.

    A filter that ignored this would silently overrule a standing exemption.
    """

    @pytest.mark.parametrize("job", (
        _job(salary_range="$50,000 - $60,000"),
        _job(company="DataAnnotation"),
    ))
    def test_vip_survives_every_exclusion(self, job):
        job = dict(job, vip={"is_vip": True})
        d = hard_filter(job, TEST_CRITERIA)
        assert d.eligible is True
        assert d.vip_exempt is True

    def test_vip_survives_a_credential_exclusion_too(self):
        job = _job(description="Must hold an active security clearance",
                   vip={"is_vip": True})
        d = hard_filter(job, _CREDENTIALS)
        assert d.eligible is True
        assert d.vip_exempt is True

    def test_the_suppressed_reasons_are_still_reported(self):
        """Exempt must not mean invisible — the operator still needs the why."""
        job = _job(company="DataAnnotation", vip={"is_vip": True})
        d = hard_filter(job, TEST_CRITERIA)
        assert d.eligible is True
        assert ExclusionReason.EXCLUDED_COMPANY in d.suppressed_reasons

    def test_a_non_vip_flag_does_not_exempt(self):
        job = _job(company="DataAnnotation", vip={"is_vip": False})
        assert hard_filter(job, TEST_CRITERIA).eligible is False


class TestDecisionContract:
    def test_reasons_are_bounded_codes_never_free_text(self):
        d = hard_filter(_job(description="security clearance required"), TEST_CRITERIA)
        assert all(isinstance(r, ExclusionReason) for r in d.reasons)

    def test_the_decision_is_immutable(self):
        d = hard_filter(_job(), TEST_CRITERIA)
        with pytest.raises(AttributeError):
            d.eligible = False

    def test_multiple_independent_reasons_are_all_reported(self):
        criteria = Criteria(
            hard_requirement_phrases=("security clearance",),
            compensation_floor_usd=DEFAULT_CRITERIA.compensation_floor_usd,
            excluded_companies=DEFAULT_CRITERIA.excluded_companies,
        )
        d = hard_filter(
            _job(company="DataAnnotation", salary_range="$40k-$60k",
                 description="Must hold an active security clearance"),
            criteria,
        )
        assert len(set(d.reasons)) == 3

    def test_an_empty_criteria_set_excludes_nothing(self):
        """The filter is entirely criteria-driven; it hardcodes no policy."""
        empty = Criteria()
        d = hard_filter(
            _job(company="DataAnnotation", salary_range="$40k",
                 description="Top Secret clearance required"),
            empty,
        )
        assert d.eligible is True


class TestPurity:
    def test_the_filter_performs_no_io(self, monkeypatch):
        """No model call, no network, no disk — or it cannot be the cheap path."""
        import builtins
        import socket

        def _no(*a, **k):
            raise AssertionError("hard_filter must not perform I/O")

        monkeypatch.setattr(builtins, "open", _no)
        monkeypatch.setattr(socket, "socket", _no)
        hard_filter(_job(description="security clearance required"), TEST_CRITERIA)

    def test_the_job_mapping_is_not_mutated(self):
        job = _job()
        before = dict(job)
        hard_filter(job, TEST_CRITERIA)
        assert job == before


class TestDefaultCriteriaProvenance:
    def test_the_floor_comes_from_local_state_not_from_source(self, tmp_path, monkeypatch):
        """The bail line is config, not a literal -- assert the WIRING, not the value.

        This used to assert a specific figure, which both republished the most
        negotiation-sensitive number in the system and tied the suite to the
        operator's machine. Which number is correct stays a policy decision:
        the repo has stated two that genuinely disagree, and the config should
        carry the LOWER one, because it excludes strictly fewer jobs and
        over-exclusion is the expensive error.
        """
        path = tmp_path / "matcher-criteria.json"
        path.write_text('{"compensation_floor_usd": 111222}', encoding="utf-8")
        monkeypatch.setattr(matcher_filter, "_CRITERIA_PATH", path)
        assert matcher_filter._load_compensation_floor_usd() == 111222

    def test_an_absent_config_disables_the_floor_rather_than_defaulting(
        self, tmp_path, monkeypatch
    ):
        """Failing open is the documented direction: exclude nothing on pay."""
        monkeypatch.setattr(matcher_filter, "_CRITERIA_PATH", tmp_path / "absent.json")
        assert matcher_filter._load_compensation_floor_usd() is None

    def test_no_authorization_criterion_is_asserted(self):
        """No visa/sponsorship signal exists in any job record or criteria file.

        An authorization filter would have to invent both the rule and the data,
        so there is deliberately none. Encoded as a test so its absence reads as
        a decision rather than an omission.
        """
        assert not hasattr(DEFAULT_CRITERIA, "work_authorization")


class TestTheMatcherSeamIsDeliberatelyUnwired:
    """Measured decision, like the tracker route — keep it from being "fixed".

    The filter excludes 3.5% of raw Scout discoveries, which is what made it
    look worth wiring ahead of the matcher. It is not: measured against 432
    real SCORE_REQUESTs joined to their pipeline records, only **4 (0.9%)**
    are filterable, because a job reaching the matcher has already passed
    Scout's own exclusions. 41% of those requests carry no salary at all.

    0.9% of matcher calls is roughly $0.03/week, against a live change to the
    job pipeline that would auto-archive without a model ever looking. The
    filter earns its place where it already runs — the pre-submission gate and
    as a non-circular label source for the golden set — not here.

    See docs/superpowers/plans/2026-08-10-jobflow-premium-quality-routing.md.
    """

    def test_the_filter_is_not_imported_by_any_matcher_runtime_path(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[2]
        wired = [
            p for p in (root / "graphs").rglob("*.py")
            if "matcher_filter" in p.read_text(encoding="utf-8", errors="replace")
        ]
        assert wired == [], f"matcher scoring path now imports the filter: {wired}"
