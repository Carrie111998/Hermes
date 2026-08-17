# Re-landing ruff Pyflakes "F" group enforcement

**Date:** 2026-08-16
**Baseline:** `c3b8083116` (local `main`)
**Status:** approved, Stage 1 in progress

## Problem

The Pyflakes "F" group was cleared and enforced project-wide in May 2026 (commits
`80383d1cf6` broadening, `01690ea871` F841 backlog). It fell out of `main` during the
v0.15.1 upstream cutover — not by a named revert, but silently, the same way the hosted
console was lost in the 0.19.0 merge. Nothing noticed, because nothing was checking.

Today `main` selects `["PLW1514"]` only, so a green `ruff check` proves PLW1514 and
nothing else. Measured on `c3b8083116`:

**1049 findings across 544 of 3657 tracked `.py` files** — F401 541, F841 348, F601 94,
F821 43, F811 14, F402 4, F541 3, F822 2.

The count is drifting upward (1039 on 2026-08-11, 1049 on 2026-08-16). Every day without
a gate adds work, and at least one finding is a live production crash.

## What the current state actually is

Three premises that looked true from the May-era record but are not:

| Assumption | Reality on `c3b8083116` |
|---|---|
| `.github/workflows/lint.yml` has no blocking ruff job | It **does** — a `ruff-blocking` job runs `ruff check .` with no `--exit-zero`. The enforcement pipe is already built and needs no edit; it is merely pointed at a near-empty rule set. |
| `.pre-commit-config.yaml`'s only hook is gitleaks | Two hooks: gitleaks and a local `event-type-coverage` hook. The **ruff hook** is the one genuinely missing. |
| The `graphs/jobflow.py` F821 and the shadowed-test F811s are unlanded | Their content **is** on local `main` (rebased off `47e45dce71`, which is now unreferenced). F821 is 43, not 44; F811 is 14, not 20. |

So of the three pieces of enforcement infrastructure the archive branch carries, `main`
is missing two: the `select` value and the pre-commit hook.

## Vehicle: read the archive, redo the edits

The original work is readable on `main-pre-0.15.1-cutover` (tip `df97ba1248`). That branch
is a deliberate archive under a standing instruction: **do not merge it** (it diverges
across 544 files and merging reverts `main` to pre-0.15.1 state) and **do not delete it**.

Replaying `01690ea871` as a patch was evaluated and rejected on measurement, not instinct.
A non-mutating in-memory 3-way replay (`git merge-tree --write-tree --merge-base
01690ea871^ main 01690ea871`) conflicts on **29 of its 106 paths**, and 95 of the 106 files
diverged from the archive's parent. Main's 348 F841 is 1.75x the campaign's original 198,
so most of today's backlog is post-cutover code the archive never saw.

The archive's value is its **decision record** — which bindings were dropped versus kept,
and why — not its diff.

Two things must not be copied verbatim:

- The per-file-ignore path `skills/red-teaming/godmode/scripts/auto_jailbreak.py` is now
  `optional-skills/security/godmode/scripts/auto_jailbreak.py`.
- The archive predates PLW1514, so the target is `select = ["F", "PLW1514"]`, not `["F"]`.

## Design

### Gate first, burn down after

The gate goes on in Stage 1, before any code is cleaned, via a **sunset list**: a
per-file, per-rule `per-file-ignores` table naming exactly the 544 files that offend today.
Every other file — 3113 of them, 85% of the tree — is gated from day one. No new offender
can enter a clean file.

The list is **per-rule, not per-file-blanket**: `"agent/agent_init.py" = ["F821"]`, so a
file exempted for F401 is still gated on F821. 502 of the 544 entries name a single rule,
so rule-cluster burn-down deletes entries wholesale rather than editing them.

The list is append-never, shrink-only. It reaches zero at Stage 6 and is deleted.

### Config lives in `ruff.toml`

The whole `[tool.ruff]` block moves out of `pyproject.toml` into a root `ruff.toml`.
Rationale: the sunset list is 544 lines and would more than double `pyproject.toml`
(421 lines today), putting a large churning diff in the file that also holds dependencies,
pytest markers, and build config. `ruff.toml` is still exactly one ruff config source, and
it is a file whose entire purpose is to shrink to nothing.

