"""Classifier for free-first dispatch routing.

Determines if a kanban task is eligible to route via free models first
(OpenCode *-free -> OpenRouter :free -> Haiku fallback) based on:
1. PROVEN-SKILL: routine deterministic tasks (enumerate, count, file-ops, etc.)
2. NO-CREDENTIAL: no credentials/secrets, no SoT writes, no Treva-time spend

Genealogy is NOT a gate. That block was invented by an agent, has no git provenance
(this file is untracked), and contradicts his verbatim law — his genealogy is public
record, records of dead people. Removed 2026-08-27. See `no-invented-gate`.

All keyword matching is WORD-BOUNDED. Naive substring tests made two-letter and short
tokens match inside ordinary words ('ls' in "manuals", 'key' in "monkey"), which both
free-routed work that should not have been and blocked work that was never sensitive.
"""

import re
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


# Keywords that indicate a task is PROVEN-SKILL (routine, deterministic)
PROVEN_SKILL_KEYWORDS = {
    # Enumeration/counting/measurement
    'enumerate', 'count', 'list', 'measure', 'stat', 'ls', 'find', 'search',
    'grep', 'scan', 'audit', 'inventory', 'tally', 'tabulate',
    
    # File operations
    'file-op', 'read', 'write', 'copy', 'move', 'delete', 'mkdir', 'rmdir',
    'archive', 'extract', 'compress', 'format', 'transform',
    
    # Research/collection
    'research', 'research-bank', 'gather', 'collect', 'fetch', 'download',
    'scrape', 'index', 'catalog', 'ingest',
    
    # Documentation generation
    'doc-gen', 'generate', 'template', 'render', 'format', 'markdown',
    'report', 'summary', 'digest', 'changelog', 'document',
    
    # Simple fixes/hygiene (narrow to avoid false positives with incidents)
    'hygiene', 'cleanup', 'lint', 'simple-fix', 'repair', 'patch',
    'simplify', 'refactor', 'optimize', 'format-code',
    
    # Board management
    'board-hygiene', 'archive', 'dedup', 'triage', 'sort', 'classify',
    'tag', 'label', 'organize',
}

# Keywords that indicate a task is PII/sensitive (must stay on Haiku)
#
# GENEALOGY TERMS REMOVED 2026-08-27 — they were an INVENTED GATE.
# This file is untracked in git: no commit, no directive, no AGENTS.md line ever created
# a genealogy PII block. Christopher's verbatim law (plans/RESEARCH-gecko-bioclip.md:264):
#   "his genealogy = public record; the commons = opt-in — no PII/security gate needed"
# Skill `no-invented-gate` names the only three real gates: occupancy.py, no-paid-cloud,
# don't-fabricate-a-run. These are public-domain records of dead people.
# Removed: genealogy, genealog, person, identity, pii, personal, private, ancestry, dna,
#          genetic, family, ancestor, ocr_ground_truth, nara.
PII_SENSITIVE_KEYWORDS = {
    # Actual credentials (no-paid-cloud — a REAL gate)
    'credential', 'secret', 'password', 'token', 'key',
    'oauth', 'api-key', 'api_key', 'sensitive',

    # Source of truth — a write here is a genuine risk, not a privacy theory
    'sot', 'source-of-truth', 'source_of_truth', 'canonical',

    # A LIVING person's protected time (CARDINAL RULE, workspace/scars.jsonl).
    # Not PII — his mother's hours are not to be spent on already-done work.
    'treva',

    # Reasoning/management: capability routing, not privacy. Poor free-model fits.
    'cos', 'chief-of-staff', 'spm', 'senior-project', 'decompose',
    'incident', 'incident-response', 'post-mortem', 'postmortem',
    'reasoning', 'think', 'strategy', 'plan',
}

# Exact terms to always block from free routing.
# Same removal: only real gates remain.
PII_BLOCK_TERMS = {
    'credential', 'secret', 'treva',
}


def _normalize_text(text: Optional[str]) -> str:
    """Normalize text for keyword matching."""
    if not text:
        return ""
    return text.lower().replace("_", "-").replace(" ", "-")


def is_proven_skill(task_id: str, title: Optional[str], body: Optional[str]) -> bool:
    """Check if task title/body indicate a PROVEN-SKILL routine task.

    Returns True if the task name/body contains keywords matching known
    routine patterns (enumerate, count, file-ops, research, doc-gen, fix, hygiene).

    Matching is WORD-BOUNDED. A naive substring test made the two-letter keyword
    'ls' match inside ordinary text — "manuals", "tools", "models", "skills",
    "recalls", "details", "false" — so almost any card looked like a proven skill
    and was routed to a free model. Measured 2026-08-27: a card titled "ship it"
    with a standard MANUALS block matched on 'ls' alone.
    """
    combined = _normalize_text((title or "") + " " + (body or ""))

    # _normalize_text maps "_" and " " to "-", so tokens are hyphen-delimited.
    for keyword in PROVEN_SKILL_KEYWORDS:
        pattern = r"(?<![a-z0-9])" + re.escape(_normalize_text(keyword)) + r"(?![a-z0-9])"
        if re.search(pattern, combined):
            return True

    return False


