"""Assemble a labelled evaluation set from evidence that did not come from the scorer.

The premium-routing plan asks for a golden set before any route may change.
The obvious source is the pipeline's own history — but most of it is the
scorer's own output fed back as truth. 4,670 rows are archived, and they are
archived *because the scorer scored them low*. Grading the scorer against those
measures nothing: it will agree with itself perfectly and prove nothing about
quality.

So the only admissible labels are ones the scorer did not produce:

* a human acted on the job (``actor_id: diego`` through the intent applier), or
* a fact printed in the posting decides it (a stated ceiling under the bail
  line), which is true whatever any model says.

Auto-route approvals look identical to human ones at a glance — same stage,
same shape — and are the trap this module exists to avoid.
"""

from __future__ import annotations

import pytest

from jobflow_quality.golden_set import (
    Difficulty,
    GoldenItem,
    Label,
    LabelSource,
    build_golden_set,
)
from jobflow_quality.matcher_filter import DEFAULT_CRITERIA


def _row(**over):
    base = {
        "title": "VP Data Engineering",
        "company": "Acme Bank",
        "description_raw": "Lead the data platform.",
        "score": 7.8,
        "stage": "approved",
        "history": [],
    }
    base.update(over)
    return base


def _human_event():
    return {"from_stage": "scored", "to_stage": "approved",
            "metadata": {"actor_id": "diego", "emitted_by": "tracker-intent-applier"}}


def _auto_event():
    return {"from_stage": "scored", "to_stage": "approved",
            "metadata": {"approved_by": "main-auto-route",
                         "reason": "Auto-approved under JobFlow score routing (score >= 8.0)"}}


class TestHumanApprovalsBecomeLabels:
    def test_a_diego_actor_event_yields_an_advance_label(self):
        items = build_golden_set({"j1": _row(history=[_human_event()])}, DEFAULT_CRITERIA)
        assert len(items) == 1
        assert items[0].label is Label.ADVANCE
        assert items[0].source is LabelSource.HUMAN_APPROVAL

    def test_human_approvals_are_marked_nuanced(self):
        """They are the hard cases — a scorer must not be graded only on easy ones."""
        items = build_golden_set({"j1": _row(history=[_human_event()])}, DEFAULT_CRITERIA)
        assert items[0].difficulty is Difficulty.NUANCED

    def test_a_later_archive_does_not_revoke_the_approval(self):
        """Diego approved it; the employer going quiet is not a scoring error.

        11 of the 20 human-approved rows are archived today. Reading that as
        "the label was wrong" would invert most of the positive evidence.
        """
        row = _row(stage="archived", history=[_human_event()])
        items = build_golden_set({"j1": row}, DEFAULT_CRITERIA)
        assert len(items) == 1
        assert items[0].label is Label.ADVANCE


class TestCircularLabelsAreRefused:
    """The whole point. An auto-approval is the scorer's own verdict."""

    def test_an_auto_route_approval_is_not_a_label(self):
        items = build_golden_set({"j1": _row(history=[_auto_event()])}, DEFAULT_CRITERIA)
        assert items == ()

    def test_stage_approved_alone_is_not_a_label(self):
        """26 of 32 approved rows are VIP auto-approvals, not scoring judgements."""
        items = build_golden_set({"j1": _row(stage="approved", vip=True, history=[])},
                                 DEFAULT_CRITERIA)
        assert items == ()

    def test_stage_archived_alone_is_not_a_label(self):
        """4,670 archived rows were archived BY the scorer. Circular."""
        items = build_golden_set({"j1": _row(stage="archived", history=[])},
                                 DEFAULT_CRITERIA)
        assert items == ()

    def test_a_human_event_alongside_an_auto_event_still_counts(self):
        row = _row(history=[_auto_event(), _human_event()])
        assert len(build_golden_set({"j1": row}, DEFAULT_CRITERIA)) == 1


