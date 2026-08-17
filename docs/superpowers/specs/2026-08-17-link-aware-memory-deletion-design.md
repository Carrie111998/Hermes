# Link-aware memory deletion — design

Date: 2026-08-17
Status: approved (Diego, 2026-08-17), pending implementation plan
Scope: `~/.claude/hooks/`, `~/.claude/settings.json`
Related: `C:\Users\diego\memory-link-lint.ps1` (in `~/.homeops.git`), MemPalace drawers
`claude-code/memory-dead-link-repair-2026-08-17`, `claude-code/memory-dead-link-final-resolution-2026-08-17`

## Problem

The `~/.claude` memory-consolidation pass deletes memory files without resolving the
wikilinks that point at them. Deletion is content-safe — the facts are merged into a
survivor — but every inbound link to the deleted filename is silently orphaned.

Ground truth: commit `486258b` in the `~/.claude` repo (auto-snapshot, 239 files, bot
identity, `2026-08-15 01:47:09Z`) removed 118 files from
`projects/C--Users-diego--hermes-agent-src/memory/` (0 added, 121 modified). The merge
was correct. Six inbound wikilinks were orphaned. Five were repaired by hand on
2026-08-17; the sixth, a cross-root reference from the `hermes` root, was resolved the
same day by inlining the fact into the referrer.

Detection today is after-the-fact and manual: `memory-link-lint.ps1` must be run by a
human who thinks to run it. Nothing in the write path is link-aware.

## Verified current state

Measured 2026-08-17, not assumed:

| Fact | Value |
|---|---|
| Full lint | 25.4s, 10 versioned roots, 1153 files, **DEAD = 0** |
| Non-DEAD categories | NEARMISS 2, CROSSROOT 15, RENAMED 31, SUFFIX 26, CONVENTION 75, GBRAIN 7, PATH 2, PROSE 65 |
| Cross-root reverse-link index (Python) | **2.2s**, 1354 files, 1333 distinct targets |

The two file counts differ deliberately and are not in conflict: the linter scans the
**10 versioned** roots (1153 files), while the index measurement above walked all **14**
memory dirs present on disk, including the four deliberately unversioned ones. The
production module derives its roots exactly as the linter does, so it will scan the 10 —
making 2.2s an over-estimate of the real cost, not an under-estimate.
| `~/.claude` git hooks | none; no `.pre-commit-config.yaml` |
| `~/.claude` commit authorship | every commit is an automated `SessionEnd` snapshot from `hooks/auto-commit-claude-memory.py` (`git add -A`, bot identity, contract *"exit 0 ALWAYS. Never block session end."*) |
| `consolidate-memory` skill | vendor-bundled; **not** in `~/.claude/skills` (17 user skills, none by that name) and not in the plugin cache (only `superpowers`). Not editable. |
| `~/.claude/docs/**` | ignored by the repo's `/*` allowlist — a spec cannot live there |
| `~/.claude/hooks/**` | allowlisted; new hooks version cleanly |

Two consequences drove the design:

1. **The cross-root oracle already exists.** `memory-link-lint.ps1` indexes all ten
   roots and classifies `CROSSROOT` via `Find-OtherRootsWithTarget`. The brief's "hard
   case" — a pass inside one root cannot see referrers in another — is not a missing
   capability. It is a capability consulted at the wrong time. The design therefore
   moves *when* the whole-corpus index is queried, rather than trying to teach the
   consolidation pass to see sideways.

2. **Refusing the snapshot is the wrong lever.** The brief's option 2 proposed a
   pre-commit gate refusing a commit that raises the DEAD count. In this repo the only
   commits are automated backups written at `SessionEnd`, when no agent remains to act
   on a refusal — and the next session's `git add -A` would sweep the same deletion in
   regardless. A refusal would cost the backup and defer the problem by one session.
   **Layer 3 below reports; it never refuses.**

### A correctness trap in the obvious formulation

"Refuse a commit that increases the DEAD count" is not merely the wrong lever, it is an
unsound detector. `Resolve-Cause` in the linter tries `SUFFIX`, `CONVENTION`, `RENAMED`
and `NEARMISS` before `DEAD`. A link to a deleted name can therefore re-resolve onto a
*different surviving file* and be reported in a clean non-DEAD category — a link now
pointing at the wrong memory, with the DEAD count unmoved.

The gate must key on **"this file is going away and things point at it,"** never on a
delta in the DEAD count.

## Decisions

| Question | Decision |
|---|---|
| Where does link-awareness bite? | Live `PreToolUse` gate that **blocks**, plus a snapshot-time backstop. Never block the snapshot itself. |
| Cross-root referrers | Block. **Inline the fact by default** (the 2026-08-17 precedent). Repointing is permitted when the fact is too large to inline, accepting the `DEAD → CROSSROOT` trade, but the agent must say so. |
| Escape hatch | Self-clearing by default; referrers deleted by the **same command** are auto-excluded; an explicit, logged override marker exists for genuine deadlocks. |

