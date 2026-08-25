# Context rule disposition ledger — root `AGENTS.md`

This is the tracked disposition evidence for the context-file budget cleanup.

Every level-1/2/3 section of the pre-cleanup root `AGENTS.md` is enumerated
below exactly once, with its source line range, its Unicode character count,
and its disposition. The table was written before the cleanup and corrected
against the pre-cleanup Git blobs after implementation.

## Baseline measurements

| Artifact | Unicode chars | Lines |
|---|---:|---:|
| `AGENTS.md` (before) | 95,154 | 1,784 |
| `apps/desktop/AGENTS.md` (before) | 11,195 | 210 |
| `AGENTS.md` (after) | 27,883 | 489 |
| `apps/desktop/AGENTS.md` (after) | 7,307 | 133 |

These are decoded Unicode-character and logical-line counts from the Git blobs
and final files. The draft's 1,785/211 line figures were off by one; both
`splitlines()` and `awk` report 1,784/210 for the newline-terminated base blobs.

Startup budget in force: explicit `context_file_max_chars: 40000` (it overrides
the dynamic cap). Nested progressive-hint budget: 8,000 per file. Measured with
`python scripts/check_context_file_limits.py --json .`:

```
startup   95,154  AGENTS.md                                    -> over the 40,000 cap
nested    11,195  apps/desktop/AGENTS.md                        -> over the 8,000 cap
nested    20,904  skills/creative/popular-web-designs/templates/claude.md  -> over the 8,000 cap
```

Section ranges below sum to exactly 95,154 characters — the whole file — so the
enumeration is complete and non-overlapping by construction.

## Disposition vocabulary

| Verdict | Meaning |
|---|---|
| `KEEP_ROOT` | Stays in the root guide, possibly compressed. Never weakened. |
| `MOVE_NESTED` | Moves to a scoped `AGENTS.md` closer to the code it governs. |
| `LINK_CANONICAL_DOC` | Content already has (or belongs in) a canonical doc or source file; root keeps a pointer where needed for discoverability or a named contract. |
| `MOVE_SKILL` | Moves into a skill. **Not used** — see "Archaeology" below. |
| `DELETE_DUPLICATE` | Restates a rule stated elsewhere in the same file. |
| `DELETE_STALE` | An inventory, count, or version literal whose canonical source is the tree itself. |

## Ledger

