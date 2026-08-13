"""Build a labelled evaluation set from evidence the scorer did not produce.

The premium-routing plan gates every route change on a golden set. The tempting
source is the pipeline's own history, and it is mostly poison: of 5,189 rows,
4,670 are archived — archived *because the scorer scored them low*. Grading the
scorer against its own past verdicts yields near-perfect agreement and says
nothing at all about quality. That circularity is the failure this module
exists to prevent, and it is not hypothetical: an auto-route approval and a
human approval land in the same stage, with the same shape, distinguishable
only by an actor buried in a history entry.

Two label sources survive the test "would this still be true if the scorer were
wrong?":

**Human decisions.** A person moved the job through the approval gate
(``actor_id: diego`` via ``tracker-intent-applier``). These are the *nuanced*
cases — the ones a scorer can plausibly get wrong — so they carry the weight.

**Posting facts.** A stated compensation ceiling below the bail line decides the
job whatever any model thinks. These are *obvious*, and are labelled as such
because an evaluation set padded with easy negatives flatters every scorer that
reads it.

Difficulty is therefore recorded per item, and :func:`summarize` refuses to call
a set balanced when one difficulty dominates.

Items carry no posting text — only a SHA-256 reference — so the set can be
committed while the postings stay where they are.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any

from .matcher_filter import Criteria, hard_filter


class Label(str, Enum):
    ADVANCE = "advance"
    EXCLUDE = "exclude"


class LabelSource(str, Enum):
    HUMAN_APPROVAL = "human_approval"
    DETERMINISTIC_EXCLUSION = "deterministic_exclusion"


class Difficulty(str, Enum):
    OBVIOUS = "obvious"
    NUANCED = "nuanced"


# The intent applier stamps a real person's decision. Everything else that
# reaches stage `approved` — score routing, VIP auto-approval — is the system
# agreeing with itself.
_HUMAN_ACTORS = ("diego",)

# Markers of a machine approval. Present only so the circular case is named in
# code rather than merely absent from it.
_AUTO_MARKERS = ("main-auto-route", "auto-approved under", "auto_approve")

# Below this share, one difficulty dominates and the set cannot distinguish a
# good scorer from one that only handles easy cases.
_MIN_DIFFICULTY_SHARE = 0.2


@dataclass(frozen=True)
class GoldenItem:
    """One labelled job. Immutable, and free of posting text by construction."""

    job_id: str
    label: Label
    source: LabelSource
    difficulty: Difficulty
    evidence: str
    description_sha256: str
    conflicted: bool = False


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _has_human_decision(row: Mapping[str, Any]) -> bool:
    history = row.get("history")
    if not isinstance(history, list):
        return False
    for entry in history:
        if not isinstance(entry, Mapping):
            continue  # a malformed entry is not evidence either way
        try:
            blob = json.dumps(entry).lower()
        except (TypeError, ValueError):
            continue
        if any(f'"actor_id": "{actor}"' in blob for actor in _HUMAN_ACTORS):
            return True
    return False


def _deterministic_exclusion(row: Mapping[str, Any], criteria: Criteria) -> str | None:
    """Reuse the production filter so the label and the runtime rule cannot drift."""
    job = dict(row)
    job["description"] = row.get("description_raw") or row.get("description") or ""
    decision = hard_filter(job, criteria)
    if decision.eligible or not decision.reasons:
        return None
    return decision.reasons[0].value


def build_golden_set(
    jobs: Mapping[str, Any],
    criteria: Criteria,
) -> tuple[GoldenItem, ...]:
    """Label what can be labelled honestly, and skip the rest.

    Silence is the common answer: an ordinary scored-then-archived row carries
    no admissible evidence and produces nothing. Never raises — a malformed row
    is skipped, because a crash here would be indistinguishable from an empty
    result to the caller that matters.
    """
    if not isinstance(jobs, Mapping):
        return ()

    items: list[GoldenItem] = []
    for job_id, row in jobs.items():
        if not isinstance(job_id, str) or not job_id.strip():
            continue
        if not isinstance(row, Mapping):
            continue

        digest = _sha256(str(row.get("description_raw") or row.get("description") or ""))
        human = _has_human_decision(row)
        excluded = _deterministic_exclusion(row, criteria)

        if human:
            # A person overruling a deterministic rule is the more informative
            # fact, and one job must never yield two contradictory labels.
            items.append(GoldenItem(
                job_id=job_id,
                label=Label.ADVANCE,
                source=LabelSource.HUMAN_APPROVAL,
                difficulty=Difficulty.NUANCED,
                evidence=LabelSource.HUMAN_APPROVAL.value,
                description_sha256=digest,
                conflicted=excluded is not None,
            ))
        elif excluded is not None:
            items.append(GoldenItem(
                job_id=job_id,
                label=Label.EXCLUDE,
                source=LabelSource.DETERMINISTIC_EXCLUSION,
                difficulty=Difficulty.OBVIOUS,
                evidence=excluded,
                description_sha256=digest,
            ))

    # Sorted so a rebuild is byte-comparable with the last one.
    return tuple(sorted(items, key=lambda i: i.job_id))


def balanced_sample(items: tuple[GoldenItem, ...]) -> tuple[GoldenItem, ...]:
    """Cap every difficulty at the scarcest one, deterministically.

    The real set is 82% obvious negatives, and used whole it grades any
    competent ranker near-perfectly while hiding the nuanced failures worth
    finding. Capping to parity is what makes it able to discriminate.

    Selection is by sorted ``job_id`` rather than a shuffle, because two runs
    that graded different samples produce scores nobody can compare.

    A set containing only one difficulty returns empty: there is nothing to
    balance against, and quietly returning the easy half would restore exactly
    the flattery this removes.
    """
    if not items:
        return ()

    buckets: dict[Difficulty, list[GoldenItem]] = {}
    for item in items:
        buckets.setdefault(item.difficulty, []).append(item)
    if len(buckets) < len(Difficulty):
        return ()

    cap = min(len(bucket) for bucket in buckets.values())
    kept: list[GoldenItem] = []
    for bucket in buckets.values():
        kept.extend(sorted(bucket, key=lambda i: i.job_id)[:cap])
    return tuple(sorted(kept, key=lambda i: i.job_id))


def summarize(items: tuple[GoldenItem, ...]) -> dict[str, Any]:
    """Describe the set, including whether it is fit to judge anything.

    ``balanced`` is the load-bearing field. A set that is 95% obvious negatives
    will score any competent ranker near-perfectly and hide exactly the failures
    worth finding, so it reports False rather than letting a headline accuracy
    stand unqualified.
    """
    by_label: dict[str, int] = {}
    by_difficulty: dict[str, int] = {}
    for item in items:
        by_label[item.label.value] = by_label.get(item.label.value, 0) + 1
        by_difficulty[item.difficulty.value] = by_difficulty.get(item.difficulty.value, 0) + 1

    total = len(items)
    balanced = bool(total) and all(
        by_difficulty.get(d.value, 0) / total >= _MIN_DIFFICULTY_SHARE
        for d in Difficulty
    )
    return {
        "total": total,
        "by_label": by_label,
        "by_difficulty": by_difficulty,
        "conflicted": sum(1 for i in items if i.conflicted),
        "balanced": balanced,
    }
