# R3-CAND-018 — final correctness review

## Verdict

**REQUEST CHANGES.** The exact candidate closes the four prior R3-CAND-018 findings in the exercised Windows security matrix, but the final repair introduces a real ordinary-save regression and does not establish a race-safe POSIX CAS boundary. Do not approve or publish this candidate.

## Candidate binding

- Repository: `NousResearch/hermes-agent`
- Worktree: `C:/temp/hermes-redteam-f751a8c5/impl/r3-cand-018`
- Branch: `redteam/r3-cand-018`
- Exact reviewed `HEAD`: `105ad21194f8956f49b79cfb764dfa414232ac54`
- `HEAD^{tree}`: `972019d61461b4b5143c7de5d5ea89bd553cf442`
- `HEAD^`: `8596e02f7b4d6302c14987636e610f7578e6a2b1`
- Tree of `HEAD^`: `519eca344fea28807fdfede56bd2b1b3c449e6d7`
- Declared base: `306db2776c6b6f1acc85c31c4dabba3263f0e9fd`
- `git merge-base base HEAD`: `306db2776c6b6f1acc85c31c4dabba3263f0e9fd`
- Candidate status before this review artifact: clean
- All referenced Git objects were present; `git diff --check base..HEAD`: exit `0`

The exact head is the documentation/receipt commit; the implementation under review is its first parent, repair commit `8596e02f7b4d6302c14987636e610f7578e6a2b1` (`fix(auth): close final publication and recovery races`). The repair changes four implementation/test paths (`403 insertions, 88 deletions`); the exact head adds the three final receipt/review paths.

## Prior findings — disposition

The final repair is materially better and the focused regressions are causally meaningful:

- **F1 strict schema:** `_validate_auth_store_schema()` now validates `suppressed_sources` and `_load_auth_store()` requires `providers` or `credential_pool` for ordinary reads. The `allow_legacy_empty=True` escape is explicitly scoped to legacy-compatible auth/credential-pool readers. Invalid current documents remain read-only and preserve their original bytes/digest.
- **F2 recovery CAS:** the reviewed corrupt-source digest is passed through the locked save path, rechecked after serialization, and rechecked in `_atomic_publish_auth_store()`. The adversarial mutation-after-serialization test and stale-loaded-writer test pass.
- **F3 Windows publication:** the repaired path retains a validated parent handle, opens the source with `DELETE`, validates source/parent identity, and publishes through `SetFileInformationByHandle`; the final-component and ancestor-swap tests pass.
- **F4 Windows sidecar:** exclusive temporary creation, no-replace hard-link publication, retained-parent protection, and final identity/byte verification are present; collision, symlink, interruption, and ancestor-swap tests pass.

These positives do not cure the blockers below.

## Blocking findings

### F5 — ordinary fresh-dict save fails when replacing an existing store

**Severity: high; functional regression introduced by repair commit `8596e02`.**

In `hermes_cli/auth_store.py:474-505`, the Windows publication path opens the existing destination into `target_handle`. It closes that handle only inside the `expected_digest is not None` branch. A legitimate caller can pass a newly constructed valid store to `_save_auth_store()` for an existing file; that object has no remembered snapshot, so `expected_digest` is `None`. The destination handle then remains open when `_windows_rename_relative(..., replace_existing=True)` is called, and Windows rejects the replacement.

Live exact-candidate probe, using the repository’s Windows interpreter (`C:/Users/andre/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe`, `win32`) and a temporary target outside the checkout:

1. `_save_auth_store({"version": 1, "providers": {"first": {}}}, existing_path)` succeeded for the first write.
2. `_save_auth_store({"version": 1, "providers": {"second": {}}}, existing_path)` raised `OSError: [Errno 5] Could not publish auth-store file: auth.json`.

