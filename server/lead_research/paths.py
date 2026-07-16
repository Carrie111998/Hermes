"""Profile-aware paths for tenant lead-research data."""
from pathlib import Path

from hermes_constants import get_hermes_home


def tenant_research_root(company_id: str) -> Path:
    safe = "".join(ch for ch in company_id if ch.isalnum() or ch in {"-", "_"})
    if not safe or safe != company_id:
        raise ValueError("invalid company id")
    root = get_hermes_home() / "lead-research" / safe
    root.mkdir(parents=True, exist_ok=True)
    return root
