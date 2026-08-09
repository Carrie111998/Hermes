#!/usr/bin/env python3
"""Report gitleaks findings that a change *introduces*, by secret value.

Used by ``.github/workflows/secret-scan.yml``. The workflow materializes the
changed files at the base revision and at HEAD, scans each into a JSON report,
and calls this to decide which findings are new.

Why not ``gitleaks --baseline-path``
------------------------------------
A baseline entry is matched on location (file, rule, line) plus ``Entropy``,
and ``Entropy`` is a rounded float over the secret's characters. So a
*different* secret at the same path, rule and line is suppressed whenever the
two entropies coincide — and any permutation of the baselined value collides
exactly. Verified against gitleaks 8.24.3: replacing a flagged fixture
credential with a different real one on the same line reported "no leaks
found".

That is exactly the merge-resolution case the content pass exists to catch
("replace the placeholder with the real key"), so keying the exemption on
location is the wrong choice. This keys on the secret's value instead.

Deliberately *not* part of the key
----------------------------------
File and line. A secret that merely moved was not introduced by the change,
and one whose value changed was — wherever it happens to sit. Keying on
location would report every line shift and miss every in-place replacement,
which is backwards on both counts.

Counted as a multiset
---------------------
Occurrences are compared by count, not by set membership. A resolution that
adds a *second* copy of a credential already present in the changed files
leaves the key unchanged, so plain membership would exempt both copies and
report nothing new. Only as many occurrences as existed at the base are
exempt.

Several base-side reports, unioned by location
----------------------------------------------
The caller passes more than one base-side report: the target tip, the deleted
files it could not put in the same tree, and the branch's previous head. Those
are not independent tallies. Location is *not* part of the head-vs-base key —
a secret that merely moved was not introduced — but it is exactly what
distinguishes one occurrence from another *between parents*, so the reports
are reconciled per ``(rule, secret, file)`` — count within a report, take the
largest across reports, then sum over files.

The **file**, not the line. Including the line looked more precise and was
less correct: the same inherited credential routinely sits at different lines
in the target tip and the branch's previous head, and treating those as two
occurrences inflated the exemption enough for a merge resolution to slip a
genuinely new copy past. A line shift is not a second secret.

This is a chosen error direction, not a solved problem. Two parents each
holding one occurrence of a value in one file, with HEAD holding two, is
ambiguous: it is either one inherited occurrence plus one the merge added, or
two inherited occurrences that were always distinct. Nothing in the reports
separates those — they differ only in whether HEAD's lines happen to coincide
with the parents', and a rule keyed on that coincidence reports the *moved*
secret above, which is the reason this comparison is content-keyed at all.
Given the ambiguity the exemption is kept tight, so the failure mode is a
merge push failing on a credential both parents already had, rather than a
merge resolution slipping a real one through. Recovering from the first costs
a glance at a diff-scoped report; the second is what this pass exists to
prevent.

Secrets are never printed. Only the path, line and rule of a new finding are
reported; the reports themselves stay in the caller's temp dirs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def load(path: Path) -> list[dict]:
    """Read a gitleaks JSON report, tolerating the empty-report shapes."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read gitleaks report {path}: {exc}") from exc
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # Refuse rather than treat an unparseable report as "nothing found" —
        # a check that silently passes on a broken input is worse than none.
        raise SystemExit(f"gitleaks report {path} is not valid JSON: {exc}") from exc
    if data is None:
        return []
    if not isinstance(data, list):
        raise SystemExit(f"gitleaks report {path} is not a list of findings")
    return data


def key(finding: dict) -> tuple[str, str]:
    return (str(finding.get("RuleID", "")), str(finding.get("Secret", "")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path, help="gitleaks JSON for the base revision")
    parser.add_argument("head", type=Path, help="gitleaks JSON for HEAD")
    parser.add_argument(
        "--extra-base",
        "--alt-base",
        type=Path,
        action="append",
        default=[],
        dest="extra_base",
        metavar="REPORT",
        help=(
            "another base-side report: the deleted-file tree, or the branch's "
            "previous head. All base-side reports are combined as a union over "
            "(rule, secret, file, line) — see the module docstring for why "
            "neither summing nor maximum is right."
        ),
    )
    args = parser.parse_args(argv)

    reports = [load(args.base)] + [load(extra) for extra in args.extra_base]
    head = load(args.head)

    # Union over (rule, secret, file, line) across every base-side report, then
    # reduce to value counts. Both simpler merges are wrong, in opposite
    # directions, and this PR shipped each of them in turn:
    #
    #   summing   — the target tip and the previous branch head describe the
    #               same paths, so a value both hold once was exempted twice,
    #               and a merge resolution adding a second copy passed.
    #   maximum   — but when the two parents hold that value at *different*
    #               paths, HEAD legitimately inherits both, and taking the
    #               larger single count reported the second as introduced.
    #
    # Deduplicating by location does both jobs: the same occurrence seen in two
    # parents collapses, while genuinely distinct occurrences survive.
    # Per report, count occurrences at each location; across reports keep the
    # largest count seen for that location. Two findings at the *same* location
    # in one report are two occurrences and must both stay exempt, so the merge
    # cannot be a plain set union either.
    merged: Counter = Counter()
    for report in reports:
        for spot, n in Counter(
            (key(f), str(f.get("File", ""))) for f in report
        ).items():
            if n > merged[spot]:
                merged[spot] = n

    remaining: Counter = Counter()
    for (k, _file), n in merged.items():
        remaining[k] += n
    exempt = sum(remaining.values())

    new = []
    for finding in head:
        k = key(finding)
        if remaining[k] > 0:
            remaining[k] -= 1
            continue
        new.append(finding)

    for finding in new:
        print(
            f"::error file={finding.get('File')},line={finding.get('StartLine')}::"
            f"{finding.get('RuleID')}: secret introduced by this change"
        )
    print(
        f"content scan: {len(head)} finding(s) in changed files, "
        f"{exempt} already present at the base, {len(new)} new"
    )
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
