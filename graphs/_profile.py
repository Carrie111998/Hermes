"""Diego's CV profile summary loader.

Reads the first Summary Variation from the CV Handler's master-resume.md as
a compact profile blob the Matcher node can ground its scoring against.

Later iterations can pull structured profile + skills registry + certifications
via CV Handler's kb-query skill; for Phase-B MVP we use the canonical
master-resume summary directly.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_RESUME_PATH = (
    Path.home() / ".hermes" / "profiles" / "cv-handler" / "workspace" / "kb" / "master-resume.md"
)


# Compact profile card used as system-prompt grounding for scoring. Static
# until we wire CV Handler in Phase B iter 2. Captures the essentials the
# Matcher needs: role shape, comp anchor, geo, industry, credentials.
FALLBACK_PROFILE = """\
Diego De Aragao — CFA, CTP, FDP, CAP-X
Location: St. Petersburg, FL (US Citizen). Open to remote US.
Experience: 15+ years global financial services; Citi, AmEx. Leadership in
  FP&A, balance sheet strategy, risk data platforms, AI/ML for finance.
Expertise:
  - Finance/treasury transformation, balance sheet + liquidity + interest-rate risk
  - AI, ML, RAG, agentic systems applied to finance use cases
  - Product management (data/AI products, cross-border payments)
  - Data governance, model risk management (MCAs/PRSAs/EUCs/MRM)
  - Programs at scale ($2B funding transformation, LIBOR transition)
Degrees: BS CompSci, MS Economics (Finance), MBA, MS Computational Data Analytics
Target seniority: VP / Senior Director / Head-of / Director (IC6-7 or people mgr)
Target comp: $260K base sweet spot; $220-300K+ acceptable; below $180K = bail
Preferred industries: banking, fintech, capital markets, asset mgmt, insurance
Acceptable (neutral): broad SaaS with finance/data angle
Avoid: IT sales, HR, federal contracting, non-finance pure engineering roles
Remote preference: fully remote US > Tampa/St Pete/NYC/Charlotte/Atlanta/Miami
Languages: English, Spanish, Portuguese (all fluent).
"""


@lru_cache(maxsize=1)
def load_profile_summary() -> str:
    """Return compact profile summary grounded on Diego's master resume.

    Prefers the first Summary Variation block in master-resume.md; falls back
    to FALLBACK_PROFILE if the file is unavailable.
    """
    if not _RESUME_PATH.exists():
        return FALLBACK_PROFILE
    try:
        text = _RESUME_PATH.read_text(encoding="utf-8")
        # Extract the first "### ▶ ..." block. Each block is a single paragraph
        # of summary bullets; we concat up to ~1500 chars so the Matcher has
        # context without blowing the LLM context window.
        match = re.search(r"###\s+▶\s+.*?(?=\n###\s+▶\s+|\Z)", text, re.DOTALL)
        if match:
            first_block = match.group(0).strip()
            if len(first_block) > 2500:
                first_block = first_block[:2500] + "\n[truncated]"
            return f"{first_block}\n\n---\n{FALLBACK_PROFILE}"
        return FALLBACK_PROFILE
    except Exception:
        return FALLBACK_PROFILE
