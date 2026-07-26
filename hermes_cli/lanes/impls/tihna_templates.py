"""Editable prompt templates for the Tihna weekly digest."""

CLASSIFY_PROMPT = """
You are ranking items for a weekly Tihna frequency-therapy trends digest.
Score each item 0-100 on:
  - novelty (is this new information)
  - signal_strength (peer-reviewed, primary source, or authoritative)
  - relevance to frequency therapy, binaural beats, brainwave entrainment,
    or neuroscience of consciousness

Return valid JSON only, matching:
  [{"external_id": "...", "score": <int>, "reason": "<one sentence>"}, ...]

Items to score:
<ITEMS>
""".strip()

DIGEST_PROMPT = """
Write a weekly Tihna trends digest as valid Markdown with exactly these 5 sections:

# Tihna Weekly Trends — <WEEK_LABEL>

## Signal Summary
  3-4 sentences of top-line takeaway.

## Notable Papers
  Bullet list of peer-reviewed / primary-source items with 1-line summary and link.

## Community Chatter
  Bullet list of subreddit / forum / Substack items with 1-line summary and link.

## Adjacent Tech
  Bullet list of items about neuroscience tools, wearables, sensors that touch
  the frequency-therapy space, with 1-line summary and link.

## Recommended Follow-ups
  2-3 numbered suggestions for what Adrian should read/watch/investigate this week.

Use the ranked items below. Only include items with score >= 60.
Preserve original links exactly. Do not fabricate items or links.

Ranked items:
<RANKED_ITEMS>
""".strip()

SECTION_HEADINGS = (
    "## Signal Summary",
    "## Notable Papers",
    "## Community Chatter",
    "## Adjacent Tech",
    "## Recommended Follow-ups",
)

__all__ = ["CLASSIFY_PROMPT", "DIGEST_PROMPT", "SECTION_HEADINGS"]