Verified empirically, not assumed: with both files present, ruff reads `ruff.toml` and
ignores `pyproject.toml`'s `[tool.ruff]` **silently** — no warning, no error. A leftover
`[tool.ruff]` table in `pyproject.toml` would therefore be dead config that still reads as
live to anyone grepping for it. Stage 1 removes it and leaves a pointer comment, and the
config test asserts it never comes back.

### Anti-regression guard

This work was lost once to a silent config revert. `tests/test_lint_config.py` asserts:

1. `ruff.toml` exists at the repo root.
2. Its `lint.select` contains both `F` and `PLW1514`.
3. `pyproject.toml` carries no `[tool.ruff]` table (which ruff would silently ignore).

A future merge that drops the config then fails a test instead of passing quietly. This
mirrors the existing `events/coverage.py` idiom of guarding a config invariant with a
cheap, no-I/O check.

**Accepted risk:** the blocking CI job installs ruff **unpinned** (`uv tool install ruff`),
while the archive pinned `0.15.12`. With the F group enabled, a ruff release that adds or
tightens an F rule can break CI on an unrelated PR. Pinning was offered and declined; this
is recorded as a known, accepted exposure, not an oversight.

## Stages

### Stage 1 — Gate on (no code edits)

The only stage that changes enforcement. Everything after it is burn-down.

1. Move `[tool.ruff]` / `[tool.ruff.lint]` / `[tool.ruff.lint.per-file-ignores]` from
   `pyproject.toml` to a root `ruff.toml`, preserving `preview = true` and the four
   existing PLW1514 per-file-ignores verbatim. Leave a pointer comment in `pyproject.toml`.
2. Set `select = ["F", "PLW1514"]`.
3. Re-path the godmode F821 ignore to `optional-skills/security/godmode/scripts/auto_jailbreak.py`.
4. Generate the 544-entry sunset list.
5. Add the ruff pre-commit hook (`astral-sh/ruff-pre-commit`, pinned). Confirm the current
   hook id — upstream renamed `ruff` to `ruff-check` in recent releases.
6. Add `tests/test_lint_config.py`.

**Exit criteria**

- `ruff check . --no-cache` exits 0 on the tree.
- A deliberately introduced F401 in a clean (unlisted) file **fails** the check — proving
  the gate is live rather than vacuously green.
- `tests/test_lint_config.py` passes, and fails when `F` is removed from `select`.
- `pytest --collect-only` count unchanged from baseline.

### Stages 2-5 — Burn-down

Ordered by signal density, not size. Each stage deletes entries from the sunset list and is
independently landable and independently abandonable.

| Stage | Rules | Findings | Files | List after |
|---|---|---|---|---|
| 2 | F821, F811, F822, F402 | 63 | 31 | 525 |
| 3 | F601 | 94 | 4 | 523 |
| 4 | F401, F541 | 544 | 383 | 172 |
| 5 | F841 | 348 | 172 | 0 |

**Stage 2 — the real bugs.** A prior triage (2026-08-16, worktree `sharp-payne-a1fb61`,
MemPalace `agent-src/decisions`) already graded all 43 F821 **by execution**, not by
reading ruff output, and found 19 hits across 5 files that are live defects. Those
findings are adopted here rather than re-derived:

1. `plugins/platforms/sms/adapter.py:415-423` — `re.sub()` x13 in
   `_strip_markdown_for_sms`, `re` never imported. Confirmed `NameError` by executing the
   AST-extracted function. Fires on **every outbound SMS containing markdown**. Lands as
   its own commit, first.