| Lines | Chars | Lvl | Section | Verdict | Rationale / destination |
|---|---:|---|---|---|---|
| 1-6 | 170 | # | Hermes Agent - Development Guide | `KEEP_ROOT` | Root title + "never give up on the right solution". Kept verbatim as the file's opening. |
| 7-28 | 1,123 | ## | What Hermes Is | `KEEP_ROOT` | Root outline §1. What Hermes is + the two governing design properties (caching, narrow waist). Load-bearing lens for every review. |
| 29-51 | 1,311 | ## | Contribution Rubric — What We Want / What We Don't | `KEEP_ROOT` | Root outline §2. Intent layer + the sweeper's when-NOT-to-close instruction. Not derivable from code. |
| 52-95 | 2,798 | ### | What we want | `KEEP_ROOT` | Root outline §2. Merge criteria. Kept whole; only prose tightened. |
| 96-137 | 2,770 | ### | What we don't want (rejected even when well-built) | `KEEP_ROOT` | Root outline §2. Rejection criteria, incl. .env-vs-config.yaml, telemetry opt-in gate, third-party-plugin policy. Kept whole. |
| 138-181 | 2,850 | ### | Before you call it a bug — verify the premise (and when NOT to close) | `KEEP_ROOT` | Root outline §2. Bug-premise and original-intent verification. Explicitly listed as must-preserve in the plan. |
| 182-212 | 1,918 | ### | The Footprint Ladder (new capability decision) | `KEEP_ROOT` | Root outline §3. The Footprint Ladder — the narrow-waist decision procedure. |
| 213-251 | 2,236 | ### | Surface capability is a property of the SESSION, never of the process env | `KEEP_ROOT` | Root outline §3. Session-scoped capability rule. `apps/desktop/AGENTS.md` cites this heading by name, so the heading text is preserved. |
| 252-262 | 324 | ## | Development Environment | `KEEP_ROOT` | Root outline §4. Minimal bootstrap (venv probe order). |
| 263-315 | 3,489 | ## | Project Structure | `LINK_CANONICAL_DOC` | Tree inventory drifts constantly. Root keeps ~8 load-bearing entry points; full tree -> `website/docs/developer-guide/architecture.md` (Directory Structure). |
| 316-338 | 1,420 | ## | TypeScript Style | `KEEP_ROOT` | Compressed. No canonical TS-style doc exists and the rules span `apps/`, `ui-tui/`, `web/`, `website/` — no single directory could own a nested file. Kept as a short bullet list. |
| 339-352 | 305 | ## | File Dependency Chain | `LINK_CANONICAL_DOC` | Duplicated verbatim by `architecture.md` (File Dependency Chain). Root keeps a pointer. |
| 353-388 | 1,579 | ## | AIAgent Class (run_agent.py) | `LINK_CANONICAL_DOC` | Signature sketch of a ~60-parameter constructor — stale by construction. Canonical: `run_agent.py` + `website/docs/developer-guide/agent-loop.md`. |
| 389-412 | 858 | ### | Agent Loop | `LINK_CANONICAL_DOC` | Pseudocode of the real loop. Canonical: `agent-loop.md` (Turn Lifecycle). |
| 413-421 | 824 | ## | CLI Architecture (cli.py) | `LINK_CANONICAL_DOC` | CLI component inventory. Canonical: `cli.py` + `website/docs/user-guide/cli.md`. |
| 422-433 | 781 | ### | Slash Command Registry (`hermes_cli/commands.py`) | `LINK_CANONICAL_DOC` | Registry consumer list, derivable from `hermes_cli/commands.py`. |
| 434-466 | 1,663 | ### | Adding a Slash Command | `KEEP_ROOT` | Compressed to the 3-step contract. `website/docs/developer-guide/extending-the-cli.md` covers wrapper CLIs and TUI widgets, NOT the slash-command registry — no canonical home exists, so dropping it would lose the rule. |
| 467-470 | 161 | ## | TUI Architecture (ui-tui + tui_gateway) | `LINK_CANONICAL_DOC` | Canonical: `website/docs/user-guide/tui.md` + `tui_gateway/server.py`. |
| 471-481 | 325 | ### | Process Model | `LINK_CANONICAL_DOC` | Process diagram. Canonical: `tui.md`. |
| 482-485 | 157 | ### | Transport | `LINK_CANONICAL_DOC` | Transport one-liner. Canonical: `tui_gateway/server.py` method/event catalog. |
| 486-498 | 753 | ### | Key Surfaces | `DELETE_STALE` | Component/method inventory table that drifts with every Ink refactor. Canonical: the source tree. |
| 499-503 | 258 | ### | Slash Command Flow | `LINK_CANONICAL_DOC` | Dispatch order, derivable from `app.tsx` + `_SlashWorker`. |
| 504-517 | 353 | ### | Dev Commands | `DELETE_STALE` | npm script list — canonical source is `ui-tui/package.json`. |
| 518-530 | 1,745 | ### | TUI in the Dashboard (`hermes dashboard` → `/chat`) | `KEEP_ROOT` | Compressed to the rule only: do not re-implement the primary chat experience in React; structured React around the TUI is allowed. Mechanics (xterm, PTY, resize frames) link to `hermes_cli/pty_bridge.py`. |
| 531-547 | 4,427 | ### | Electron Desktop Chat App (`apps/desktop/`) | `MOVE_NESTED` | Desktop-scoped architecture + the slash-palette curation rule move to `apps/desktop/AGENTS.md`. Root keeps a one-line pointer. Largest single reduction (4,427 chars). |
| 548-597 | 2,683 | ## | Adding New Tools | `KEEP_ROOT` | Compressed, heading preserved verbatim: `CONTRIBUTING.md:399` points at "AGENTS.md (section **Adding New Tools**)" and CONTRIBUTING.md is out of scope for this change. Kept: plugin-route-first, the 2-file requirement, toolset wiring is manual, handlers return JSON strings. Removed: the duplicated profile-path paragraphs (see 1383). |
| 598-620 | 972 | ## | Dependency Pinning Policy | `KEEP_ROOT` | Supply-chain invariant. Compressed to the four pinning rules; the worked example table collapses to one line. |
| 621-622 | 25 | ## | Adding Configuration | `KEEP_ROOT` | Heading folds into the hard-invariants section. |
| 623-630 | 399 | ### | config.yaml options: | `KEEP_ROOT` | The `_config_version`-bump rule is behavioral (when a bump IS and IS NOT required). Kept. |
| 631-646 | 740 | ### | Top-level `config.yaml` sections (non-exhaustive): | `DELETE_STALE` | Self-declared non-exhaustive section inventory. Canonical: `DEFAULT_CONFIG` in `hermes_cli/config.py` + `website/docs/user-guide/configuration.md`. |
| 647-663 | 650 | ### | .env variables (SECRETS ONLY — API keys, tokens, passwords): | `KEEP_ROOT` | Secrets-vs-settings invariant — explicitly listed as must-preserve. Kept; the `OPTIONAL_ENV_VARS` metadata snippet collapses to a pointer. |
| 664-674 | 600 | ### | Config loaders (three paths — know which one you're in): | `KEEP_ROOT` | Compressed to two lines. The "CLI sees the key, gateway doesn't" failure mode is a real, repeated defect and has no canonical doc. |
| 675-684 | 435 | ### | Working directory: | `KEEP_ROOT` | Workdir invariant (`terminal.cwd` -> `TERMINAL_CWD`; `MESSAGING_CWD` removed). Part of the workdir/session-isolation invariant set. |
| 685-688 | 184 | ## | Skin/Theme System | `LINK_CANONICAL_DOC` | Canonical: `website/docs/user-guide/features/skins.md`. |
| 689-701 | 578 | ### | Architecture | `LINK_CANONICAL_DOC` | Skin-engine function inventory. Canonical: `hermes_cli/skin_engine.py` + skins.md. |
| 702-722 | 1,080 | ### | What skins customize | `DELETE_STALE` | 20-row skin-key table. Canonical: `SkinConfig` + skins.md. |
| 723-729 | 231 | ### | Built-in skins | `DELETE_STALE` | Built-in skin inventory — changes whenever a skin ships. |
| 730-744 | 280 | ### | Adding a built-in skin | `LINK_CANONICAL_DOC` | Procedure. Canonical: skins.md. |
| 745-773 | 494 | ### | User skins (YAML) | `LINK_CANONICAL_DOC` | User YAML example. Canonical: skins.md. |
| 774-779 | 212 | ## | Plugins | `KEEP_ROOT` | Compressed into the extension-routing section (§6): two plugin surfaces, both under `plugins/`. |
| 780-824 | 2,414 | ### | General plugins (`hermes_cli/plugins.py` + `plugins/<name>/`) | `LINK_CANONICAL_DOC` | Canonical: `website/docs/developer-guide/plugins/index.md` (incl. the native compatibility contract already cited by anchor). Root keeps one line: `discover_plugins()` only runs as a side effect of importing `model_tools.py`. |
| 825-886 | 3,593 | ### | Memory-provider plugins (`plugins/memory/<name>/`) | `LINK_CANONICAL_DOC` | Canonical: `website/docs/developer-guide/memory-provider-plugin.md`. Root keeps one line for the closed in-tree provider set; the "plugins must not modify core files" and third-party-product rules are already stated at 96 (DELETE_DUPLICATE for those paragraphs). |
| 887-911 | 1,168 | ### | Model-provider plugins (`plugins/model-providers/<name>/`) | `LINK_CANONICAL_DOC` | The section already ends with "Full authoring guide: website/docs/developer-guide/model-provider-plugin.md". |
| 912-922 | 558 | ### | Dashboard / context-engine / image-gen plugin directories | `LINK_CANONICAL_DOC` | Directory inventory. Canonical: the `plugins/` tree + the `hermes-example-plugins` companion repo link, which moves into the root pointer. |
| 923-990 | 3,843 | ### | Bot Mode (`apps/desktop/src/plugins/hermes-bots/`) | `MOVE_NESTED` | Desktop-scoped forever-chat contract moves to `apps/desktop/AGENTS.md`: exact hidden-title registry lookup, fail-closed lookup errors, lineage-tip selection, adopt-before-mint creation, and bans on session-ID pins, recency/visibility selection, and per-bot browsers. Regression tests remain the authority. |
| 991-1007 | 757 | ## | Skills | `KEEP_ROOT` | Compressed: the `skills/` vs `optional-skills/` routing rule is a review decision. Inventory of categories links to `website/docs/developer-guide/creating-skills.md`. |
| 1008-1019 | 503 | ### | SKILL.md frontmatter | `LINK_CANONICAL_DOC` | Frontmatter field list. Canonical: creating-skills.md (SKILL.md Format). |
| 1020-1101 | 3,984 | ### | Skill authoring standards (HARDLINE) | `KEEP_ROOT` | Heading preserved verbatim. `skills/software-development/hermes-agent-skill-authoring/SKILL.md` names this section as its source of truth, and `creating-skills.md` documents structure but NOT these merge-blocking standards. Compressed 3,984 -> ~1,300 by dropping the inline verification snippet and the worked examples. |
| 1102-1120 | 759 | ## | Toolsets | `LINK_CANONICAL_DOC` | Toolset-key inventory drifts. Canonical: `toolsets.py` + `website/docs/reference/toolsets-reference.md`. |
| 1121-1155 | 1,458 | ## | Delegation (`delegate_task`) | `LINK_CANONICAL_DOC` | Canonical: `website/docs/user-guide/features/delegation.md`. Root keeps the durability rule (background delegation is process-local; use `cronjob` / `terminal(background, notify_on_complete)` for restart-surviving work). |
| 1156-1189 | 1,423 | ## | Curator (skill lifecycle) | `LINK_CANONICAL_DOC` | Section already ends with "Full user-facing docs: website/docs/user-guide/features/curator.md". |
| 1190-1225 | 1,526 | ## | Cron (scheduled jobs) | `LINK_CANONICAL_DOC` | Canonical: `website/docs/user-guide/features/cron.md`. Root keeps one line: cron deliveries are never mirrored into the target gateway session (message-role alternation). |
| 1226-1269 | 2,153 | ## | Kanban (multi-agent work queue) | `LINK_CANONICAL_DOC` | Section already ends with "Full user-facing docs: website/docs/user-guide/features/kanban.md". |
| 1270-1326 | 3,219 | ## | Update Pipeline (`hermes update`) | `KEEP_ROOT` | Compressed 3,219 -> ~900. No canonical developer doc exists. Kept: the six-stage shape, "a PR that weakens a stage answers for the failure class it guards", the never-partial-snapshot rule, fleet-wide restart, and the stale-gateway verify gate. Per-stage incident narration removed. |
| 1327-1342 | 778 | ### | Gateway lifecycle vs. the Desktop app | `KEEP_ROOT` | Behavioral: `hermes serve` dies with the app, the messaging gateway survives it. Kept as two lines with the two do-not-"fix" directives. |
| 1343-1344 | 23 | ## | Important Policies | `KEEP_ROOT` | Heading becomes root outline §7 (hard invariants). |
| 1345-1358 | 676 | ### | Prompt Caching Must Not Break | `KEEP_ROOT` | Prompt-caching invariant — explicitly listed as must-preserve, incl. the cache-aware slash-command rule. |
| 1359-1373 | 692 | ### | Background Process Notifications (Gateway) | `LINK_CANONICAL_DOC` | Config-value documentation for `display.background_process_notifications`. Canonical: `website/docs/user-guide/configuration.md`. |
| 1374-1382 | 404 | ## | Profiles: Multi-Instance Support | `KEEP_ROOT` | Profiles invariant — explicitly listed as must-preserve. |
| 1383-1453 | 3,830 | ### | Rules for profile-safe code | `KEEP_ROOT` | All seven profile-safe rules are load-bearing. Compressed 3,830 -> ~1,700 by replacing GOOD/BAD code blocks with one-line statements; rule 7 (fail-closed scoped secret reads) keeps its full "never fall through to os.environ" wording. |
| 1454-1455 | 19 | ## | Known Pitfalls | `KEEP_ROOT` | Heading kept inside §7. |
| 1456-1476 | 1,236 | ### | DO NOT infer process identity from argv substrings | `KEEP_ROOT` | Compressed. Argv-substring identity inference is the largest single bug class cited in the file; the canonical matchers and the derive-flags-from-the-parser rule are kept. |
| 1477-1481 | 307 | ### | DO NOT hardcode `~/.hermes` paths | `DELETE_DUPLICATE` | Restates profile-safe rules 1 and 2 (1383) verbatim in intent. |
| 1482-1484 | 146 | ### | All CLI menu-pickers MUST use curses. | `KEEP_ROOT` | 146 chars; kept as-is. |
| 1485-1487 | 183 | ### | DO NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code | `KEEP_ROOT` | 183 chars; kept as-is. |
| 1488-1490 | 281 | ### | `_last_resolved_tool_names` is a process-global in `model_tools.py` | `KEEP_ROOT` | 281 chars; kept as-is. |
| 1491-1493 | 513 | ### | DO NOT hardcode cross-tool references in schema descriptions | `KEEP_ROOT` | Compressed to two lines; the dynamic-cross-reference escape hatch is retained. |
| 1494-1504 | 670 | ### | The gateway has TWO message guards — both must bypass approval/control commands | `KEEP_ROOT` | Compressed. "A new control command must bypass BOTH guards" is the rule; the guard locations stay as file pointers. |
| 1505-1545 | 2,668 | ### | Streaming delivery contract (stream-is-the-message adapters) — duplicate-final class | `KEEP_ROOT` | Compressed 2,668 -> ~1,000. All four invariants retained as one line each plus the contract-test pointer; the live-incident ledger and Slack API ground truth are dropped (they are already encoded in the connector comments and `tests/gateway/test_stream_final_contract.py`). |
| 1546-1553 | 442 | ### | Squash merges from stale branches silently revert recent fixes | `KEEP_ROOT` | Merge-safety rule. Compressed to three lines. |
| 1554-1558 | 261 | ### | Don't wire in dead code without E2E validation | `KEEP_ROOT` | Testing invariant — explicitly listed as must-preserve (real-path E2E). |
| 1559-1576 | 662 | ### | Tests must not write to `~/.hermes/` | `KEEP_ROOT` | Hermetic-test invariant. Compressed: the `profile_env` fixture body collapses to a pointer at `tests/hermes_cli/test_profiles.py`. |
| 1577-1578 | 12 | ## | Testing | `KEEP_ROOT` | Heading becomes root outline §8 (testing contract). |
| 1579-1615 | 2,246 | ### | Python | `KEEP_ROOT` | `scripts/run_tests.sh` mandate + flake policy. Compressed: the without/with-wrapper comparison table collapses to one sentence. |
| 1616-1627 | 659 | ### | Where to place what tests | `KEEP_ROOT` | CI-classifier placement rule — a JS-asserting Python test silently never runs on the PR that would break it. Kept. |
| 1628-1681 | 2,843 | ### | Don't fake the host OS | `KEEP_ROOT` | Native OS test lanes — explicitly listed as must-preserve. Compressed ~2,843 -> ~1,100; the marker-not-skipif trap and the `wine2e` lane are retained. |
| 1682-1730 | 1,787 | ### | Don't write change-detector tests | `KEEP_ROOT` | No change-detector tests — explicitly listed as must-preserve. Compressed by cutting the do/don't code blocks to one example each. |
| 1731-1784 | 2,297 | ### | Never read source code in tests | `KEEP_ROOT` | No source-reading tests — explicitly listed as must-preserve. Compressed by cutting the TS worked example to a one-line rule. |

