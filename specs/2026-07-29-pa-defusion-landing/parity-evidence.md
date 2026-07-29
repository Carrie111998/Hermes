# PA de-fusion Phase 1 landing-reconcile evidence

**WB:** `1a61fede-2fea-406c-8c72-b1d4ed1bb05e`  
**Mode:** build and hold  
**Captured:** 2026-07-29 08:40 +08

## Held result

| Repository | Reconciled state |
|---|---|
| Hermes | New held branch `origin/worker/41cb9170`; Branch-B replay `2d7287b1604531fad55d8ad23711cfb10f4af8e2`; 2026-07-28 intersection neutralization `28f2048bb1f343a0b2b3e386f034638738aedd6e`. |
| Hermes Branch A | `origin/worker/ef7660ab` remains `bcff03f4bacb0a3c63d7bddda03e27e164ec10ac`. Its full stack was already on the reconcile base. |
| Hermes Branch B | `origin/worker/77854585` remains `0f2eb822b8e7392fe0d5034237b65bcce1ef72a6`. |
| systems | `c85cd9a7` is already on current `origin/main`; no new commit. |
| marshal | `531fd40df9` is already on current `origin/main`; no new commit. |

The Hermes reconcile base was `origin/main` at
`08efbfb67b3c69400dd2845a8e805296e3de6287`. The final Hermes delta is 11
paths, 184 insertions and 54 deletions. The intersection-by-intersection
disposition is in `conflict-resolution-log.md`.

The landing WB expressly defines Branch B as D1-17/21/23/24. That instruction
supersedes the original Phase-1 evidence's D1-23 exclusion for one narrow
change: `gateway/platforms/whatsapp.py` now reads neutral `_pa_local_time`
first and retains `_tgg_sgt` as a legacy fallback. No other WhatsApp renderer
behavior was moved into Phase 1.

## Parity bar

| Gate | Result | Evidence |
|---|---|---|
| Registry byte parity | PASS | Origin-main and final dumps each contain 79 tools and are byte-identical at SHA-256 `08f322d1c7bfd51dc8d55b3661fa55ff78c9a613b0df57636431d000bae49485`. `evidence/registry-parity.txt`, `registry-origin-main.json`, `registry-final.json`. |
| Focused replay | PASS | 49 passed. `evidence/focused-replay.txt`. |
| 2026-07-28 intersection suite | PASS | 191 passed, 9 skipped across the durable consumer, PA business facts, and Python sandbox surfaces. `evidence/increment-intersection-tests.txt`. |
| Ruff on changed Python | PASS | No findings. `evidence/ruff.txt`. |
| Plane lint, warn mode | PASS with visible debt | Exit 0; 108 scanned findings: 23 baseline and 85 unsuppressed new findings. `evidence/plane-lint-warn.txt`. |
| Full broad command | EXECUTED; repository/environment baseline remains red | Exact requested command collected 23,509 tests: 23,134 passed, 178 skipped, 2 xfailed, 199 failed, and 22 collection errors. Most collection errors are unavailable optional extras. `evidence/full-pytest.txt`. |
| Broad failure comparison | PASS: no final-only deterministic failure | The 221 failed/error nodeids from the broad run were rerun on origin-main and the final stack using the exact same final-worktree interpreter. Both produced the exact same 45-nodeid failure set: 36 failed, 571 passed, 9 collection errors. Final-only 0; baseline-only 0. `evidence/broad-failure-parity.json`, `baseline-sameenv-failure-reproduction.txt`, `final-sameenv-failure-reproduction.txt`. |
| Manifest generation | PASS | Canonical generator completed at 578 files and produced no diff. `evidence/manifest-regeneration.txt`. |
| Bundle dry-run | PASS | `pcl pa-agent bundle --dry-run` assembled the manifest at source commit `28f2048bb1`, file count 578. `evidence/bundle-dry-run.json`. |

The full repository suite is not represented as green. It was run completely,
and its failed/error population was rechecked against the exact origin-main
base with a shared interpreter. That controlled comparison found no
final-stack-only deterministic failure.

## Manifest and moved-file check

No runtime file moved or was renamed. The only newly added path is
`tests/fixtures/replay/archived-tgg-envelope.json`. The manifest was still
regenerated through its canonical generator because the assembled runtime
changed; the generated manifest remained byte-clean. The bundle dry-run
verified the 578-file runtime set.

## Safety and scope

- No merge to any main.
- No deploy, release, client-host access, or client-data mutation.
- No MTU repin or restart.
- `b9c6ab5431` remains out of the stack.
- Original Branch A and Branch B remote refs remain untouched.

## Cross-provider review

Pending at this evidence checkpoint. The review artifact will be recorded at
`evidence/cross-provider-review.md`; this held stack does not satisfy terminal
review clearance until that file says `CLEAR`.
