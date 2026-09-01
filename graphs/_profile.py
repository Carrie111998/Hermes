"""Candidate profile summary loader.

Composes the compact profile blob the Matcher and Tailor nodes ground their
output against, from two files under the CV Handler's knowledge base:

  ``master-resume.md``  -- the first Summary Variation block (narrative).
  ``profile-card.md``   -- targeting metadata the resume prose does not state
                           (comp anchor, geography, industry preferences).

NOTHING PERSONAL LIVES IN THIS MODULE. Until 2026-09-01 the card content was a
hardcoded ``FALLBACK_PROFILE`` constant here -- real name, location, citizenship,
employers, degrees and compensation floor -- and this file is tracked, so all of
it was published to a public fork and is no longer retractable (the fork's parent
repository serves the objects regardless of what happens to the fork). The card
now lives beside the resume in local state and is read at runtime; the constant
below is a non-identifying placeholder whose only job is to make its own absence
obvious to whoever reads the model's output.

Keep it that way: if you need a new profile field, add it to ``profile-card.md``,
not to this module. ``tests/graphs/test_profile_no_personal_data.py`` fails the
build if real figures reappear here or in ``_prompts.py``.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_KB_DIR = Path.home() / ".hermes" / "profiles" / "cv-handler" / "workspace" / "kb"
_RESUME_PATH = _KB_DIR / "master-resume.md"
_PROFILE_CARD_PATH = _KB_DIR / "profile-card.md"

# Everything above the card's own `---` rule is prose explaining what the file
# is for; only the body below it is profile content.
_CARD_BODY_SEPARATOR = "\n---\n"

# Shown ONLY when no card is readable. Deliberately schema-shaped and obviously
# empty: a scoring run grounded on this should look wrong at a glance rather
# than quietly score against a plausible-looking stand-in.
PLACEHOLDER_PROFILE = """\
NO CANDIDATE PROFILE LOADED.

The profile card could not be read, so no candidate-specific grounding is
available. Score only what the job description itself supports, mark every
candidate-relative dimension as unknowable (median 5), and say in `gaps` that
the profile was missing.
"""


def _read_profile_card() -> str | None:
    """Return the card's profile body, or None if it is unreadable/empty."""

    try:
        raw = _PROFILE_CARD_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("profile card unreadable at %s: %s", _PROFILE_CARD_PATH, exc)
        return None
    # Split on the FIRST rule so the explanatory header never reaches the model.
    # Deliberately not rpartition: the header is always first, and splitting on
    # the last rule would silently truncate a card whose body contains one.
    _, sep, body = raw.partition(_CARD_BODY_SEPARATOR)
    card = (body if sep else raw).strip()
    if not card:
        logger.warning("profile card at %s has an empty body", _PROFILE_CARD_PATH)
        return None
    return card


def _read_resume_summary() -> str | None:
    """Return the first Summary Variation block from the master resume."""

    try:
        text = _RESUME_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("master resume unreadable at %s: %s", _RESUME_PATH, exc)
        return None
    # Each "### ▶ ..." block is a single paragraph of summary bullets; cap it so
    # the Matcher has context without blowing the LLM context window.
    match = re.search(r"###\s+▶\s+.*?(?=\n###\s+▶\s+|\Z)", text, re.DOTALL)
    if not match:
        logger.warning("no Summary Variation block found in %s", _RESUME_PATH)
        return None
    block = match.group(0).strip()
    if len(block) > 2500:
        block = block[:2500] + "\n[truncated]"
    return block


@lru_cache(maxsize=1)
def load_profile_summary() -> str:
    """Return the compact profile summary used to ground scoring and tailoring.

    Composes the resume's first Summary Variation block with the profile card.
    Either part may be missing; if BOTH are, returns :data:`PLACEHOLDER_PROFILE`
    so the absence is visible in the model's output instead of being papered
    over with stale hardcoded values.
    """

    parts = [part for part in (_read_resume_summary(), _read_profile_card()) if part]
    if not parts:
        logger.error(
            "no candidate profile available -- checked %s and %s",
            _RESUME_PATH,
            _PROFILE_CARD_PATH,
        )
        return PLACEHOLDER_PROFILE
    return "\n\n---\n".join(parts)