## Totals

| Verdict | Sections |
|---|---:|
| `KEEP_ROOT` | 45 |
| `LINK_CANONICAL_DOC` | 25 |
| `DELETE_STALE` | 5 |
| `MOVE_NESTED` | 2 |
| `DELETE_DUPLICATE` | 1 |
| `MOVE_SKILL` | 0 |
| **Total** | **78** |

Verification: 78 headings enumerated, 78 rows, and the section character counts
sum to 95,154 — the full file. No heading appears twice; none is missing.

## Archaeology (`git log -p -S`)

The plan requires intent archaeology only when a behavioral or safety rule is
being **deleted or weakened**. This ledger deletes six sections, and every one
of them is an inventory, a count, or a restatement:

| Lines | Section | Why no archaeology |
|---|---|---|
| 486-498 | TUI Key Surfaces | Component/method table; canonical source is the Ink tree. |
| 504-517 | TUI Dev Commands | npm script list; canonical source is `ui-tui/package.json`. |
| 631-646 | Top-level `config.yaml` sections | Self-declared "non-exhaustive" inventory. |
| 702-722 | What skins customize | Skin-key table. |
| 723-729 | Built-in skins | Skin inventory. |
| 1477-1481 | DO NOT hardcode `~/.hermes` paths | Restates profile-safe rules 1-2 (lines 1383-1453) with no added constraint. |

