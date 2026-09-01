"""Deterministic exclusions ahead of the premium seven-dimension ranker.

The matcher is the fleet's most expensive activity by a wide margin. A share of
the jobs it scores are disqualified by facts already printed in the posting —
a mandatory clearance, a stated ceiling well under the floor, a blocklisted
employer. Deciding those without a model call is the cheapest saving available,
and it costs no quality: nothing here is a judgement the ranker would make
better.

**The asymmetry is the whole design.** A job wrongly kept costs one model call.
A job wrongly excluded costs a real opportunity, silently, with no artifact to
notice later. So every rule below fires only on evidence PRESENT in the posting,
and every ambiguity resolves toward eligible. Absent, unparseable, or partial
data is never grounds for exclusion.

Three things deliberately do NOT live here:

* **Domain and seniority fit.** "Non-finance", "too junior", "wrong industry"
  are semantic calls the ranker already makes with a rubric. Approximating them
  with keywords would archive good jobs to save a call.
* **Location.** It is a scored dimension with a documented penalty, not a
  disqualifier, and the acceptable-location lists in the repo disagree.
* **Work authorization.** No job record or criteria file on this machine carries
  a sponsorship signal, so such a filter would have to invent both the rule and
  the data it reads.

Policy lives entirely in :class:`Criteria`. This module hardcodes none of it —
an empty ``Criteria()`` excludes nothing, which is what makes the rules
auditable and adjustable without touching code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
from pathlib import Path
import re
from typing import Any

logger = logging.getLogger(__name__)


class ExclusionReason(str, Enum):
    """Bounded codes. A downstream consumer must never parse free text."""

    UNMET_HARD_REQUIREMENT = "unmet_hard_requirement"
    BELOW_COMPENSATION_FLOOR = "below_compensation_floor"
    EXCLUDED_COMPANY = "excluded_company"


# EMPTY BY MEASUREMENT, not by oversight.
#
# The obvious rule here is "exclude postings that name a credential the
# candidate lacks", and both SOUL.md and the matcher's batch_score_all.py
# already list phrases for it: security clearance, top secret, series 7, cfa
# charterholder required, dfars. Replayed against the 925 real jobs in
# mailbox/tracker/processed/*SCOUT_DISCOVERY*.json those phrases excluded 10
# jobs, of which **1 was a true positive** — CLA, "Series 7, 24, and 65
# licenses required".
#
# The other nine say the opposite of what the keyword implies:
#
# The postings, quoted by their obtainability language rather than by
# employer -- the phrasing is the evidence, the company name is not, and
# naming them publishes the candidate's live application pipeline:
#
#   "already possess OR APPLY UPON ARRIVAL FOR Series 7 and 63"
#   "Series 7 ... (OR ABILITY TO OBTAIN within company policy)"
#   "MAY BE required to obtain ... security clearance"
#   "Security clearance ELIGIBILITY. U.S. citizenship with the ability to
#    obtain" — eligibility the candidate profile may satisfy
#   "ability to obtain ... if required PREFERRED"
#   listed under "Relevant certifications"
#
# A credential obtainable after hire is not a disqualifier, and the difference
# between "must already hold" and "will apply for on arrival" lives in the
# sentence, not the phrase. That is a reading task — precisely what the
# seven-dimension ranker does, and what this filter must not attempt. At 1/10
# precision the rule archives target-profile roles at three of the employers
# above to save ten model calls, which is the expensive error trading against
# the cheap one.
#
# The mechanism stays because Criteria is caller-supplied and a future phrase
# set may earn its place. Anything added here must first be replayed against
# real postings and shown not to fire on obtainability language.
_HARD_REQUIREMENT_PHRASES: tuple[str, ...] = ()

# The candidate's stated bail line, READ FROM LOCAL STATE -- never a literal
# here. It is the single most negotiation-sensitive number in this repository:
# a counterparty who knows the floor knows exactly how low an offer can go. It
# was a hardcoded constant until 2026-09-01 and this file is tracked, so it was
# published to a public fork and is no longer retractable.
#
# Absent or unreadable config yields None, which disables compensation
# filtering entirely. That is the correct failure direction and the same one
# the module docstring argues for throughout: a job wrongly kept costs one
# model call, a job wrongly excluded costs a real opportunity silently. Never
# "helpfully" restore a default figure here.
#
# The repo has historically stated two different floors; they genuinely
# disagree, so the config should carry the LOWER one, which excludes strictly
# fewer jobs. Raising it is a policy decision, not a code change.
_CRITERIA_PATH = (
    Path.home() / ".hermes" / "profiles" / "cv-handler" / "workspace" / "kb"
    / "matcher-criteria.json"
)


def _load_compensation_floor_usd() -> int | None:
    """Read the compensation floor from local state; None if unavailable."""

    try:
        raw = json.loads(_CRITERIA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "matcher criteria unreadable at %s (%s) -- compensation filtering "
            "disabled; no job will be excluded on pay",
            _CRITERIA_PATH,
            exc,
        )
        return None
    value = raw.get("compensation_floor_usd") if isinstance(raw, Mapping) else None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        logger.warning(
            "matcher criteria at %s has no usable compensation_floor_usd -- "
            "compensation filtering disabled",
            _CRITERIA_PATH,
        )
        return None
    return value


_COMPENSATION_FLOOR_USD = _load_compensation_floor_usd()

_EXCLUDED_COMPANIES: tuple[str, ...] = ("dataannotation",)


@dataclass(frozen=True)
class Criteria:
    """Everything this filter is allowed to act on.

    Empty by default so that a caller who supplies nothing excludes nothing;
    :data:`DEFAULT_CRITERIA` carries the documented policy.
    """

    hard_requirement_phrases: tuple[str, ...] = ()
    compensation_floor_usd: int | None = None
    excluded_companies: tuple[str, ...] = ()


DEFAULT_CRITERIA = Criteria(
    hard_requirement_phrases=_HARD_REQUIREMENT_PHRASES,
    compensation_floor_usd=_COMPENSATION_FLOOR_USD,
    excluded_companies=_EXCLUDED_COMPANIES,
)


@dataclass(frozen=True)
class FilterDecision:
    """Why a job was or was not sent to the ranker.

    ``suppressed_reasons`` exists so a VIP exemption stays visible: the job goes
    through, but the operator can still see what would otherwise have stopped
    it. An exemption that hid its own cause would be indistinguishable from the
    filter never firing.
    """

    eligible: bool
    reasons: tuple[ExclusionReason, ...] = ()
    vip_exempt: bool = False
    suppressed_reasons: tuple[ExclusionReason, ...] = field(default=())


# "series 7" must not match "series 700". `\b` cannot express that: between the
# "7" and the "0" both characters are word characters, so no boundary exists
# there and `r"series 7\b"` simply fails to match — but `"series 7" in text`
# succeeds, which is the trap this replaces. The explicit lookarounds assert
# "no word character adjacent", which is the property actually wanted.
def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w]){re.escape(phrase)}(?![\w])", re.IGNORECASE)


_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _matches(phrase: str, haystack: str) -> bool:
    pattern = _PATTERN_CACHE.get(phrase)
    if pattern is None:
        pattern = _PATTERN_CACHE[phrase] = _phrase_pattern(phrase)
    return pattern.search(haystack) is not None


_HOURLY = re.compile(r"\b(?:per|an|/)\s*(?:hour|hr)\b|\bhourly\b", re.IGNORECASE)
_AMOUNT = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?")


def _structured_ceiling_usd(raw: Mapping[str, Any]) -> int | None:
    """Read Scout's structured salary record.

    Shape (mailbox/tracker/processed/*SCOUT_DISCOVERY*.json):
    ``{'min': 165000.0, 'max': 205000.0, 'currency': 'USD',
    'period': 'annual', 'source': 'posted'}``.

    Better evidence than the string form, because period and currency are
    stated rather than inferred — so anything not explicitly an annual USD
    ceiling is declined rather than guessed at.
    """
    currency = raw.get("currency")
    if isinstance(currency, str) and currency.strip().upper() not in {"USD", ""}:
        return None

    period = raw.get("period")
    if isinstance(period, str) and period.strip().lower() not in {"annual", "yearly", "year", ""}:
        return None

    ceiling = raw.get("max")
    # `min` alone is never enough: an open-topped band tops out above the floor
    # far more often than not.
    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool):
        return None
    return int(ceiling) if ceiling >= 1_000 else None


def _stated_ceiling_usd(raw: Any) -> int | None:
    """The highest annual figure a salary record actually commits to.

    Returns None whenever the value cannot be read with confidence — an
    undisclosed, descriptive, foreign-currency or hourly posting must reach the
    ranker rather than be archived on a guess. Only the ceiling is used: a range
    is an invitation to negotiate at its top, and excluding on the floor of a
    wide band would archive most senior postings.
    """
    if isinstance(raw, Mapping):
        return _structured_ceiling_usd(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    if _HOURLY.search(raw):
        # An hourly rate is not comparable to an annual floor without assuming
        # a schedule. $85/hr reads as "85" and would look catastrophically low.
        return None

    values: list[int] = []
    for amount, suffix in _AMOUNT.findall(raw):
        try:
            number = float(amount.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            number *= 1_000
        values.append(int(number))

    if not values:
        return None
    ceiling = max(values)
    # A bare "$95" or "$120" is a unit this function cannot identify; treating
    # it as an annual salary would exclude on a misread.
    return ceiling if ceiling >= 1_000 else None


def _text(job: Mapping[str, Any], *keys: str) -> str:
    parts = [job.get(key) for key in keys]
    return " ".join(part for part in parts if isinstance(part, str))


def _is_vip(job: Mapping[str, Any]) -> bool:
    vip = job.get("vip")
    if isinstance(vip, Mapping):
        return bool(vip.get("is_vip"))
    return bool(vip) if isinstance(vip, bool) else False


def hard_filter(job: Mapping[str, Any], criteria: Criteria) -> FilterDecision:
    """Decide whether ``job`` is worth a premium scoring call.

    Never raises on a malformed job: an unreadable posting is eligible, because
    the ranker can look at it and this function cannot.
    """
    if not isinstance(job, Mapping):
        return FilterDecision(eligible=True)

    reasons: list[ExclusionReason] = []

    haystack = _text(job, "title", "description")
    if haystack and any(
        _matches(phrase, haystack) for phrase in criteria.hard_requirement_phrases
    ):
        reasons.append(ExclusionReason.UNMET_HARD_REQUIREMENT)

    if criteria.compensation_floor_usd is not None:
        ceiling = _stated_ceiling_usd(job.get("salary_range"))
        if ceiling is not None and ceiling < criteria.compensation_floor_usd:
            reasons.append(ExclusionReason.BELOW_COMPENSATION_FLOOR)

    company = job.get("company")
    if isinstance(company, str) and criteria.excluded_companies:
        # Exact match on the normalised name. Substring matching would archive
        # an innocent employer whose name merely contains a blocklisted one.
        normalised = company.strip().casefold()
        if normalised in {name.casefold() for name in criteria.excluded_companies}:
            reasons.append(ExclusionReason.EXCLUDED_COMPANY)

    if not reasons:
        return FilterDecision(eligible=True)

    if _is_vip(job):
        # A standing exemption outranks every rule here, but the cause stays
        # on the record.
        return FilterDecision(
            eligible=True, vip_exempt=True, suppressed_reasons=tuple(reasons)
        )

    return FilterDecision(eligible=False, reasons=tuple(reasons))
