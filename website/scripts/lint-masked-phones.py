#!/usr/bin/env python3
"""Docs lint: reject masked example phone numbers inside code blocks.

The docs historically shipped placeholder phone numbers like ``+155****4567``
(and ``+123****7890``) inside copy-paste commands and env-var examples.
Because the asterisks are literal, a user who copies the example sends a
garbage number to the platform. This check fails CI when a masked E.164
number appears in any doc file.

Exits 0 on clean, 1 on violations (printing each file + line).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "docs"

# +1 followed by a run of digits/asterisks containing 3+ consecutive asterisks
MASKED_RE = re.compile(r"\+\d{1,3}[\d\*]{3,}\*{3,}[\d\*]*")

violations = []
for p in sorted(ROOT.rglob("*.md")):
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if MASKED_RE.search(line):
            violations.append(f"{p.relative_to(ROOT.parent)}:{lineno}: {line.strip()}")

if violations:
    print("Masked example phone numbers found (use a valid fictional E.164 like +15550123456):")
    for v in violations:
        print(f"  {v}")
    sys.exit(1)

print("OK: no masked phone numbers in docs")
