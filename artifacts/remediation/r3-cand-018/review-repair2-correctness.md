# R3-CAND-018 — repair2 residual closure

## Verdict

**REPAIR2 VERIFIED FOR THE REVIEWED RESIDUALS.** The repair1 correctness findings
R1 (Windows no-follow read/open TOCTOU) and R2 (valid-JSON structural corruption
normalization) are closed at the new head below. The extraction remains intact.

## Candidate binding

- Worktree: `C:/TEMP/hermes-redteam-f751a8c5/impl/r3-cand-018`
- Repair1 head / repair2 parent: `61994a580080b0092fa36226ab97aa25c9d8cbd5`
- Repair2 head: `4f1967c8f9e5dbb5843e322628f15ec75ad8d18f`
- Repair2 tree: `2e407325fadd90f12d6b9890acc52380c6ab73ec`
- Branch: `redteam/r3-cand-018`
- Final status after this artifact commit: required to be clean

## Closed residuals

### R1 — Windows final/ancestor reparse TOCTOU

`hermes_cli.auth_store._read_auth_bytes()` now uses a native Windows
`CreateFile` handle with `FILE_FLAG_OPEN_REPARSE_POINT`, reads only from that
validated handle, rejects reparse attributes, and compares
`GetFinalPathNameByHandle()` with the requested path. This closes both final
component substitution and ancestor junction/reparse redirection. The parent is
also handle-validated before any auth temp file is created.

Regression coverage changes the path after the lexical check and before the
handle open for both a final symlink and an ancestor directory symlink. Both
assert rejection and preserve attacker-controlled bytes.

### R2 — structural corruption normalization

Canonical auth-store schema validation now rejects invalid top-level values,
`providers` list/entry shapes, invalid `credential_pool` shapes/items, invalid
version/metadata types, and incomplete explicit recovery replacements. Valid
JSON structural corruption is preserved byte-for-byte in a `.corrupt` sidecar,
raises `AuthStoreCorruptionError`, carries the SHA-256 digest, and cannot be
silently normalized to an empty store.

### Recovery CAS and publication

`recover_auth_store()` now requires a non-empty reviewed corruption SHA-256 token;
there is no fallback that snapshots the digest implicitly. Primary publication no
longer calls `utils.atomic_replace()` (which resolves destination links). It uses
atomic no-follow directory-entry publication, validates the Windows parent and
existing destination through native handles, and avoids post-publication path
`chmod` traversal.

## Verification

Focused canonical runner, exact committed head:

```text
scripts/run_tests.sh --basetemp=C:/TEMP/hermes-redteam-f751a8c5/verify/r3-cand-018-repair2/pytest-focused-committed-01 \
  tests/hermes_cli/test_auth_store_r3_cand_018.py \
  tests/hermes_cli/test_auth_store_r3_cand_018_adversarial.py \
  tests/hermes_cli/test_auth_store_read_failure.py \
  tests/hermes_cli/test_auth_store_windows_encoding.py
```

Result: **4 files, 43 tests passed, 0 failed**, exit 0, native Windows
`win32` / Python 3.11.15.

Related auth/provider selection:

```text
scripts/run_tests.sh --basetemp=C:/TEMP/hermes-redteam-f751a8c5/verify/r3-cand-018-repair2/pytest-related-03 \
  tests/hermes_cli/test_auth_*.py \
  tests/agent/test_credential_pool_oauth_writethrough.py
```

Result: **20 files, 186 tests passed, 7 skipped, 1 failed**. The only failure is
the known Windows file-mode baseline in
`tests/hermes_cli/test_auth_nous_provider.py::test_shared_store_write_and_read_roundtrip`
(observed `0o666`; repair1 independently reproduced the same failure at the
base). It is not an R3-CAND-018 regression and is not claimed green here.

Additional gates on the committed implementation:

- `python -m py_compile` over the changed implementation and tests: exit 0.
- `scripts/check-windows-footguns.py`: exit 0, 3 staged files scanned.
- `git diff --check`: exit 0.
- Changed implementation remains below the 2,000-line ceiling: `auth_store.py`
  is 1,066 lines.
- No push performed.