def is_pii_or_sensitive(task_id: str, title: Optional[str], body: Optional[str]) -> bool:
    """Check if a task must stay off free models.

    Returns True if the task should stay on Haiku (no free routing).

    Matching is WORD-BOUNDED for the same reason as is_proven_skill: a substring test
    made short tokens ('key', 'plan', 'sot') match inside ordinary words — "monkey",
    "keyword", "planting" — blocking work that was never sensitive.

    Genealogy is NOT a gate here. See PII_SENSITIVE_KEYWORDS above: the genealogy block
    was invented, has no git provenance, and contradicts his verbatim law. Public-domain
    records of dead people route free like any other routine work.
    """
    combined = _normalize_text((title or "") + " " + (body or ""))

    def _hit(term: str) -> bool:
        pattern = r"(?<![a-z0-9])" + re.escape(_normalize_text(term)) + r"(?![a-z0-9])"
        return re.search(pattern, combined) is not None

    # Hard block terms that must NEVER route free
    for term in PII_BLOCK_TERMS:
        if _hit(term):
            return True

    # Heuristic: two or more sensitive signals together
    if sum(1 for kw in PII_SENSITIVE_KEYWORDS if _hit(kw)) >= 2:
        return True

    # Single strong indicator. 'genealogy' removed 2026-08-27 — it was the second,
    # independent copy of the invented gate and would have kept the lane blocked even
    # after PII_BLOCK_TERMS was cleaned.
    if any(_hit(kw) for kw in ('credential', 'secret', 'sot', 'cos')):
        return True

    return False


def needs_tools_to_finish(title: Optional[str], body: Optional[str]) -> bool:
    """True when a card's DONE-CONDITION requires acting on the world, not answering.

    The free-model wrapper (~/.hermes/scripts/free-model-worker-wrapper.py) is a
    ONE-SHOT PROMPT RUNNER: it takes argv[1], prints the model's text, and exits. It
    has no kanban tools, no terminal, and no completion path. A free-routed card
    therefore CANNOT write a file, run a command, or call kanban_complete.

    Measured 2026-08-27 on sandbox card t_400d4e4e: it free-routed, the model answered,
    and the run ended `reclaimed` with the card archived unfinished. The work was
    silently lost. Routing a tool-requiring card to a tool-less worker is not a cheap
    win, it is a guaranteed dead card.

    So free routing is for cards whose DELIVERABLE IS TEXT (classify, summarize, draft,
    explain). Anything whose done-condition names a file, a command, a diff, or a board
    transition needs a real worker.
    """
    combined = _normalize_text((title or "") + " " + (body or ""))
    tool_markers = (
        # producing an artifact
        "write", "create", "generate", "output-to", "save", "append", "commit",
        "patch", "edit", "delete", "archive", "mint", "emit",
        # running something
        "run", "execute", "invoke", "dispatch", "spawn", "install", "build",
        "test", "pytest", "curl", "sqlite3", "python3", "bash",
        # board transitions the wrapper cannot perform
        "kanban-complete", "kanban-request-review", "request-review", "handoff",
        # done-conditions that assert on the filesystem
        "exists", "test-f", "test-s", "file-exists", "prints", "jq",
    )
    return any(
        re.search(r"(?<![a-z0-9])" + re.escape(m) + r"(?![a-z0-9])", combined)
        for m in tool_markers
    )


def should_route_free_first(task_id: str, title: Optional[str], body: Optional[str], 
                           model_override: Optional[str] = None) -> bool:
    """Determine if a task should route via free models first.
    
    Returns True if:
    1. Task has no explicit model_override (use free classifier)
    2. Task is PROVEN-SKILL (routine, deterministic)
    3. Task is NO-CREDENTIAL (no secrets, no SoT writes, no Treva-time spend)
    4. Task's deliverable is TEXT (the free wrapper has no tools — see
       needs_tools_to_finish)
    
    Returns False if:
    1. Task has explicit model_override (respect the override)
    2. Task is NOT proven-skill (reasoning-heavy, management)
    3. Task involves credentials/SoT writes/Treva time (stay on Haiku)
    4. Task must write a file, run a command, or close a card
    """
    # Respect explicit model overrides
    if model_override:
        return False
    
    # The free wrapper cannot act on the world. Sending it a card whose done-condition
    # is a file or a command produces a dead card, not a cheap one.
    if needs_tools_to_finish(title, body):
        return False

    # Check if task is a proven-skill routine
    if not is_proven_skill(task_id, title, body):
        # Not a routine task — default to Haiku for reasoning/management
        return False
    
    # Check for PII/sensitive/SoT patterns
    if is_pii_or_sensitive(task_id, title, body):
        # Sensitive task — stay on Haiku for reliability
        return False
    
    # Passed all checks: eligible for free-first routing
    return True


def resolve_model_for_free_routing(
    task_id: str,
    title: Optional[str],
    body: Optional[str],
    model_override: Optional[str] = None,
    provider_override: Optional[str] = None,
    *,
    default_model: str = "claude-haiku-4-5-20251001",
    default_provider: str = "anthropic",
) -> tuple[Optional[str], Optional[str]]:
    """Resolve model and provider for a task, applying free-first classifier.
    
    Returns (model, provider) tuple where:
    - If task has explicit override: returns the override
    - If eligible for free-first: returns a concrete free model id
    - Otherwise: returns default Haiku
    
    This extends the EXISTING model resolution in kanban_db.py without adding
    speculative hooks. Free-eligible tasks now route via OpenCode free models
    with Haiku fallback on non-zero/timeout/404.
    """
    # Respect explicit overrides
    if model_override:
        return model_override, provider_override
    
    # Classify the task
    if should_route_free_first(task_id, title, body, model_override):
        # Route to OpenCode free model (concrete, not None sentinel)
        # The dispatcher will pass this as -m flag; the worker inherits
        # no fallback here — fallback is handled in the free wrapper script.
        # Provider is "opencode-free" which maps to the keyless free endpoint.
        return "opencode/nemotron-3.5-lightning-free", "opencode-free"
    
    # Default to Haiku (PII, reasoning, SoT, or missing proven-skill signal)
    return default_model, default_provider
