---
name: opensource-contribution
description: "Screen a GitHub issue for duplicate-PR, assignee, and CLA blockers before carrying it to a PR."
version: 0.1.0
author: MershLab
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Issues, Pull-Requests, Screening, MershLab]
    related_skills: [github-issue-to-pr, github-pr-workflow, github-auth]
---

# Open Source Contribution Gate

A pre-flight gate in front of `github-issue-to-pr`. Run this first on every
candidate issue, before that skill's own procedure starts. It does not
replace `github-issue-to-pr` — it closes a real gap in it: that skill's own
duplicate sweep (its step 2) is a one-off `gh pr list --search`, and checks
nothing else. It does not check whether the issue is already assigned to
someone, and it does not check whether the repo requires a CLA you have
not signed. Both are real, common reasons a finished PR gets closed
unmerged, after the work is already done.

## When to Use

Before starting work on any GitHub issue as an autonomous contribution
candidate — whether picked from a configured repo list during an
unattended sweep, or named directly by a person.

Don't use for: reviewing an existing PR, or issues already confirmed
CLEAR by a screen run earlier in the same session.

## Procedure

### 1. Run the screen

```bash
contrib-screen <owner>/<repo>#<issue-number>
```

This runs three checks — duplicate PR (via the issue's cross-reference
timeline, broader than a keyword search), assignee, CLA/DCO — and appends
a record of exactly what it checked and found to a local, append-only log
(`~/.contrib-screen/log.jsonl`). If a specific org's CLA is already
signed, pass `--signed-org <org>` so it stops blocking on that org every
time. For scripted use, `--json` and the exit code (`0` CLEAR, `1`
otherwise) are both machine-readable — prefer these over parsing the
human-readable line.

Done when the verdict is known and recorded.

### 2. Act on the verdict

- **CLEAR** — proceed directly to `github-issue-to-pr`'s own procedure,
  starting at its step 1, unmodified. Nothing about that skill changes.
- **DUPLICATE** — a PR already references this issue. Read the URL the
  log recorded (a cross-reference hit can come from a different repo
  entirely, e.g. a changelog mention — treat this as "go look," not an
  automatic skip, per `contrib-screen`'s own documented limitation). If
  it genuinely covers the same issue, stop, do not open a second PR.
- **ASSIGNED** — someone is already on this issue. Stop.
- **CLA-blocking** — the repo requires a CLA not yet signed for that org.
  Stop, unless `--signed-org` should have applied and didn't (recheck the
  org name).

Done when either work has moved to `github-issue-to-pr`, or the candidate
is skipped with the reason recorded.

### 3. Record the final outcome

Regardless of which branch above was taken, once `github-issue-to-pr`'s
own step 8 finishes (or this skill stopped at step 2), the outcome —
repo, issue, verdict, and PR URL or the reason nothing was opened — is
what a future sweep or a person checking status needs. `contrib-screen`'s
own log already covers the screening verdict; this step is only about not
losing the *outcome* of what happened after, once the harness has
somewhere durable to write it (see Known limitation below).

## Pitfalls

- Relying on `github-issue-to-pr`'s own duplicate sweep alone — it does
  not check assignee or CLA at all, which is the entire reason this skill
  exists.
- Running `contrib-screen` but proceeding anyway on a non-CLEAR verdict.
- Treating a DUPLICATE verdict as an automatic skip without reading the
  URL the log recorded — it can be a false positive from an unrelated
  repo mentioning the same issue number.

## Known limitation

`contrib-screen` is not yet published anywhere pip-installable (no PyPI
release, no pushed git remote as of this session) — step 1 will fail with
a plain "command not found" until that's fixed, not a screening failure.
Install from a local checkout in the meantime:
`pip install /path/to/contrib-screen`. Step 3's durable outcome log has no
home yet either (`kernel.py`'s append-only log, per the harness system
design doc, is not built) — until it exists, record the outcome by hand
or in the calling session's own notes, don't skip it silently.

## Verification

- [ ] `contrib-screen` run before any other action on the candidate.
- [ ] Verdict recorded (log file, or `--json` output captured).
- [ ] CLEAR hands off to `github-issue-to-pr`'s full, unmodified procedure.
- [ ] Non-CLEAR stops here — no PR opened, reason recorded.
- [ ] Final outcome recorded regardless of which path was taken.
