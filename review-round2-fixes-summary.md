# PR #99450 — round-2 review fixes

## Round-2 blockers closed

Seven ways the pre-mutation quiesce could authorize or report a mutation it had
not proven safe, each fixed at the point where the missing proof was assumed:

1. **Ledger discovery fails closed.** `ledger_entries(strict=True)` raises
   `LedgerUnreadable` instead of quarantining and returning `[]`, so a corrupt
   roster and a box with no serve/dashboard backends stop producing the identical
   plan. The strict read deliberately does not quarantine — moving the file aside
   would make the next read a positively-empty roster and turn a fail-closed abort
   into a fail-open retry.
2. **Supervised-stop authority requires proof.** A backend is ours to stop and
   respawn only when `spawner_is_dead()` is `True`. `None` now reads as
   Desktop-supervised and refuses.
3. **Serve/dashboard discovery errors abort** the update rather than degrade it.
4. **Respawn is argv-lossless.** The ledger records `argv_list` (full,
   untruncated) alongside the legacy joined `argv`; `hermes_home` is recorded and
   set in the replacement's environment. A legacy string that cannot be proven to
   round-trip is refused rather than respawned wrong.
5. **Restart-proof is runtime-kind-correct.** Serve/dashboard backends self-stamp
   `code_sha` at registration and the probe reads the replacement's own stamp.
6. **The pre-mutation gate re-inventories** the fleet and fails closed on any
   answer but a clean one; runtimes are matched on forge-proof `(pid, start_time)`.
7. **Pending restart obligations merge** with the undischarged ones already on
   disk instead of overwriting them, and are persisted before the first stop.

launchd jobs are addressed in their own domain (`system` for LaunchDaemons,
`gui/<uid>` otherwise).

## Follow-up: the legacy-argv round-trip check was checking the wrong join

An independent rerun of the seven new suites found a real defect that the
packaging run reported as green:

```
tests/hermes_cli/test_update_respawn_lossless_argv.py::TestAmbiguousLegacyRecordsAreRefused::test_a_quoted_legacy_argv_is_refused
E       assert 3329455 is None
```

**Root cause — production, not test flake.** `_legacy_argv_parts()` in
`hermes_cli/update_cmd.py` refused an ambiguous legacy argv by checking

```python
if shlex.join(parts) != argv.strip():
```

`shlex.join` re-quotes whatever it is handed, so it regenerates a *quoted* string
byte-for-byte and the check passes. That accepts exactly the records it exists to
refuse: `hermes serve --config '/my dir/c.toml'` splits to four tokens, survives
the check, and is handed to `Popen` — a respawn of a command the operator never
ran. The legacy record was produced by `" ".join(sys.argv[:10])`, so a **plain**
join is the only correct inverse; the "quoting is the tell" argument in the
docstring only holds against `" ".join`.

**Why the packaging run missed it.** The bug was masked by ambient state, not by
test ordering. `_respawn_recorded_runtime` also returns `None` when `Popen`
raises `OSError`, and the recorded argv starts with `hermes`. `scripts/run_tests.sh`
builds a hermetic env but forwards the caller's `PATH` verbatim (`PATH="$PATH"`),
so:

* `hermes` not on `PATH` → `FileNotFoundError` → `None` → the assertion passes
  **by accident**, and the broken check is invisible;
* `hermes` on `PATH` (any run with `.venv/bin` activated, e.g. the Nix devShell)
  → `Popen` succeeds, a PID comes back, the test fails — and a real
  `hermes serve --config '/my dir/c.toml'` is launched.

Per-file subprocess isolation in the runner is intact; suite order was never a
factor. The variable was `PATH`.

**Fix.**

* `hermes_cli/update_cmd.py` — compare against `" ".join(parts)`. This only ever
  refuses *more* than before; the fail-closed contract is strengthened, not
  weakened. Quotes and runs of whitespace now both fail the round trip.
* `tests/hermes_cli/test_update_respawn_lossless_argv.py` — new `never_spawns`
  fixture patches `Popen` to raise on the refusal tests, so a refusal must be
  decided *before* anything is launched and the suite answers identically with or
  without `hermes` on `PATH`. Added
  `test_repeated_whitespace_in_a_legacy_argv_is_refused`.

Verified: with the old `shlex.join` check restored and **no** `hermes` on `PATH`,
the hardened test still fails —
`AssertionError: a record that must be refused was spawned: ['hermes', 'serve', '--config', '/my dir/c.toml']`
— so the environment dependency is gone.

## Exact test results

Run through `scripts/run_tests.sh` (per-file subprocess isolation, `TZ=UTC`,
`LANG=C.UTF-8`, `PYTHONHASHSEED=0`). Every suite was run twice: once on a clean
`PATH`, once with `hermes` resolvable ("hostile `PATH`" — an inert shim, so the
repro could not start a real gateway).

| Scope | Clean `PATH` | Hostile `PATH` |
| --- | --- | --- |
| `test_update_respawn_lossless_argv.py` | 13 passed, 0 failed | 13 passed, 0 failed |
| All seven new suites | 77 passed, 0 failed (4.6s) | 77 passed, 0 failed (4.6s) |

Before the fix, on a hostile `PATH`: `1 failed, 11 deselected` —
`test_a_quoted_legacy_argv_is_refused`.

The seven new suites:

| Suite | Tests |
| --- | --- |
| `test_update_ledger_discovery_fail_closed.py` | 11 |
| `test_update_quiesce_gate_recollect.py` | 11 |
| `test_update_respawn_lossless_argv.py` | 13 |
| `test_update_restart_pending_merge.py` | 9 |
| `test_update_runtime_sha_proof.py` | 15 |
| `test_update_serve_supervisor_fail_closed.py` | 6 |
| `test_update_supervised_stop_authority.py` | 12 |
| **Total** | **77** |

Wider regression sweep over every `tests/hermes_cli/test_update*.py` plus
`test_cmd_update.py`, `test_serve_runtime_inventory.py` and
`tests/tools/test_terminal_update_guard.py`:

```
=== Summary: 81 files, 908 tests passed, 0 failed, 9 skipped (100% complete) in 83.0s (16 workers) ===
```

`ruff check` clean on both changed files.

## Head

Branch `task/t_6fab6ce9`, on top of `d401c3610c`
(`fix(update): close the round-2 blockers in the pre-mutation quiesce`):

* `c249d9d687cc92897062521ccfffac2f23ad380a` — `fix(update): check the legacy argv round trip against a plain join`
  (the code and test change all results above were measured against);
* this document, committed on top of it as `docs(update): record the round-2 fix
  results` — which is the branch head.
