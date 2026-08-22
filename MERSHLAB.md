# What's MershLab's, what's upstream

`harness` is a private fork of [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent)
(MIT). Almost everything in this repository is Nous Research's work,
unmodified. This file exists so that's never ambiguous.

## The default, and why it's a default, not a rule

Adding a new file in one of Hermes's own extension points, or in
`mershlab/` when nothing else fits, is the cheap path — it's what kept
the first upstream sync a clean fast-forward across 92 commits, zero
conflicts, the same day this fork was created. That's a real, proven
cost saving, not a policy for its own sake.

**It is not a restriction.** Editing a file Hermes ships is allowed when
the use case genuinely needs it — this is MershLab's fork, not a
read-only mirror. The actual cost of doing so: the next `git fetch
upstream && git merge upstream/main` will hit a real merge conflict on
that file, needing manual resolution, every time upstream touches it
again too. Worth knowing before editing, not a reason to avoid editing —
pick the default when it's free, edit directly when it's actually needed,
and note why here when it happens, so a future sync's conflict has a
reason attached instead of a mystery to re-derive.

## What's MershLab's

- **`skills/github/opensource-contribution/`** — the autonomous
  contribution gate: screen an issue (duplicate PR, assignee, CLA),
  check org-wide before implementing, claim it against concurrent runs,
  hand off to Hermes's own `github-issue-to-pr` unmodified, ground the
  drafted text in the target org's real voice, record the outcome.
- **`plugins/contrib-screen/`** — the tools that skill runs on:
  `contrib_screen`, `contrib_screen_index`, `contrib_screen_search`,
  `contrib_screen_voice`, `contrib_screen_claim`. Ported in from a
  separate standalone repo of the same name (MIT, MershLab), folded into
  this fork so the harness is one repo to install, not several. Every
  tool is live-tested, not just written.
- **`plugins/kernel/`** — the audit invariant: observes every outgoing
  model call via Hermes's real `pre_api_request`/`post_api_request`
  hooks, flags a session whose message history silently shrinks between
  two consecutive calls. Detects and records, loudly; cannot block,
  because Hermes's hooks are observer-only by design. See its own
  `README.md` for exactly what it checks and what it deliberately
  doesn't.
- **`mershlab/`** — anything that isn't a Hermes plugin or skill and
  still needs a home: process supervision, deployment scripts. See its
  own `README.md` for the exact dividing line.
- **This file.**

## What's not MershLab's, and why it's here anyway

Everything else: the agent loop, the provider registry, the memory
system, the gateway, the scheduler, hundreds of other skills, `iron-proxy`
(the egress credential firewall), the desktop/web/TUI surfaces. All real
Nous Research engineering, used because rebuilding it would be worse
than building on it — the reasoning is recorded in full in MershLab's own
private research (`internal-docs/harness/`, not part of this repo, not
public), not repeated here.

## Why a fork, not a small standalone tool

An earlier plan for this project was a small, independent CLI, decoupled
from any single AI vendor. That plan is not what got built — `harness`
runs on Hermes's full runtime, by explicit choice, because the alternative
was rebuilding a large fraction of it from scratch. The one piece that
still stands alone on its own is `contrib-screen`'s original repo (kept
separately, unpushed, MIT) — its screening logic is duplicated here as a
native plugin, not replaced.

## Status

Design and research live in MershLab's own private `internal-docs`
repository, not here — this file states facts about what's in this repo,
not the reasoning behind them.
