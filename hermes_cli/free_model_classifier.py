"""Classifier for free-first dispatch routing.

Determines if a kanban task is eligible to route via free models first
(OpenCode *-free -> OpenRouter :free -> Haiku fallback) based on:
1. PROVEN-SKILL: routine deterministic tasks (enumerate, count, file-ops, etc.)
2. NO-PII: no genealogy identity, no Treva rows, no credentials, no SoT writes
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
PII_SENSITIVE_KEYWORDS = {
    # Identity/genealogy
    'genealogy', 'person', 'identity', 'pii', 'personal', 'private',
    'genealog', 'ancestry', 'dna', 'genetic', 'family', 'ancestor',
    
    # Sensitive data (narrow to actual credentials, not "auth" alone which is too broad)
    'credential', 'secret', 'password', 'token', 'key', 
    'oauth', 'api-key', 'api_key', 'sensitive',
    
    # Source of truth (actual SoT terms, not generic "database")
    'sot', 'source-of-truth', 'source_of_truth', 'canonical',
    'ocr_ground_truth', 'nara', 'treva',
    
    # Reasoning/management (harder to parallelize)
    'cos', 'chief-of-staff', 'spm', 'senior-project', 'decompose',
    'incident', 'incident-response', 'post-mortem', 'postmortem',
    'reasoning', 'think', 'strategy', 'plan',
}

# Exact terms to always block from free routing
PII_BLOCK_TERMS = {
    'genealogy', 'ocr_ground_truth', 'nara_transcription', 'treva',
    'person', 'pii', 'credential', 'secret', 'ocr-genealogy',
    'genealogy-identity', 'graves-genealogy', 'graves-gps',
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
    """Check if task involves PII, genealogy identity, credentials, or SoT writes.
    
    Returns True if the task should stay on Haiku (no free routing).
    """
    combined = _normalize_text((title or "") + " " + (body or ""))
    
    # Hard block terms that must NEVER route free
    for term in PII_BLOCK_TERMS:
        if term in combined:
            return True
    
    # Heuristic: multiple sensitive keywords (genealogy + person, etc.)
    sensitive_count = sum(1 for kw in PII_SENSITIVE_KEYWORDS if kw in combined)
    if sensitive_count >= 2:
        return True
    
    # Single strong indicator
    if any(kw in combined for kw in ['genealogy', 'credential', 'secret', 'sot', 'cos']):
        return True
    
    return False


def should_route_free_first(task_id: str, title: Optional[str], body: Optional[str], 
                           model_override: Optional[str] = None) -> bool:
    """Determine if a task should route via free models first.
    
    Returns True if:
    1. Task has no explicit model_override (use free classifier)
    2. Task is PROVEN-SKILL (routine, deterministic)
    3. Task is NO-PII (no genealogy identity, credentials, SoT writes)
    
    Returns False if:
    1. Task has explicit model_override (respect the override)
    2. Task is NOT proven-skill (reasoning-heavy, management)
    3. Task involves PII/genealogy identity/credentials/SoT (stay on Haiku)
    """
    # Respect explicit model overrides
    if model_override:
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
