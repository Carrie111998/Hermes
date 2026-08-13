"""Tests for the non-code deliverable rubric verify-on-stop gate (Gap 3).

Covers the pure decision logic in ``agent.verify_hooks``: rubric selection,
mechanical criteria evaluation, and the continue-nudge builder — including the
default-off guarantee and every early-return guard, so the gate can never trap
the loop or change default behavior.
"""

from __future__ import annotations

from agent.verify_hooks import (
    active_verify_rubric,
    build_rubric_verify_nudge,
    unmet_rubric_criteria,
)

WORKPAPER_RUBRIC = {
    "max_nudges": 2,
    "criteria": [
        {"name": "privilege-header", "any_of": ["PRIVILEGED AND CONFIDENTIAL"],
         "hint": "Start with the privilege header."},
        {"name": "sources", "any_of": ["SOURCE:", "Source:"],
         "hint": "Every figure needs a SOURCE: line."},
        {"name": "conclusion", "any_of": ["CONCLUSION"],
         "hint": "End with a CONCLUSION section."},
    ],
}


def _cfg(name="workpaper", rubrics=None):
    return {"agent": {
        "verify_rubric": name,
        "verify_rubrics": rubrics if rubrics is not None else {"workpaper": WORKPAPER_RUBRIC},
    }}


# ── active_verify_rubric ────────────────────────────────────────────────────

def test_disabled_by_default():
    # Empty name → no active rubric → gate inert (default agent behavior).
    assert active_verify_rubric({"agent": {"verify_rubric": ""}}) is None
    assert active_verify_rubric({}) is None
    assert active_verify_rubric({"agent": {}}) is None


def test_active_rubric_selected_by_name():
    assert active_verify_rubric(_cfg()) is WORKPAPER_RUBRIC


def test_unknown_rubric_name_is_none():
    assert active_verify_rubric(_cfg(name="nonesuch")) is None


# ── unmet_rubric_criteria ───────────────────────────────────────────────────

def test_all_met_returns_empty():
    text = "PRIVILEGED AND CONFIDENTIAL\nFigure 1 SOURCE: bank stmt\nCONCLUSION: ok"
    assert unmet_rubric_criteria(text, WORKPAPER_RUBRIC) == []


def test_missing_criteria_reported():
    text = "Some analysis with SOURCE: x but no header and no wrap-up."
    unmet = {c["name"] for c in unmet_rubric_criteria(text, WORKPAPER_RUBRIC)}
    assert unmet == {"privilege-header", "conclusion"}


def test_case_insensitive_default():
    # lower-case 'conclusion' still satisfies the 'CONCLUSION' marker by default.
    text = "PRIVILEGED AND CONFIDENTIAL\nsource: x\nconclusion here"
    assert unmet_rubric_criteria(text, WORKPAPER_RUBRIC) == []


def test_case_sensitive_when_configured():
    rubric = {"case_insensitive": False,
              "criteria": [{"name": "c", "any_of": ["CONCLUSION"]}]}
    assert {c["name"] for c in unmet_rubric_criteria("conclusion", rubric)} == {"c"}
    assert unmet_rubric_criteria("CONCLUSION", rubric) == []


def test_regex_marker():
    rubric = {"criteria": [{"name": "figref", "any_of": [r"fig(ure)?\s*\d+"], "regex": True}]}
    assert unmet_rubric_criteria("see Figure 3", rubric) == []
    assert {c["name"] for c in unmet_rubric_criteria("no refs here", rubric)} == {"figref"}


def test_markerless_criterion_skipped():
    # A criterion with no usable markers can't be evaluated → never "unmet".
    rubric = {"criteria": [{"name": "empty", "any_of": []},
                           {"name": "bad", "any_of": [""]}]}
    assert unmet_rubric_criteria("anything", rubric) == []


def test_bad_regex_is_skipped_not_fatal():
    rubric = {"criteria": [{"name": "broken", "any_of": ["(unclosed"], "regex": True}]}
    # Invalid pattern is a config bug, not a deliverable failure — skipped.
    assert unmet_rubric_criteria("text", rubric) == []


# ── build_rubric_verify_nudge (the gate decision) ───────────────────────────

INCOMPLETE = "Analysis with SOURCE: x but nothing else."
COMPLETE = "PRIVILEGED AND CONFIDENTIAL\nSOURCE: bank\nCONCLUSION: fine"


def test_nudge_none_when_no_active_rubric():
    assert build_rubric_verify_nudge(
        final_response=INCOMPLETE, config={"agent": {"verify_rubric": ""}}) is None


def test_nudge_none_on_coding_turn():
    assert build_rubric_verify_nudge(
        final_response=INCOMPLETE, rubric=WORKPAPER_RUBRIC, coding=True) is None


def test_nudge_none_when_files_changed():
    assert build_rubric_verify_nudge(
        final_response=INCOMPLETE, rubric=WORKPAPER_RUBRIC,
        changed_paths=["a.py"]) is None


def test_nudge_none_on_empty_response():
    assert build_rubric_verify_nudge(
        final_response="   ", rubric=WORKPAPER_RUBRIC) is None
    assert build_rubric_verify_nudge(
        final_response=None, rubric=WORKPAPER_RUBRIC) is None


def test_nudge_none_when_all_met():
    assert build_rubric_verify_nudge(
        final_response=COMPLETE, rubric=WORKPAPER_RUBRIC) is None


def test_nudge_none_when_attempt_cap_reached():
    assert build_rubric_verify_nudge(
        final_response=INCOMPLETE, rubric=WORKPAPER_RUBRIC, attempt=2) is None


def test_nudge_lists_unmet_criteria():
    msg = build_rubric_verify_nudge(
        final_response=INCOMPLETE, rubric=WORKPAPER_RUBRIC, attempt=0)
    assert msg is not None
    assert "privilege-header" in msg and "conclusion" in msg
    # met criterion is not nagged about
    assert "sources" not in msg
    # includes the escape hatch for genuinely-inapplicable criteria
    assert "does not apply" in msg


def test_nudge_resolves_rubric_from_config_when_not_passed():
    msg = build_rubric_verify_nudge(final_response=INCOMPLETE, config=_cfg(), attempt=0)
    assert msg is not None and "privilege-header" in msg
