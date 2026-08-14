#!/usr/bin/env python3
"""Repro: workflow files lack mandatory review protection (no CODEOWNERS).

Bug class: #23632 / #31935 — no .github/CODEOWNERS means workflow files
(.github/workflows/*.yml) can be modified without mandatory review,
a supply-chain risk for a repo whose CI runs arbitrary code.

On main: FAILS (no CODEOWNERS entry for workflows). With the fix: PASSES.
"""
import sys
from pathlib import Path

repo = Path(__file__).resolve().parent
codeowners = repo / ".github" / "CODEOWNERS"

if not codeowners.exists():
    print("FAIL: .github/CODEOWNERS does not exist")
    sys.exit(1)

text = codeowners.read_text(encoding="utf-8")
lines = {
    line.strip()
    for line in text.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}
workflow_covered = ".github/workflows/ @NousResearch/hermes-maintainers" in lines
if workflow_covered:
    print("PASS: workflow paths covered by CODEOWNERS")
    sys.exit(0)
print("FAIL: no CODEOWNERS rule covers .github/workflows/")
sys.exit(1)
