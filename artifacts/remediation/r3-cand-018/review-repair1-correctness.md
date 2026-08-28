# R3-CAND-018 — post-repair correctness review 1

## Verdict

**REQUEST CHANGES — candidate is not correct on Windows.**

The repair correctly routes the CLI recovery-source reader through the extracted
no-follow reader, but the no-follow guarantee is not race-safe on this Windows
runtime. A second, independent defect allows valid-JSON auth-store schema errors
to be silently replaced by an empty store. The focused regression files pass,
but these two findings are outside the exercised oracle set and block approval.

This review is based on the pinned candidate worktree and live execution only.
No repair receipt, repair transcript, or peer-review artifact was used.

## Candidate binding

- Worktree: `C:/TEMP/hermes-redteam-f751a8c5/impl/r3-cand-018`
- Base: `306db2776c6b6f1acc85c31c4dabba3263f0e9fd`
- Candidate parent: `a1f8c5181b191634834ef2d48ba385b2865575fd`
- Candidate `HEAD`: `61994a580080b0092fa36226ab97aa25c9d8cbd5`
- Candidate tree: `3d92d8c81b452fa126ea60ace5bb2f3d5c3c85ef`
- Branch: `redteam/r3-cand-018`
- Pre-artifact Git status: clean (`## redteam/r3-cand-018`)
- Base-to-HEAD diff: 8 paths, `1299 insertions(+), 538 deletions(-)`.
- Changed paths: `hermes_cli/auth.py`, `hermes_cli/auth_commands.py`,
  `hermes_cli/auth_store.py`, `hermes_cli/subcommands/auth.py`,
  `tests/hermes_cli/test_auth_store_r3_cand_018.py`,
  `tests/hermes_cli/test_auth_store_r3_cand_018_adversarial.py`,
  `tests/hermes_cli/test_auth_store_read_failure.py`, and
  `tests/hermes_cli/test_auth_store_windows_encoding.py`.
- `git diff --check 306db..HEAD`: exit 0.

The repair commit at `HEAD` changes `hermes_cli/auth_commands.py` from
`path.read_bytes()` to `auth_mod._auth_store._read_auth_bytes(path)` in
`_safe_recovery_source()`.

## Findings

### R3-CAND-018-R1 — Windows no-follow read remains TOCTOU-vulnerable

**Severity: high; blocks the repaired security contract.**

Source anchors:

- `hermes_cli/auth_store.py:142-165`: `_is_reparse_or_link()` performs a
  separate `lstat()`, then `_read_auth_bytes()` calls `os.open()`.
- `hermes_cli/auth_store.py:161-165`: `O_NOFOLLOW` is added only when the
  platform exposes it.
- `hermes_cli/auth_commands.py:527-538`: `_safe_recovery_source()` performs
  its own pre-check, then delegates to `_read_auth_bytes()`.

Live host evidence:

- Runtime: `sys.platform == "win32"`; `os.O_NOFOLLOW == 0`.
- Therefore the Windows path is `lstat -> os.open(path)`, with no atomic
  handle-level no-reparse protection between the check and open.
- A deterministic race probe replaced a checked regular recovery file with a
  symlink immediately before `_read_auth_bytes()` opened it. The reader returned
  the attacker-target bytes (`b'{"providers":{"attacker":{}}}'`) rather than
  rejecting the path.
- The same substitution through `auth_commands._safe_recovery_source()` also
  returned the attacker-target bytes.

The ordinary symlink test passes when the link exists before the initial check,
but that does not prove the required race property. The repair fixes the direct
`read_bytes()` seam but still relies on a check-then-open sequence on Windows.
Recovery imports can consequently ingest a path that was not the regular file
reviewed by the pre-check.

**Required correction:** make the Windows read/open operation itself refuse
reparse traversal (for example, a native handle opened with the repository's
existing `FILE_FLAG_OPEN_REPARSE_POINT` pattern, followed by final-path and
reparse validation), or provide an equivalent atomic no-follow primitive. Add a
regression that changes the path between validation and open and asserts the
read is rejected; a pre-existing-symlink test alone is insufficient.

### R3-CAND-018-R2 — Valid JSON with an invalid auth-store shape is silently reset

**Severity: high; violates read-only corruption and explicit-recovery behavior.**

Source anchors:

- `hermes_cli/auth_store.py:560-583`: after successful JSON decoding,
  non-dict values and dictionaries without a dictionary `providers` or
  `credential_pool` (or legacy `systems`) fall through to an empty-store result.
