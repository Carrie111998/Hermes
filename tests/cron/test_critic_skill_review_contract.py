"""Contract test: the Critic daily skill-review prompt, SOUL, and skill all
require the structured `brief`, and the documented example blocks parse
through the real agent-iteration extractor."""
import json
import re
from pathlib import Path

from cron.scheduler import AGENT_ITERATION_MARKER_RE, _extract_agent_iteration

REPO = Path(__file__).resolve().parents[3]
SOUL = REPO / "profiles" / "critic" / "SOUL.md"
JOBS = REPO / "profiles" / "main" / "cron" / "jobs.json"
SKILL = (
    REPO / "profiles" / "main" / "skills" / "orchestrator"
    / "critic-daily-skill-ranking-pass" / "SKILL.md"
)

REQUIRED_SECTIONS = [
    "VERDICT",
    "REVIEWED",
    "TOP REJECTED EVIDENCE",
    "SCORES MOVED",
    "RETIREMENT FLAGS",
    "ACTION NEEDED",
]


def _critic_prompt() -> str:
    data = json.loads(JOBS.read_text(encoding="utf-8"))
    jobs = data["jobs"] if isinstance(data, dict) and "jobs" in data else data
    matches = [j for j in jobs if j.get("name") == "critic-skill-review"]
    assert len(matches) == 1, f"expected exactly one critic-skill-review job, got {len(matches)}"
    return json.dumps(matches[0])  # search across prompt/instructions fields


def test_prompt_requires_all_sections():
    prompt = _critic_prompt()
    for section in REQUIRED_SECTIONS:
        assert section in prompt, f"prompt missing required section: {section}"
    assert "brief" in prompt


def test_soul_requires_all_sections():
    soul = SOUL.read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        assert section in soul, f"SOUL missing required section: {section}"


def test_skill_requires_sections_and_rejected_evidence():
    skill = SKILL.read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        assert section in skill, f"SKILL missing required section: {section}"
    # up to three concrete rejected-evidence groups (skill × count → reason)
    assert "TOP REJECTED EVIDENCE" in skill
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
    # every section present in at least one example brief
    joined = "\n".join(briefs)
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
