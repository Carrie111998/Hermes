# contrib-screen (bundled plugin)

Ported in from a separate standalone repo (`contrib-screen`, MIT, never
pushed anywhere public) into `plugins/contrib-screen/` here, on the
founder's explicit call: the harness should be one repo a person installs
once, not several repos they have to clone and wire together separately.

Same checks, same logic, unchanged from the standalone version — this is
a structural move, not a rewrite. What changed: it's now a real Hermes
tool (`contrib_screen`, `contrib_screen_index`, `contrib_screen_search`,
`contrib_screen_voice`) the model calls directly like any other tool, not
a subprocess shelled out to via `terminal`. State moved from
`~/.contrib-screen/` to `$HERMES_HOME/contrib-screen/`, matching every
other bundled plugin's convention (see `plugins/disk-cleanup/`).

`kind: backend` in `plugin.yaml` means this loads automatically — no
`hermes plugins enable` step, no separate install. It ships with the
repo and works the moment Hermes does.

## What each tool does

- `contrib_screen` — screen one issue: duplicate PR, assignee, CLA gate.
  Used by `skills/github/opensource-contribution/SKILL.md` as the
  pre-flight gate in front of Hermes's own `github-issue-to-pr`.
- `contrib_screen_index` — pull an org's issues/PRs/comments into a local
  FTS-searchable SQLite index. Always pass `repos` scoped to real
  candidates — see `internal-docs/harness/org-awareness-and-voice-design.md`
  (private repo) for why indexing a whole large org isn't the plan.
- `contrib_screen_search` — full-text search across an already-indexed
  org. The actual mechanism behind "is this issue already handled
  elsewhere in this org."
- `contrib_screen_voice` — real merged PR text from an indexed org, for
  grounding the model's own drafting in how this specific org actually
  writes. Calibration, not an AI-authorship detector.

## What's still standalone-repo shaped, deliberately

None of this changes `contrib-screen`'s own status as the independently-
ownable, standalone-CLI artifact the 08-19 founding note asked for — that
repo still exists, still works on its own, still meets that bar for
anyone who wants a small tool with no Hermes dependency. This plugin is
the *harness's* copy, folded in for the founder's "one repo" requirement;
it is not a replacement for the standalone tool's own reason for existing.