No behavioral, safety, contribution, caching, profile, or testing rule is
deleted or weakened by this ledger. `LINK_CANONICAL_DOC` rows route inventory
or implementation detail to the named canonical source. Named external
pointers that must remain stable are verified separately below.
`MOVE_SKILL` is unused on purpose: the one candidate, "Skill authoring
standards (HARDLINE)", is *cited by* a skill as its source of truth (see below),
so moving it would invert the dependency.

## Mandatory invariants — where each one lands

The plan names eight invariant families that must survive. Each stays in the
root file, self-sufficient after compaction:

| Invariant | Source lines | Root destination |
|---|---|---|
| Project intent + contribution rubric | 29-137 | §2 Contribution rubric |
| Bug-premise / original-intent verification | 138-181 | §2 Contribution rubric |
| Footprint ladder / narrow waist | 182-212 | §3 Footprint ladder |
| Prompt caching + message alternation | 1345-1358, 1190-1225 | §7 Hard invariants |
| Session-scoped surface capability | 213-251 | §3 (heading text preserved verbatim) |
| Profile-safe paths + secrets vs settings | 647-663, 1374-1453 | §7 Hard invariants |
| Real-path E2E + native OS test lanes | 1554-1558, 1628-1681 | §8 Testing contract |
| No source-reading tests, no change-detector tests | 1682-1784 | §8 Testing contract |

