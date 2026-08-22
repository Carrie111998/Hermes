---
name: opensource-contribution
description: "Screen a GitHub issue for duplicate-PR, assignee, and CLA blockers before carrying it to a PR."
version: 0.3.0
author: MershLab
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Issues, Pull-Requests, Screening, MershLab]
    related_skills: [github-issue-to-pr, github-pr-workflow, github-auth, ast-grep]
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

Uses the `contrib_screen` toolset — a native Hermes plugin
(`plugins/contrib-screen/`), not an external CLI. Nothing to install
separately; it ships with this repo.

## When to Use

Before starting work on any GitHub issue as an autonomous contribution
candidate — whether picked from a configured repo list during an
unattended sweep, or named directly by a person.

Don't use for: reviewing an existing PR, or issues already confirmed
CLEAR by a screen run earlier in the same session.

## Procedure

### 1. Run the screen

Call the `contrib_screen` tool:

```
contrib_screen(target="<owner>/<repo>#<issue-number>", signed_orgs=["<org>", ...])
```

`signed_orgs` is optional — pass any orgs whose CLA is already signed so
it stops blocking on them every time. This runs three checks — duplicate
PR (via the issue's cross-reference timeline, broader than a keyword
search), assignee, CLA/DCO — and appends a record of exactly what it
checked and found to `$HERMES_HOME/contrib-screen/log.jsonl`. The tool
result's `verdict` field is the machine-readable outcome
(`clear`/`duplicate`/`assigned`/`cla_required`/`not_found`) — read that
field, don't parse the human-readable `label`.

Done when the verdict is known and recorded.

### 2. Check org-wide, not just this repo

`github-issue-to-pr`'s duplicate sweep and step 1 above both scope to
*this* repo. A large org can have the same symptom already handled in a
sibling repo. If `contrib_screen` returns CLEAR and the target org is
worth checking further (multi-repo orgs, not a single-repo project): run
a live, org-scoped GitHub search (`gh api` or the search API, `org:` +
key symptom terms from the issue) before implementing anything. Only if
that surfaces real candidate repos, `contrib_screen_index` those specific
repos (never the whole org — see this tool's own description for why),
then `contrib_screen_search` for a fuller check. Skip this step entirely
for a single-repo org/project; it exists for the Microsoft/Google/NVIDIA
-scale case.

### 3. Act on the verdict

- **CLEAR** (and step 2 found nothing org-wide) — proceed directly to
  `github-issue-to-pr`'s own procedure, starting at its step 1,
  unmodified. Nothing about that skill changes. Its own step 5 ("search
  sibling call sites for the same bug shape, fix the whole class") is a
  structural question answered with a text/regex search by default —
  load the `ast-grep` skill for that step specifically when the fix
  needs to find the same *pattern*, not the same *string*, across the
  codebase.
- **DUPLICATE** — a PR already references this issue. Read the URL in
  the result (a cross-reference hit can come from a different repo
  entirely, e.g. a changelog mention — treat this as "go look," not an
  automatic skip). If it genuinely covers the same issue, stop, do not
  open a second PR.
- **ASSIGNED** — someone is already on this issue. Stop.
- **CLA_REQUIRED** — the repo requires a CLA not yet signed for that org.
  Stop, unless `signed_orgs` should have applied and didn't (recheck the
  org name).

Done when either work has moved to `github-issue-to-pr`, or the candidate
is skipped with the reason recorded.

### 4. Claim the issue before starting real work

`contrib_screen` catches an *existing* PR; it does not stop two
overlapping runs (an unattended sweep re-firing before the previous
run's PR exists yet, or a founder-triggered run overlapping a scheduled
one) from both picking the same CLEAR issue at the same moment — a real
gap, found by comparing against OpenClaw's own `gh-issues` skill
(`internal-docs/harness/openclaw/skill-survey.md`, private repo).

Call `contrib_screen_claim(target=...)` before proceeding to
`github-issue-to-pr`. This is a real tool (atomic file creation, not a
check-then-write race), not prose to follow by hand — the same
discipline `contrib_screen` itself applies to duplicate/assignee/CLA
checks, extended here so the claim step can't be gotten subtly wrong the
way manual file I/O could. If the result's `claimed` field is `false`,
another run already has this issue (default staleness window: 2 hours,
override with `ttl_hours` if a specific run's normal duration is known to
be longer) — stop, treat it the same as an ASSIGNED verdict. If `true`,
proceed. No explicit cleanup needed on success — staleness alone keeps
this correct.

### 5. Ground the drafted text in this org's real voice

Once implementing (inside `github-issue-to-pr`'s own procedure), if the
org has been indexed (step 2 ran), call `contrib_screen_voice` for a
handful of real merged PR descriptions from this org before writing the
PR body — write in a way consistent with how this org's own contributors
actually write, not generic phrasing. Skip if the org was never indexed;
don't index solely for this, it's a bonus once step 2's data already
exists.

### 6. Record the final outcome

Regardless of which branch above was taken, once `github-issue-to-pr`'s
own step 8 finishes (or this skill stopped at step 3), the outcome —
repo, issue, verdict, and PR URL or the reason nothing was opened — is
what a future sweep or a person checking status needs. `contrib_screen`'s
own log already covers the screening verdict; this step is only about not
losing the *outcome* of what happened after, once the harness has
somewhere durable to write it (see Known limitation below).

## Pitfalls

- Relying on `github-issue-to-pr`'s own duplicate sweep alone — it does
  not check assignee or CLA at all, which is the entire reason this skill
  exists.
- Running `contrib_screen` but proceeding anyway on a non-CLEAR verdict.
- Treating a DUPLICATE verdict as an automatic skip without reading the
  URL in the result — it can be a false positive from an unrelated repo
  mentioning the same issue number.
- Running `contrib_screen_index` against a whole large org "just in
  case" — scope it to real candidate repos, found via a live search
  first.
- Skipping the claim file because `contrib_screen` already said CLEAR —
  CLEAR means no *existing* PR, not that no other run is about to start
  one right now.

## Known limitation

Step 6's durable outcome log has no home yet (`kernel.py`'s append-only
log, per the harness system design doc, is not built) — until it exists,
record the outcome by hand or in the calling session's own notes, don't
skip it silently.

## Verification

- [ ] `contrib_screen` run before any other action on the candidate.
- [ ] For multi-repo orgs, an org-wide check ran before implementing.
- [ ] A claim file was checked and written before `github-issue-to-pr`
      started, not skipped.
- [ ] CLEAR (both repo and org-wide) hands off to `github-issue-to-pr`'s
      full, unmodified procedure.
- [ ] Non-CLEAR (including an unexpired existing claim) stops here — no
      PR opened, reason recorded.
- [ ] Final outcome recorded regardless of which path was taken.