2. `plugins/platforms/whatsapp/adapter.py:724,875` — bare `json.loads` with `json` unbound
   at module scope. **The 2026-08-11 record that the module "imports `import json as
   _json`" is wrong and was corrected by that triage:** the import sits at line 2347,
   *inside a function*, under a different name, so it never binds module-scope `json`.
   Both call sites are inside `try:/except Exception:`, so the `NameError` is **swallowed**
   — a silent behaviour bug, not a crash. JSON mention-patterns never parse (they always
   fall through to line/comma splitting) and every `lid-mapping-*.json` is silently skipped.
3. `plugins/google_meet/cli.py:97` — `except Exception as e:` defines a closure referencing
   `e`, invoked later via argparse dispatch. Python deletes the except-name at clause exit.
   Same class as the `graphs/jobflow.py` finding: an error path that itself errors.
4. `tests/tools/test_file_ops_cwd_tracking.py:33` — `os.name` with no `import os`. This
   **closes an open baseline question**: the 5 `TestShellFileOpsCwdTracking` failures
   recorded as unexplained in `tests-tools-windows-baseline.md` are all this `NameError`,
   proven by a serial re-run. A real regression with a one-line fix, not a timeout.
5. `tests/gateway/test_shutdown_watchdog.py:54` — `raise _ExitCalled(code)`, a class defined
   nowhere in the file or any conftest.

The remaining ~19 F821 across 13 files are runtime-safe annotation gaps (quoted, or under
`from __future__ import annotations`); they only break `typing.get_type_hints()` and take
one-line `TYPE_CHECKING` imports. So the count is not 43 latent crashes. The last 5 are the
godmode exec-injected globals, already permanently exempted in Stage 1.

F822 is an undefined name in `gateway/platforms/__init__.py`'s `__all__`. For F811, note
`hermes_cli/runtime_provider.py:799` redefines `has_named_custom_provider`
**byte-identically** to line 592 — a copy-paste artifact, so **delete** the second def
rather than rename it. Diff both segments before un-shadowing anything: a duplicate class
is as likely to be a copy-paste artifact as lost coverage.

**Stage 3 — F601.** 90 of 94 are in `scripts/release.py`, a contributor email-to-handle
attribution dict with repeated keys. Dedupe where both values agree. **Any key whose two
values differ is a real attribution bug** and gets flagged, not silently collapsed — that
divergent-value case is exactly what F601 was originally adopted for, after
`agent_failure_cluster` was declared twice with different routing targets and silently
misrouted live traffic.

**Stage 4 — autofix, reviewed.** `ruff --fix` handles F401/F541 (rated safe), but the diff
is reviewed rather than trusted. `__init__.py` re-exports and conditional imports are the
known false-positive shape.

**Stage 5 — F841, hand-graded.** The archive's decision rule applies verbatim; never
`--unsafe-fixes` (ruff rated only 4 of 198 fixes safe last time, and the unsafe fix can
delete a statement whose right-hand side has side effects):

- RHS with side effects — calls, awaits, `mocker.patch`, `monkeypatch`, object
  construction, `with ... as`, `pytest.importorskip` — keep the call, drop only the binding.
- Pure RHS — literals, `dict.get`, attribute reads, `len`, `.lower()`, comparisons, local
  comprehensions — remove the line.
- `with ... as x:` / `pytest.raises(...) as x` unused — drop `as x`, keep the context
  manager active.

Because F401 is gated by Stage 4, the cascade the archive documented — clearing an F841
orphans the import that fed it — is caught by the linter automatically rather than by
memory. Split across sessions by directory.

299 of the 348 are in `tests/`. These are hand-graded too, not exempted: the May campaign
found genuine dead refactor artifacts precisely there (three orphaned `name=value,`
statements in `tests/run_agent/test_provider_parity.py`, left over from a botched edit).

### Stage 6 — Close out

Delete the emptied sunset-list table, tighten `tests/test_lint_config.py` to assert no F
entries remain in `per-file-ignores`, land, and update the agent-memory record
`ruff-f601-scope-decision.md`.

## Verification, every stage

- `ruff check . --no-cache` exits 0; `--select <stage rules>` reports zero findings.
- `pytest --collect-only` count **must not fall**. This is the check that catches an
  un-shadowing or import edit that silently drops tests — the exact failure mode F811 was
  adopted for.
- Targeted `pytest` on the directories touched.
- Pre-existing failures are **proven** pre-existing by stashing and re-running on the clean
  tree, never assumed.

## Operating constraints

- Commits go through `python ~/.hermes/ops/git-quiet-commit.py` from PowerShell with a
  timeout of at least 600s. Never `git commit -- <paths>`, never `git commit -a`, never
  `--no-verify`.
- Import-test from the worktree (CWD/PYTHONPATH first). The editable install resolves
  top-level modules from `~/.hermes/agent-src`, so testing via the installed package tests
  the wrong tree.
- Landing target is **local `main`**, not pushed. `main` and `origin/main` are divergent
  (15665 ahead / 1545 behind).
- `main-pre-0.15.1-cutover` is read-only: never merged, never deleted.

## Non-goals

- Any rule group beyond `F` and the existing `PLW1514`.
- Pinning the CI ruff version (offered, declined, recorded above).
- Reformatting, or any edit not required to clear a finding.
- **"Fixing" the `Invalid # noqa directive on run_agent.py:107` warning.** It is prose —
  the comment describes ``# noqa: F401`` re-exports and ruff parses the literal sequence
  inside the sentence. There is no malformed directive. The 2026-08-15 audit and the
  2026-08-16 triage both examined it and recorded **do not fix**; that decision stands.
  The warning rides along on every ruff run and should be left alone.