## Non-goals

- **Do not normalize the non-DEAD categories.** `CONVENTION`, `RENAMED`, `SUFFIX`,
  `PROSE`, `CROSSROOT` are expected and deliberate — Diego's 2026-08-10 lint-only call.
- **Do not add auto-fixing to the linter.** Same call. The linter stays read-only.
- **Do not refuse the `SessionEnd` snapshot** under any condition.
- **Do not edit the vendor `consolidate-memory` skill** (it is not on disk).
- No containment guarantee. This is a guardrail against an accident, not a boundary
  against a determined caller — the same philosophy `block-unscoped-process-kill.py`
  states about its own escape marker.

## Architecture

Four pieces: one shared module and three consumers, each seeing something the others
structurally cannot.

### `hooks/memory_links.py` — the shared module

The only unit that knows what a link is. Everything else asks it questions.

- **Root derivation.** Roots come from `git check-ignore --no-index` against the
  `~/.claude` `.gitignore` negation blocks — the same source the linter uses. Never
  hardcoded: the linter's own history records that a pinned two-root list was blind to
  eight of the ten roots, including the one every `cwd=~` session writes.
- **Identity.** A file is identified by its **basename and** its frontmatter `name:`
  slug. Links are written both ways, and in the `~` root 102 of 144 files carry a kebab
  `name:` over an underscore filename — basename-only indexing would misread most of it.
- **Normalization.** Strip a trailing `.md`, collapse `[\s\-_]+` to a single separator,
  casefold. Mirrors `Get-Variants`.
