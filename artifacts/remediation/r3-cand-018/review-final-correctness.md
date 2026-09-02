# R3-CAND-018 — final correctness review

## Verdict

**REQUEST CHANGES.** The candidate at the exact reviewed head is not correct for the required Windows-safe auth-store read/publication, strict-schema, and corruption-recovery CAS contract. The blockers below are independently reproducible from the final checkout. Do not approve or publish this candidate.

## Candidate binding

- Worktree: `C:/TEMP/hermes-redteam-f751a8c5/impl/r3-cand-018`
- Branch: `redteam/r3-cand-018`
- Exact reviewed `HEAD`: `d26a219ab783f0b04c8f0712adc4b31e3c754212`
- `HEAD^{tree}`: `b936037463e1b8c222c16b909fcb9db176b024db`
- `HEAD^`: `4f1967c8f9e5dbb5843e322628f15ec75ad8d18f`
- Declared implementation base: `306db2776c6b6f1acc85c31c4dabba3263f0e9fd`
- `git merge-base base HEAD`: `306db2776c6b6f1acc85c31c4dabba3263f0e9fd`
- Pre-artifact worktree status: clean
- `git cat-file` checks for `HEAD`, its tree, and the parent auth-store blob: passed
- Base-to-head scope: 10 paths, `1890 insertions(+), 538 deletions(-)`; `hermes_cli/auth_store.py` is 1,066 physical lines (binary-newline count)

## Blocking findings

### R3-CAND-018-F1 — ordinary load still accepts a structurally incomplete store

**Severity: high.**

`_validate_auth_store_schema()` at `hermes_cli/auth_store.py:277-306` defaults to `require_section=False`. The normal load path at lines `740-744` calls it without requiring `providers` or `credential_pool`, and lines `746-749` then inserts an empty `providers` mapping. Thus a valid-JSON current document such as `{"version":1}` is accepted as a usable empty store and can be rewritten, rather than being preserved as corruption and requiring explicit recovery. This is the same data-loss class that repair2 was intended to close for invalid auth-store shapes.

Live reproduction at the exact head, with a temporary target outside the checkout:

- Input bytes: `{"version":1}`
- `_load_auth_store()` returned `{'version': 1, 'providers': {}}`
- `_save_auth_store()` then rewrote the primary with a new serialized empty store and `updated_at`
- No `AuthStoreCorruptionError`, digest, or recovery requirement was raised

The validator also does not validate the `suppressed_sources` field even though its consumers require a mapping of provider IDs to lists. This leaves additional invalid current documents outside the strict-schema gate. The fix must require a complete canonical section on ordinary loads and validate every persisted top-level field shape, while preserving the exact primary bytes and reporting the SHA-256 digest.

### R3-CAND-018-F2 — recovery digest check is not a complete CAS

**Severity: high.**

`recover_auth_store()` reads and compares the reviewed digest at `hermes_cli/auth_store.py:859-870`, then calls `_save_auth_store_locked(..., recovery=True)` at lines `871-876`. In `_save_auth_store_locked()`, `expected_digest` is explicitly `None` for recovery at line `771`, and the pre-publication recheck at lines `806-809` is guarded by `not recovery`. Therefore a target change after the initial digest check but before publication is overwritten instead of rejected.

Deterministic live reproduction at the exact head monkeypatched the publication callback to replace the target with valid attacker bytes after the initial digest check. Recovery returned success and the final primary contained the replacement payload. The mandatory digest token is present, but the write is not compare-and-swap safe across the complete check-to-publish interval. Recovery must bind publication to the reviewed bytes using an atomic/handle-based mechanism or a final race-safe CAS boundary; a cooperative advisory lock alone is insufficient for this contract.

### R3-CAND-018-F3 — Windows publication closes validation handles before path-based replacement

**Severity: high; security boundary failure.**

The Windows branch of `_atomic_publish_auth_store()` at `hermes_cli/auth_store.py:349-385` validates the parent and existing destination through native handles at lines `356-367`, closes those handles, and only then performs path-based `win32file.MoveFileEx()` at lines `369-375`. A concurrent ancestor reparse/junction swap between handle close and `MoveFileEx` can redirect both source and destination path resolution.

