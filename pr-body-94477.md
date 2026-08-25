## Summary

Fixes #94477: `hermes update` hard-resets Windows shallow-clone installer checkouts instead of fast-forwarding — and keeps doing it on every update.

### Root cause (validated with a deterministic real-git repro)

The updater's own fetch is not the poisoner. The banner (`check_for_updates`) and `hermes update --check` paths fetch with `--depth 1` on shallow repos. Once upstream advances even one commit, that fetch moves `origin/<branch>` onto a commit **disconnected from HEAD** — git never re-sends history below a shallow boundary the client already has — stacking `.git/shallow` entries and leaving no merge-base. The updater's subsequent plain fetch cannot heal this; `merge --ff-only` fails with *"refusing to merge unrelated histories"* (rc 128) and the divergence fallback hard-resets a checkout with zero local commits.

```
$ git merge-base HEAD origin/main   # exit 1 — disconnected boundaries
$ .git/shallow                      # 2+ stacked entries
$ git merge --ff-only origin/main   # rc 128 → reset branch fires
```

### Fix (three parts)

1. **Reset gate (`update_cmd._cmd_update_impl`)** — before concluding "history diverged", `_repair_shallow_boundary()` detects the shallow-disconnected state and heals it with `git fetch --unshallow origin <branch>` + a retried `merge --ff-only`. Only when that retry still fails (genuine divergence — e.g. real local commits) does the updater reset, keeping the existing reset contract (recoverable via reflog). This is the repair the issue's Option 2 suggests.
2. **Banner (`_check_via_local_git`)** — on shallow checkouts, probe the remote tip passively via `git ls-remote` instead of the boundary-poisoning `--depth 1` fetch. Same tip SHA, zero local ref movement.
3. **`hermes update --check` (`_check_update_shallow`)** — same passive ls-remote + compare-API probe, honouring the upstream-remote preference for `main`. The `--depth 1` fetch + FETCH_HEAD comparison is removed. When ls-remote is unreachable (offline / transient network error), the check degrades to the same stale-ref fallback as the banner (`FETCH_HEAD`, then `<remote>/<branch>`) instead of failing hard; a hard failure remains only when the checkout has no usable refs at all.

### Relationship to #88422

#88422 (open) unshallows at fetch time in the update path — a complementary cure. This PR instead (a) fixes the poisoner so the boundary is never broken in the first place, and (b) heals already-poisoned repos (the reporter's machine carries 36 stacked boundaries) at the merge step. No merge conflict: this PR does not change the update fetch command. See the coordination comment below on absorbing #88422's fetch-layer change or keeping the two PRs separate.

### Tests

`tests/hermes_cli/test_update_shallow_boundary.py` — 9 tests. The core tests build tiny local bare remotes and exercise the **real git binary** (no network):

- poisoned shallow clone + `_repair_shallow_boundary` → fast-forwards to the true tip, `.git/shallow` gone, no reset
- genuine local commit + repair → returns False — the repair never swallows commits; on true divergence the existing reset contract still applies (recoverable via reflog)
- full clone → repair returns False, no unshallow attempted
- banner shallow check → never runs `git fetch`; boundary and `origin/main` untouched; count recovered via compare API
- `--check` shallow path → no fetch, ls-remote probe, upstream-remote preference honoured
- `--check` shallow path, remote unreachable → falls back to the last-known ref (`FETCH_HEAD`, then `origin/main`) like the banner — no hard exit; hard failure only when no refs exist at all

All 9 pass after the fix. The offline-fallback tests are RED-verified: they fail on pre-fix code (hard `sys.exit(1)` when ls-remote is unreachable) and pass with the fallback.

## Test plan

- [x] `pytest tests/hermes_cli/test_update_shallow_boundary.py` — 9 passed
- [x] `ruff check` on all changed files — clean
- [x] Regression suites (`test_cmd_update.py`, `test_banner_git_state.py`, `test_update_check.py`, `test_update_behind_count_recovery.py`) — 61 passed, 0 failures

## Out of scope

- The secondary issue in #94477 (Windows exe-replacement handoff never returning the prompt) is a separate root cause and is left out of this PR.
- No sibling `--depth 1` call sites remain in the repo (the desktop `main.cjs` mirror referenced in the old comment was refactored away).

Closes #94477