class TestDeterministicExclusions:
    def test_a_posting_under_the_bail_line_yields_an_exclude_label(self):
        row = _row(salary_range={"min": 90000.0, "max": 120000.0,
                                 "currency": "USD", "period": "annual"})
        items = build_golden_set({"j1": row}, DEFAULT_CRITERIA)
        assert len(items) == 1
        assert items[0].label is Label.EXCLUDE
        assert items[0].source is LabelSource.DETERMINISTIC_EXCLUSION

    def test_deterministic_exclusions_are_marked_obvious(self):
        """Kept separate so an evaluator cannot be flattered by easy negatives."""
        row = _row(salary_range={"min": 90000.0, "max": 120000.0,
                                 "currency": "USD", "period": "annual"})
        assert build_golden_set({"j1": row}, DEFAULT_CRITERIA)[0].difficulty is Difficulty.OBVIOUS

    def test_an_ordinary_posting_yields_no_label(self):
        assert build_golden_set({"j1": _row()}, DEFAULT_CRITERIA) == ()


class TestConflicts:
    def test_a_human_approval_overrides_a_deterministic_exclusion(self):
        """A person looked at it and said yes. That outranks the rule."""
        row = _row(history=[_human_event()],
                   salary_range={"min": 90000.0, "max": 120000.0,
                                 "currency": "USD", "period": "annual"})
        items = build_golden_set({"j1": row}, DEFAULT_CRITERIA)
        assert len(items) == 1, "one job must never produce two contradictory labels"
        assert items[0].label is Label.ADVANCE
        assert items[0].conflicted is True


class TestSecretFree:
    def test_no_item_carries_raw_posting_text(self):
        """Task 6: store references and hashes, not private raw documents."""
        secret = "CONFIDENTIAL internal comp band do not distribute"
        row = _row(description_raw=secret, history=[_human_event()])
        item = build_golden_set({"j1": row}, DEFAULT_CRITERIA)[0]
        assert secret not in repr(item)
        for value in vars(item).values():
            assert secret not in str(value)

    def test_the_description_is_referenced_by_hash(self):
        row = _row(description_raw="abc", history=[_human_event()])
        item = build_golden_set({"j1": row}, DEFAULT_CRITERIA)[0]
        assert len(item.description_sha256) == 64
        assert item.description_sha256 == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )

    def test_evidence_is_a_bounded_code_not_free_text(self):
        row = _row(history=[_human_event()])
        item = build_golden_set({"j1": row}, DEFAULT_CRITERIA)[0]
        assert item.evidence in {s.value for s in LabelSource} or item.evidence.islower()
        assert len(item.evidence) <= 64


class TestDeterminism:
    def test_the_same_input_yields_the_same_ordering(self):
        jobs = {f"j{i}": _row(history=[_human_event()]) for i in range(8)}
        assert build_golden_set(jobs, DEFAULT_CRITERIA) == build_golden_set(jobs, DEFAULT_CRITERIA)

    def test_ordering_does_not_depend_on_dict_insertion_order(self):
        a = {"j2": _row(history=[_human_event()]), "j1": _row(history=[_human_event()])}
        b = {"j1": _row(history=[_human_event()]), "j2": _row(history=[_human_event()])}
        assert [i.job_id for i in build_golden_set(a, DEFAULT_CRITERIA)] == \
               [i.job_id for i in build_golden_set(b, DEFAULT_CRITERIA)]

    def test_items_are_immutable(self):
        item = build_golden_set({"j1": _row(history=[_human_event()])}, DEFAULT_CRITERIA)[0]
        with pytest.raises(AttributeError):
            item.label = Label.EXCLUDE


class TestMalformedInput:
    @pytest.mark.parametrize("jobs", ({}, {"j1": None}, {"j1": "nonsense"}, {"j1": {}}))
    def test_unusable_input_yields_no_labels_and_never_raises(self, jobs):
        assert build_golden_set(jobs, DEFAULT_CRITERIA) == ()

    def test_a_malformed_history_entry_is_skipped(self):
        row = _row(history=["nonsense", None, _human_event()])
        assert len(build_golden_set({"j1": row}, DEFAULT_CRITERIA)) == 1

    def test_a_job_with_no_id_key_is_skipped(self):
        assert build_golden_set({"": _row(history=[_human_event()])}, DEFAULT_CRITERIA) == ()