The same probe at the immediately preceding repair2 head `d26a219ab783f0b04c8f0712adc4b31e3c754212` printed `pre_repair_second_ok`; the regression is therefore attributable to the final repair, not the host or probe. Fix the handle lifecycle so every opened target handle is closed before the replacement primitive (while retaining any required CAS identity protection), and add a canonical regression for a fresh caller-owned dict replacing an existing store.

### F6 — POSIX expected-digest publication is check-then-replace, not a complete CAS

**Severity: high under the stated adversarial stale-writer contract; host-limited runtime evidence.**

The non-Windows branch at `hermes_cli/auth_store.py:518-530` reads and hashes the target, then calls path-based `os.replace()`. `os.replace()` is atomic as a directory-entry operation, but it is not conditional on the bytes/inode that were just checked. A writer that changes the target after `_read_auth_bytes()` returns and before `os.replace()` is still overwritten. The code comment narrows the guarantee to “all in-process writers use this same lock”; that is a cooperative-lock guarantee, not a race-safe CAS boundary. This host is `win32`, so the POSIX branch was not executed here; the static path remains unclosed and requires a Linux canonical race regression or an actual conditional/identity-bound publication primitive.

## Verification receipts

### Focused security matrix — fresh canonical runner

Command was run from the exact candidate worktree through `scripts/run_tests.sh`, with `HERMES_PYTHON` explicitly bound to the frozen interpreter, `HERMES_DESKTOP`, `HERMES_DESKTOP_CWD`, `HERMES_DESKTOP_CONNECTION_MODE`, and `PYTHONPATH` unset, isolated `HOME`/`HERMES_HOME`/`TEMP`/`TMP`/`TMPDIR`, `HERMES_TEST_WORKERS=1`, and `HERMES_TEST_FILE_RETRIES=0`:

```text
bash scripts/run_tests.sh -q \
  tests/hermes_cli/test_auth_store_r3_cand_018.py \
  tests/hermes_cli/test_auth_store_r3_cand_018_adversarial.py \
  tests/hermes_cli/test_auth_store_read_failure.py \
  tests/hermes_cli/test_auth_store_windows_encoding.py
```

Result: **4 files, 46 tests passed, 0 failed, exit 0**, in `24.7s`. The matrix included the extraction/persistence-shard identity seam and all new F1–F4 adversarial regressions.

### Related auth/provider selection — fresh canonical runner

```text
bash scripts/run_tests.sh -q tests/hermes_cli/test_auth_*.py \
  tests/agent/test_credential_pool_oauth_writethrough.py
```

Result: **20 files, 189 passed, 1 failed, 7 skipped, exit 1**, in `128.8s`.

The sole failure was:

```text
tests/hermes_cli/test_auth_nous_provider.py::test_shared_store_write_and_read_roundtrip
```

It observed Windows mode `0o666` rather than the test’s accepted `0o600`/`0o644`. The same mode-baseline failure was previously reproduced at the declared base and prior candidate, so it is recorded as pre-existing rather than attributed to this repair. Nevertheless, the related auth/provider selection is not fully green. The seven skips are OS-marked tests unavailable on this `win32` host.

### Compile and hygiene

- `python -m py_compile hermes_cli/auth.py hermes_cli/auth_store.py hermes_cli/auth_commands.py agent/credential_pool.py`: exit `0`.
- `python scripts/check-windows-footguns.py --diff HEAD^`: exit `0` (`0` files scanned).
- `git diff --check base..HEAD`: exit `0`.
- Focused and related runs used fresh external scratch roots; review-owned temporary probes and roots were removed. The candidate implementation was not modified.

## Required disposition

Keep `R3-CAND-018` at **REQUEST CHANGES**. Repair F5, add its fresh-dict replacement regression, and provide a POSIX conditional-publication/CAS implementation plus a Linux race regression for F6. Then rerun the focused security matrix, the complete related auth/provider selection, extraction/seam checks, compilation, Windows reparse/TOCTOU checks, and canonical cross-platform CAS coverage from a new exact head. The known Windows mode-baseline failure must remain separately identified and must not be counted as candidate-green evidence.