## Stage 2 — delivered (2026-08-17), reconciled from two branches

Stage 1 and Stage 2 were implemented **concurrently and independently** by two sessions
that could not see each other, and `main` moved under both while they worked. The landed
result is a reconciliation of three sources, not a fast-forward of either branch.

| Source | Carried forward | Dropped |
|---|---|---|
| `claude/youthful-newton-d06c7d` (A) | `ruff.toml`, the `[tool.ruff]` deletion, the pre-commit hook, `tests/test_lint_config.py`, the SMS fix + its tests | — |
| `claude/heuristic-shirley-9f2baa` (B) | 28 source/test files: the `TYPE_CHECKING` gaps, F402 loop-var renames, F811 duplicate removals, the F822 `noqa` pair | its `pyproject.toml` `[tool.ruff]` edit; its SMS fix; its `google_meet` rewording |
| `main` | the `google_meet` fix (`c43e02a9e4`), the WhatsApp `json` bind (`3e5113bcfe`), the unique SMS end-to-end test | its SMS wrapper, superseded by A |

Three collisions had to be graded rather than merged:

1. **SMS (`_strip_markdown_for_sms`) — three different fixes.** `main` repaired the
   `NameError` by delegating the wrapper to the shared `strip_markdown`; A deleted the
   wrapper outright and called the helper at the one call site; B added `import re` and
   **restored the inlined duplicate regexes**. B's is a regression, not a fix: its bare
   `_(.+?)_` has no word-boundary guard and collapses `snake_case_names` to
   `snakecasenames`, and its `[a-z]*` fence charset leaves "Python"/"++" in the body.
   A's resolution is the one that landed. B's version was rejected on measured behaviour,
   not on style — the two pins live in
   `tests/gateway/test_sms.py::TestSmsStandaloneSendMarkdown`.
2. **`tests/gateway/test_sms.py` — both sides added a test class.** Neither was discarded.
   A's class pins the shared helper's string behaviour (fenced-code charset, the
   `__init__` → `init` known limitation); `main`'s contributed the two tests A had no
   equivalent of — an in-process/out-of-process parity assertion and an end-to-end
   `_standalone_send` test asserting the stripped `Body` actually reaches the Twilio POST.
   The four of `main`'s tests that called the now-deleted wrapper were rewritten onto
   `strip_markdown`; the two unique ones kept as `TestSmsStandaloneSendDelivery`.
3. **`gateway/platforms/__init__.py` F822.** A suppressed the whole file via the sunset
   list; B used two line-level `# noqa: F822`. B's won — the PEP 562 `__getattr__`
   re-export is a permanent, deliberate condition that wants a comment at the site, and
   the file-level entry would have masked any *future* real `__all__` typo in that file.
   Falsifier for the "just delete the `__all__` entries" reflex:
   `getattr(gateway.platforms, "QQAdapter")` returns the class.

**Result.** Sunset list **542 → 524** entries: 18 retired outright, 11 trimmed to fewer
codes. Remaining: **979 findings** — F401 535, F841 347, F601 94, F541 3.

**F821 and F822 are now zero tree-wide and gated in every file.** Neither code appears in
the sunset list at all, so a new undefined name or bad `__all__` export is a hard failure
anywhere in the repo. That is stronger than the stage-2 plan above, which assumed the
annotation-gap F821s would be suppressed rather than fixed; B had already fixed them with
`TYPE_CHECKING` blocks. The predicted "list after" of 525 was one off.

The list was **regenerated from the fixed tree**, not hand-edited: findings were recomputed
with the sunset block removed and the entries rebuilt from that output, so no entry is
stale by construction and none was widened. `tests/test_lint_config.py`'s stale-entry guard
independently re-proves this.

**One trap, if this is ever regenerated again.** Recomputing with `--config <other>.toml`
silently loses ruff's `requires-python` inference from `pyproject.toml`, and
`BaseExceptionGroup`/`ExceptionGroup` then report as F821 in four files that are actually
clean on 3.11+. Pin `target-version = "py311"` in the scratch config, and treat any
newly-required entry as a bug in the measurement before believing it.