Acceptance criterion 5 (root self-sufficiency after compaction) is satisfied
because none of these families is routed to nested context: nested files reach
the model as tool results, which compaction may summarize away.

## Pointer integrity

Three external references name a root section by title. All three headings are
preserved verbatim so no pointer breaks:

| Pointer | Required heading |
|---|---|
| `CONTRIBUTING.md:399` — "See `AGENTS.md` (section **Adding New Tools**)" | `## Adding New Tools` |
| `skills/software-development/hermes-agent-skill-authoring/SKILL.md` — "see AGENTS.md, 'Skill authoring standards (HARDLINE)' — that section is the source of truth" | `### Skill authoring standards (HARDLINE)` |
| `apps/desktop/AGENTS.md` — "See the root AGENTS.md, 'Surface capability is a property of the SESSION.'" | `### Surface capability is a property of the SESSION, never of the process env` |

Those references require no edit because the headings remain pinned verbatim.

Before this tracked ledger was added, no page under `website/docs/` referenced
a root `AGENTS.md` section by name, so the cleanup required no pointer repair.

## Nested context

Two desktop-scoped sections move to the existing `apps/desktop/AGENTS.md`:
**Electron Desktop Chat App** (lines 531-547, 4,427 chars) and **Bot Mode**
(lines 923-990, 3,843 chars). The nested guide is reduced from 11,195 to
7,307 Unicode characters while retaining both contracts and routing long
rationale to `apps/desktop/DESIGN.md`, regression tests, and desktop docs.

**No new nested `AGENTS.md` files are created.** Two candidates were considered
and rejected:

- `tests/AGENTS.md` — would only duplicate the root testing contract, which the
  plan forbids.
- A TypeScript-style file — the rules at lines 316-338 span `apps/`, `ui-tui/`,
  `web/`, and `website/`, so no single directory could own them. They stay in
  root as a short bullet list.

## Final budget result

- `AGENTS.md`: 95,154 / 1,784 lines -> **27,883 / 489**. This is above the
  25,000 review threshold but below the plan ceiling of 28,000; the retained
  space is justified by the 45 `KEEP_ROOT` sections and eight mandatory
  invariant families enumerated above.
- `apps/desktop/AGENTS.md`: 11,195 / 210 lines -> **7,307 / 133**, leaving 693
  characters of headroom below the 8,000 nested boundary.
- The unrelated Anthropic design-system reference was renamed from reserved
  `templates/claude.md` to `templates/anthropic-claude.md`; the source catalog
  plus generated English and Simplified Chinese pages were updated. All 54
  template payloads remain byte-identical. Repository-wide lint now passes
  rather than treating the 20,904-character design reference as progressive
  agent context.
