"""Contract test: the Critic daily skill-review SOUL persona and orchestrator
skill both require the structured `brief` — the stable title line, the six
section labels, and count-form rejected evidence — and the documented SOUL
example blocks parse through the real agent-iteration extractor.

Hermetic by construction: this test reads only source-controlled files
(`profiles/critic/SOUL.md` and the orchestrator skill's `SKILL.md`). It does
NOT read `profiles/main/cron/jobs.json`, which is a per-deployment runtime file
that `.gitignore` excludes and that is absent from a clean checkout. The live
cron prompt carries the same contract, but enforcing it belongs to the deploy
step against the shared runtime file, not to a clean-checkout unit test.
"""
import re
from pathlib import Path

from cron.scheduler import AGENT_ITERATION_MARKER_RE, _extract_agent_iteration

REPO = Path(__file__).resolve().parents[3]
SOUL = REPO / "profiles" / "critic" / "SOUL.md"
SKILL = (
    REPO / "profiles" / "main" / "skills" / "orchestrator"
    / "critic-daily-skill-ranking-pass" / "SKILL.md"
)

# The stable Mission Control brief header the Critic must lead every daily
# skill-review brief with (renderer shows the brief verbatim).
TITLE_LINE = "── Critic · daily skill review ──"

REQUIRED_SECTIONS = [
    "VERDICT",
    "REVIEWED",
    "TOP REJECTED EVIDENCE",
    "SCORES MOVED",
    "RETIREMENT FLAGS",
    "ACTION NEEDED",
]


def test_soul_requires_title_and_all_sections():
    soul = SOUL.read_text(encoding="utf-8")
    assert TITLE_LINE in soul, "SOUL must mandate the brief title line"
    for section in REQUIRED_SECTIONS:
        assert section in soul, f"SOUL missing required section: {section}"


def test_skill_requires_title_sections_and_rejected_evidence():
    skill = SKILL.read_text(encoding="utf-8")
    assert TITLE_LINE in skill, "SKILL must mandate the brief title line"
    for section in REQUIRED_SECTIONS:
        assert section in skill, f"SKILL missing required section: {section}"
    # up to three concrete rejected-evidence groups (skill × count → reason)
    assert re.search(r"×\s*\d|x\s*\d", skill), "SKILL must show a count-form rejected-evidence example"


def test_soul_examples_parse_and_carry_brief():
    soul = SOUL.read_text(encoding="utf-8")
    blocks = AGENT_ITERATION_MARKER_RE.findall(soul)
    briefs = []
    for raw in blocks:
        parsed, err, _ = _extract_agent_iteration(
            f"<AGENT_ITERATION_JSON>{raw}</AGENT_ITERATION_JSON>"
        )
        assert err is None, f"SOUL example failed to parse: {err}"
        if "brief" in parsed:
            briefs.append(parsed["brief"])
    assert briefs, "at least one SOUL example must carry a brief"
    joined = "\n".join(briefs)
    # every example brief leads with the stable title line...
    for brief in briefs:
        assert brief.lstrip().startswith(TITLE_LINE), (
            "each SOUL example brief must lead with the title line"
        )
    # ...and the section labels appear across the examples
    for section in REQUIRED_SECTIONS:
        assert section in joined, f"no SOUL example brief contains {section}"


def test_soul_has_no_work_and_actionable_examples():
    soul = SOUL.read_text(encoding="utf-8")
    blocks = AGENT_ITERATION_MARKER_RE.findall(soul)
    reasons = []
    has_action = False
    for raw in blocks:
        parsed, err, _ = _extract_agent_iteration(
            f"<AGENT_ITERATION_JSON>{raw}</AGENT_ITERATION_JSON>"
        )
        assert err is None
        reasons.append(parsed.get("reason"))
        if "brief" in parsed and "PROPOSED (needs you)" in parsed["brief"]:
            has_action = True
    assert "no_work" in reasons, "SOUL must keep a no_work example"
    assert has_action, "SOUL must include an actionable example with a PROPOSED line"