Deterministic Windows live reproduction at the exact head swapped the validated parent directory for a directory symlink immediately before `MoveFileEx`. The call returned success, consumed the attacker-directory temporary file, and replaced the attacker directory's `auth.json`; the original validated directory's `auth.json` remained unchanged. The publication operation therefore still has a check-then-use race despite the handle-level read fix. Keep the no-reparse/identity authority alive through the actual publication primitive, or use an equivalent directory-handle-relative, atomic no-follow replacement that cannot be redirected after validation.

### R3-CAND-018-F4 — corrupt sidecar publication follows a raced ancestor and leaks bytes

**Severity: high; credential disclosure.**

`_write_corrupt_sidecar()` at `hermes_cli/auth_store.py:646-698` creates its temporary and destination paths with path-based `os.open()`/`os.link()` at lines `657-673`. It does not validate or retain a native no-reparse parent handle. A raced ancestor can therefore redirect the forensic copy outside the intended auth directory.

Deterministic Windows live reproduction at the exact head swapped the parent for a directory symlink just before the first temporary `os.open()`. The function eventually returned `None`, but created **nine** `auth.json.corrupt*` files in the attacker directory, each containing the supplied credential-bearing corruption bytes. Returning `preserved=False` does not undo the disclosure. Sidecar creation must use the same handle-relative/no-reparse parent publication discipline as the primary, and failure cleanup must remove only owned files without leaving copies in a redirected location.

## Positive and substrate evidence

- Focused candidate tests, run individually from the guarded worktree with the native Python 3.11.15 / `win32` interpreter:
  - `tests/hermes_cli/test_auth_store_r3_cand_018.py`: **4 passed**
  - `tests/hermes_cli/test_auth_store_r3_cand_018_adversarial.py`: **23 passed**
  - `tests/hermes_cli/test_auth_store_read_failure.py`: **6 passed**
  - `tests/hermes_cli/test_auth_store_windows_encoding.py`: **10 passed**
  - Total focused result: **43 passed, 0 failed**
- Related auth/provider selection:
  - `tests/hermes_cli/test_auth_*.py tests/agent/test_credential_pool_oauth_writethrough.py`
  - **194 collected; 186 passed, 7 skipped, 1 failed; exit 1**
  - Sole failure: `tests/hermes_cli/test_auth_nous_provider.py::test_shared_store_write_and_read_roundtrip`, observed mode `0o666` instead of `0o600`/`0o644`. A preserved exact-node recheck ran three times at candidate `d26a219ab783f0b04c8f0712adc4b31e3c754212` and three times at base `306db2776c6b6f1acc85c31c4dabba3263f0e9fd`; all six runs failed identically with `0o666`. This is baseline-equivalent, not an R3-CAND-018 regression. The related selection is nevertheless not fully green. Full log: `C:/TEMP/hermes-redteam-f751a8c5/verify/r3-cand-018-final-related.log`.
- Fresh external ad-hoc verifier, exact helper `C:/Users/andre/AppData/Local/Temp/hermes-verify-kk07hib_.py`, ran against the pinned candidate with scrubbed `PYTHONPATH` and emitted `HERMES_ADHOC_VERIFY_GREEN F1_REPRODUCED F2_REPRODUCED F3_REPRODUCED F4_REPRODUCED`; helper and owned probe roots were removed afterward. This is ad-hoc finding evidence, not canonical suite-green evidence.
- Changed implementation/test `py_compile`: exit 0.
- `git diff --check base..HEAD`: exit 0.
- `scripts/check-windows-footguns.py` with no staged files: exit 0, but it reported `No staged files to scan`; this is not a substitute for the blocking runtime probes.
- The focused shard/seam test passed and no duplicate persistence owner was found in the reviewed extraction. Those positives do not establish the required race, strict-schema, or recovery-CAS guarantees.

## Required disposition

Keep the candidate at `REQUEST CHANGES`. Repair F1–F4, add regressions for the missing canonical section, invalid persisted top-level fields, target mutation after recovery review, publication ancestor substitution, and sidecar ancestor substitution/leakage. Then rerun the focused security matrix, the complete related auth/provider selection, Windows native reparse/TOCTOU probes, extraction/seam checks, compilation, and diff hygiene from the new exact head. The known file-mode baseline must remain separately identified rather than counted as candidate-green evidence.

## Review hygiene

All live probes used temporary paths under `C:/TEMP/hermes-redteam-f751a8c5/verify`; every helper/root owned by this review was removed after execution. Similarly named verification residue not owned by this review was left untouched. The candidate implementation was not modified. The final artifact writes are the only worktree mutations from this review.
