# R3-CAND-018 — final correctness review (head-qualified)

This artifact is bound to exact candidate head `d26a219ab783f0b04c8f0712adc4b31e3c754212` (`HEAD^{tree}` `b936037463e1b8c222c16b909fcb9db176b024db`) in worktree `C:/TEMP/hermes-redteam-f751a8c5/impl/r3-cand-018`.

## Verdict

**REQUEST CHANGES.** The exact head is not correct for the required Windows-safe auth-store read/publication, strict-schema, and corruption-recovery CAS contract.

## Blocking findings

1. **F1 — incomplete current schema accepted (high):** `hermes_cli/auth_store.py:277-306` defaults `require_section=False`; the ordinary load at `740-744` therefore accepts `{"version":1}`, and `746-749` silently adds an empty `providers` mapping. Live exact-head reproduction loaded and rewrote that document instead of preserving it and raising `AuthStoreCorruptionError` with a digest. `suppressed_sources` is also not schema-validated.
2. **F2 — recovery digest is not a complete CAS (high):** `recover_auth_store()` compares the reviewed digest at `859-870`, but recovery sets `expected_digest=None` at `771` and skips the final recheck at `806-809`. A deterministic publication-callback race changed the target after the initial check; recovery returned success and overwrote the changed bytes.
3. **F3 — Windows publication remains TOCTOU-vulnerable (high):** `_atomic_publish_auth_store()` validates handles at `356-367`, closes them, then calls path-based `MoveFileEx()` at `369-375`. A deterministic Windows ancestor-symlink swap before `MoveFileEx` redirected publication into the attacker directory and returned success.
4. **F4 — sidecar publication leaks across a raced ancestor (high):** `_write_corrupt_sidecar()` uses path-based `os.open`/`os.link` at `657-673` without a validated retained parent handle. A deterministic Windows ancestor swap created nine attacker-directory `auth.json.corrupt*` files containing the supplied credential-bearing corruption bytes before returning `None`.

## Verification receipts

- Focused exact-head tests: **43 passed, 0 failed** (`4 + 23 + 6 + 10` across the four candidate files).
- Related auth/provider selection: **194 collected; 186 passed, 7 skipped, 1 known pre-existing Windows mode-baseline failure; exit 1**. The exact mode-sensitive node was then run three times at candidate `d26a219ab783f0b04c8f0712adc4b31e3c754212` and three times at base `306db2776c6b6f1acc85c31c4dabba3263f0e9fd`; all six observed `0o666` and failed identically. Preserved log: `C:/TEMP/hermes-redteam-f751a8c5/verify/r3-cand-018-final-related.log`.
- Fresh external ad-hoc verifier `C:/Users/andre/AppData/Local/Temp/hermes-verify-kk07hib_.py` emitted `HERMES_ADHOC_VERIFY_GREEN F1_REPRODUCED F2_REPRODUCED F3_REPRODUCED F4_REPRODUCED` at the exact head; its helper and owned probe roots were removed. This is ad-hoc finding evidence, not canonical suite-green evidence.
- Changed Python compile: **exit 0**.
- `git diff --check base..HEAD`: **exit 0**.
- Candidate pre-artifact status: clean; merge-base with declared base `306db2776c6b6f1acc85c31c4dabba3263f0e9fd`; parent `4f1967c8f9e5dbb5843e322628f15ec75ad8d18f`.

## Required disposition

Do not approve. Repair F1–F4, add race/schema regressions, and rerun the full focused/related Windows matrix plus extraction/seam, compile, and diff gates from a new exact head. The only final worktree mutations from this review are this artifact and the canonical `review-final-correctness.md`.