class TestSetComposition:
    def test_the_set_reports_its_own_balance(self):
        from jobflow_quality.golden_set import summarize

        jobs = {
            "a": _row(history=[_human_event()]),
            "b": _row(salary_range={"min": 1.0, "max": 90000.0,
                                    "currency": "USD", "period": "annual"}),
            "c": _row(),
        }
        s = summarize(build_golden_set(jobs, DEFAULT_CRITERIA))
        assert s["total"] == 2
        assert s["by_label"] == {"advance": 1, "exclude": 1}
        assert s["by_difficulty"] == {"nuanced": 1, "obvious": 1}

    def test_an_all_obvious_set_is_flagged_as_unbalanced(self):
        """A set of only easy negatives makes any scorer look good."""
        from jobflow_quality.golden_set import summarize

        jobs = {f"j{i}": _row(salary_range={"min": 1.0, "max": 90000.0,
                                            "currency": "USD", "period": "annual"})
                for i in range(5)}
        s = summarize(build_golden_set(jobs, DEFAULT_CRITERIA))
        assert s["balanced"] is False

    def test_a_mixed_set_is_balanced(self):
        from jobflow_quality.golden_set import summarize

        jobs = {f"h{i}": _row(history=[_human_event()]) for i in range(5)}
        jobs.update({f"e{i}": _row(salary_range={"min": 1.0, "max": 90000.0,
                                                 "currency": "USD", "period": "annual"})
                     for i in range(5)})
        assert summarize(build_golden_set(jobs, DEFAULT_CRITERIA))["balanced"] is True


class TestBalancedSampling:
    """109 real items are 82% obvious negatives. Used whole, they hide failure.

    Downsampling the dominant difficulty is what makes the set usable at all —
    but it must be deterministic, or two evaluation runs grade against
    different data and their scores are not comparable.
    """

    def _mixed(self, nuanced: int, obvious: int):
        jobs = {f"h{i:03d}": _row(history=[_human_event()]) for i in range(nuanced)}
        jobs.update({f"e{i:03d}": _row(salary_range={"min": 1.0, "max": 90000.0,
                                                     "currency": "USD", "period": "annual"})
                     for i in range(obvious)})
        return build_golden_set(jobs, DEFAULT_CRITERIA)

    def test_the_dominant_difficulty_is_capped_to_the_scarcer_one(self):
        from jobflow_quality.golden_set import balanced_sample, summarize

        sampled = balanced_sample(self._mixed(20, 89))
        s = summarize(sampled)
        assert s["by_difficulty"] == {"nuanced": 20, "obvious": 20}
        assert s["balanced"] is True

    def test_sampling_is_deterministic(self):
        items = self._mixed(20, 89)
        from jobflow_quality.golden_set import balanced_sample

        assert balanced_sample(items) == balanced_sample(items)

    def test_an_already_balanced_set_is_returned_unchanged(self):
        from jobflow_quality.golden_set import balanced_sample

        items = self._mixed(10, 10)
        assert balanced_sample(items) == items

    def test_sampling_never_invents_items(self):
        from jobflow_quality.golden_set import balanced_sample

        items = self._mixed(20, 89)
        assert set(balanced_sample(items)) <= set(items)

    def test_an_empty_set_samples_to_empty(self):
        from jobflow_quality.golden_set import balanced_sample

        assert balanced_sample(()) == ()

    def test_a_single_difficulty_set_cannot_be_balanced(self):
        """Refuse to pretend: with no nuanced cases there is nothing to sample to."""
        from jobflow_quality.golden_set import balanced_sample, summarize

        items = self._mixed(0, 12)
        assert balanced_sample(items) == ()
        assert summarize(balanced_sample(items))["balanced"] is False