- `hermes_cli/auth_store.py:581-583`: the empty replacement is returned and a
  snapshot is recorded for the original raw bytes.
- `hermes_cli/auth_store.py:586-596`: an ordinary save only requires the
  on-disk bytes to be JSON-decodable; it does not reject the invalid schema.

Live probe 1 wrote `auth.json` containing `[]`. `_load_auth_store()` returned
`{'version': 1, 'providers': {}}`; `_save_auth_store()` then succeeded and
replaced the primary with a newly serialized empty store.

Live probe 2 wrote `{"providers": []}`. The same load returned an empty store,
and ordinary save replaced the primary. No corruption exception, preserved
sidecar, or explicit recovery requirement was raised.

This is data-loss behavior for a valid-JSON but structurally corrupt auth
store. It also means the snapshot/CAS path can authorize an ordinary save after
an invalid shape was normalized away. The repair's strict
`_validate_recovery_store()` validates replacement payloads, but it is not
applied to the ordinary load path, so it does not close this case.

**Required correction:** treat invalid top-level/auth-store shapes as corrupt
and read-only, preserve their exact bytes where possible, raise
`AuthStoreCorruptionError` with the digest, and require the explicit recovery
path before replacement. Add tests for scalar, list, `providers: []`, and
`credential_pool` entries with invalid shapes, asserting the primary bytes and
absence/presence of sidecars according to preservation outcome.

## Positive verification

### Extraction and seam audit

The base-to-owner AST comparison found exact function-body equality for 15 of
17 persistence callables. The two non-identical bodies are the intentionally
repaired `_load_auth_store` and `_save_auth_store` implementations. The
`_OWNER_CALLABLES` map and `_public()` identity checks passed for every owner
callable. The facade binds 16 callables directly to owner objects; the
`_load_global_auth_store` facade wrapper delegates to its extracted owner while
retaining its historical facade cache seam.

The extracted module is 885 lines. The extracted persistence lock, provider
state, snapshot/CAS, sidecar, and recovery paths were read at the candidate
head. Existing adversarial tests cover sidecar collision, sidecar symlink
protection, interrupted publication, changed-source recovery conflict, stale
writer rejection after recovery, schema rejection for explicit replacement,
and the CLI recovery import.

### Focused candidate tests

Canonical runner command, from the candidate root, with
`HERMES_PYTHON=C:/Users/andre/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe`,
`HERMES_TEST_WORKERS=1`, and the desktop/PYTHONPATH variables unset:

```text
./scripts/run_tests.sh \
  tests/hermes_cli/test_auth_store_r3_cand_018.py \
  tests/hermes_cli/test_auth_store_r3_cand_018_adversarial.py \
  tests/hermes_cli/test_auth_store_read_failure.py \
  tests/hermes_cli/test_auth_store_windows_encoding.py
```

Result: **4 files, 27 tests passed, 0 failed**, exit 0, runner wall 53.2s.
This is selection-scoped evidence; it does not cover the two findings above.

### Broader related tests

A valid, explicitly enumerated related selection collected 118 tests and ran in
fresh basetemp `C:/TEMP/hermes-redteam-f751a8c5/verify/r3-cand-018-related-rerun3/pytest-tmp`.
Result: **113 passed, 4 skipped, 1 failed**, exit 1.

The sole failure was:

```text
tests/hermes_cli/test_auth_nous_provider.py::test_shared_store_write_and_read_roundtrip
```

It expected mode `0o600` or `0o644` but observed `0o666` on Windows. The same
test was run in a separate detached worktree at the exact base
`306db2776c6b6f1acc85c31c4dabba3263f0e9fd` and failed with the same assertion
and value, so this failure is a pre-existing Windows-host baseline, not a
candidate regression. It nevertheless means the related selection is not
currently green.

### Static/compile checks

- `python -m py_compile` over the changed Python implementation and test files:
  exit 0.
- `git diff --check 306db..HEAD`: exit 0.
- Windows runtime used for probes: Python 3.11.15, pytest 9.1.1.

## Approval state

Do not approve this repaired candidate. Re-run the focused security suite,
related auth/provider suite, Windows race regression, schema-corruption
regressions, compile check, and diff check after both findings are repaired.
The mode assertion remains a separately documented base/host issue and should
not be misreported as introduced by R3-CAND-018.