- **Exclusions.** Fenced blocks (``` / ~~~, up to 3 leading spaces) and inline backtick
  code spans are not links. Without this the gate blocks on prose.
- **Interface.** `referrers_of(paths) -> {path: [Referrer(root, file, line, kind)]}`.
- **Budget.** A wall-clock deadline (default 6s, env-overridable). On overrun the query
  is abandoned and the caller allows, with a log line.

Measured cost of a full index build: 2.2s, paid only when a deletion is actually in play.

### Layer 1 — `hooks/block-memory-file-orphan.py` (`PreToolUse`, `Bash|PowerShell`)

Blocks a deletion that would orphan a live link, while the agent still holds the merge
context and repair is free.

- A `RULES` table of deletion shapes: `rm`, `Remove-Item`/`ri`/`del`/`erase`, `git rm`,
  `Move-Item`/`mv` whose **source is under a memory root and whose destination is not**
  (a move within a root is a rename, which Layer 2 reports rather than Layer 1 blocking),
  and the common Python spellings (`os.remove`, `os.unlink`, `Path.unlink`,
  `shutil.move`). The whole verdict lives in the table so
  the suite can empty it and prove the hook is not passing vacuously — the idiom already
  used by `block-unscoped-process-kill.py`.
- Extract candidate paths under a derived memory root. Resolve referrers. **Drop
  referrers that this same command also deletes** — that kills the mutual-reference
  false positive without any state.
- Any survivor → `exit 2`, stderr to the model: each referrer as `root|file:line`, the
  inline-by-default rule for cross-root referrers, and the override marker.
- Self-clearing: repoint or inline, re-issue the identical command, it passes.
- Override marker `memory-orphan: approved`, mirroring the existing
  `cross-session-kill: approved` convention rather than inventing a second one. Use is
  logged, and Layer 3 still reports any orphan the override produced.

### Layer 2 — `hooks/detect-memory-file-orphan.py` (`PostToolUse`, `Bash|PowerShell`)

Matched to the shell tools only: they are the sole route by which a file can disappear.
`Edit`/`Write`/`MultiEdit` can empty a file but never unlink one, so matching them would
add cost on the hottest tool path and detect nothing.

Catches deletions by any route Layer 1's patterns miss — a glob, a variable-expanded
path, an unrecognized verb.

- Maintains a rolling inventory of memory-root filenames in a per-session state file
  keyed by `session_id`, pruned on age so concurrent sessions never report each other's
  deletions.
- Any name that vanished since the last observation triggers a referrer query over the
  survivors; live referrers are surfaced via `systemMessage`.
- Steady-state cost is one directory listing (~1354 names). The 2.2s index is built only
  when something actually disappeared.
- No `Pre`/`Post` pairing to coordinate — the inventory is self-maintaining.
- A rename is not a false positive here: a renamed file breaks inbound links exactly as
  a deleted one does, so reporting it is correct.

This layer is what converts the failure mode from "discovered weeks later" to
"discovered seconds later, by the agent that caused it."

### Layer 3 — orphan report inside `hooks/auto-commit-claude-memory.py` (`SessionEnd`)

The only layer that sees deletions made by **other agents** — Codex, crons — between
sessions. Those never pass through any of this session's tool calls; the existing hook
header notes that `git add -A` sweeps them in.

- Runs **after** the commit succeeds, reading `git show --name-status HEAD`, never
  `git diff --cached` before it. That ordering makes it structurally impossible for a
  bug in the reporter to cost a snapshot: the `exit 0 ALWAYS` contract holds by
  construction rather than by care.
- For `D` entries under a memory root, query referrers among survivors. Any hit writes
  `~/.claude/logs/memory-orphans-<timestamp>.md` and logs a one-line summary.
- Never blocks, never fails the commit, never rewrites history.

### Instruction injection

`hooks/memory-index-size-guard.py` already emits the message that summons the pass
("invoke the /consolidate-memory skill …"). Extend that message with the link-safety
rule: before deleting a merged file, resolve its inbound links and repoint them to the
merge target, or inline the fact. This delivers the brief's option 1 intent at the exact
moment the pass begins, with no vendor skill to edit and no `~/CLAUDE.md` growth.

## Data flow — `486258b` replayed

1. The pass merges 118 `hermes-agent-src` files into survivors and issues
   `Remove-Item ...\memory\a.md, ...\memory\b.md`.
2. **Layer 1** matches `Remove-Item`, extracts the paths, builds the index (2.2s), and
   resolves referrers. Referrers inside this command's own delete set drop out. Six
   survive — five in `hermes-agent-src`, one in the `hermes` root. `exit 2` names all
   six and states the inline-by-default rule for the cross-root one.
3. The agent repoints the five, inlines the sixth, re-issues the identical command. The
   index rebuilds, finds no live referrers, `exit 0`.
4. **Layer 2** sees 118 names gone, queries, finds nothing live, stays silent. Had the
   deletion arrived by an unmatched route, this is where the six surface.
5. **Layer 3** commits, re-queries `HEAD`'s `D` entries, finds nothing, leaves the
   commit message unchanged.

## Error handling

Everything fails open except the deliberate block.

- Index build throws, a root is unreadable, git is missing, the event JSON is malformed
  → `exit 0`, log, allow. A link linter must never become the thing that stops memory
  work.
- **Hard internal deadline, not just the harness timeout.** This box has documented
  saturation storms where a seconds-scale probe takes minutes. `memory_links` enforces
  its own wall-clock budget and abandons the query on overrun. Without it the gate
  becomes a hang under exactly the pressure that makes people kill things.
- Layer 1 biases to false negatives: unparseable commands, variable-expanded paths and
  unrecognized verbs all allow.
- Encoding: files are read as UTF-8 with `errors="replace"`; BOM-less UTF-8 and stray
  NUL bytes must not abort a scan.
- Hook timeouts in `settings.json`: Layer 1 and Layer 2 get 15s (2.2s index + ~0.3s
  interpreter start, with headroom under load). Layer 3's existing 20s stands, since its
  query runs only when `D` entries exist and is bounded by the module's own deadline.

## Testing

Pytest beside the hooks, matching the existing `test_block_unscoped_process_kill.py` and
`test_mcp_registration_check.py` precedent. **Every test is written red first** — on this
box a fresh test that passes before the fix means a vacuous assert or a concurrent
writer, not success.

1. **Table-emptying** — with `RULES` emptied, nothing blocks. Proves the table drives the
   verdict.
2. **`486258b` regression** — fixture corpus with same-root and cross-root referrers;
   assert the block fires *and* that the message names each referrer.
3. Same-command referrer exclusion → allowed.
4. Override marker → allowed, and the use is logged.
5. **Frontmatter-slug identity** — a file referenced by its `name:` slug rather than its
   basename still blocks.
6. **Fenced and backticked mentions are not referrers** — no block on prose.
7. Fail-open: unreadable root, missing git, malformed event, deadline exceeded →
   `exit 0`.
8. Non-deletion commands are never blocked, over a corpus of real commands.
9. **Conformance** — `memory_links` and `memory-link-lint.ps1` agree on resolution over
   the same corpus, so the gate cannot drift from the linter that adjudicates its result.
10. Layer 3: a reporter that raises still leaves the commit at `HEAD`.
11. Layer 2: per-session state isolation; a vanished name is reported once, not on every
    subsequent tool call.

**Acceptance.** After the change, a full
`powershell -NoProfile -File C:\Users\diego\memory-link-lint.ps1` still reports
**DEAD 0**, and the non-DEAD category counts are unchanged from the table above. A
changed non-DEAD count means something normalized links that were deliberately left
alone.

## Rollout

1. Land `memory_links.py` with its own tests first — it is the only unit with real logic.
2. Land Layer 1, then Layer 2, then the Layer 3 report; register each in
   `settings.json` as it lands.
3. Extend the `memory-index-size-guard.py` message last, once the mechanism it describes
   actually exists.
4. Commit to `~/.claude` per `~/.hermes/ops/COMMITTING.md`: stage explicit paths, commit
   bare, never `--no-verify`, never `git commit -- <paths>`, never `git add -A`.

This spec lives in `~/.hermes/agent-src` because `~/.claude/docs/**` is ignored by that
repo's `/*` allowlist — the same trap recorded in the linter's own header, where
`git add -f` was explicitly rejected as the fix.
