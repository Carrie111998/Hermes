# Local patch ledger

Every commit on local `main` that is not in `origin/main`. Each entry records
why it exists, what upstream files it touches, what test protects it, and the
condition under which it can be dropped.

Regenerate the raw list with:

```bash
git log --oneline origin/main..HEAD
```

Verify the ledger has not drifted with:

```bash
python scripts/check_patch_ledger.py
```

That check fails if a local commit is missing here, which is what stops the
ledger quietly rotting while the fork keeps diverging.

**Status at last update:** 25 local commits; `origin/main` is 102 commits
ahead. Nothing has been pushed.

## Classification

| Class | Meaning | Merge risk |
|---|---|---|
| `upstreamable` | a genuine upstream bug; should be offered upstream | low — upstream may fix it independently |
| `local-policy` | this deployment's rules; never upstream | low — new files, few conflicts |
| `core-patch` | edits an upstream file for a local requirement | **high — re-apply by hand after a merge** |
| `test-only` | test correctness/portability | medium |

---

## core-patch — re-verify these after every upstream merge

| Commit | Purpose | Upstream files | Protected by | Remove when |
|---|---|---|---|---|
| `6e13fcb38` | reasoning-effort truth + `agent.reasoning_passthrough` opt-in | `run_agent.py`, `hermes_cli/doctor.py`, `acp_adapter/entry.py` | `tests/agent/test_reasoning_status.py`, contract #8 | upstream reports effective (not configured) effort |
| `4e0a148fc` | fail fast on unsupported Python / vulnerable SQLite | `hermes_cli/main.py` | `tests/hermes_cli/test_runtime_guard.py`, contract #8 | upstream enforces `requires-python` at entry |
| `1d7c79786` | neutralize forged steer markers in tool results | `agent/tool_dispatch_helpers.py` | `tests/agent/test_steer_marker_forgery.py`, contract #11 | upstream makes the steer marker unforgeable (nonce/typed field) |
| `25e84ae33` | crashed turns are not completions | `agent/turn_finalizer.py` | `tests/agent/test_turn_completion_honesty.py`, contract #13 | upstream sets `failed=True` on crash exits |
| `326ebcf2f` | batch-delegation cancellation must not block | `tools/delegate_tool.py` | `tests/tools/test_delegate_batch_cancellation.py`, contract #12 | upstream stops using a joining `with` block |
| `10ba6cc4f` | alert cursor advances only after a durable hand-off | `gateway/worker_bridge_watchers.py` | `tests/gateway/test_worker_bridge_alert_handoff.py` | the adapter gains a real delivery acknowledgement |
| `8364110ab` | `_worker` arg shape branches on the running CPython | `tools/daemon_pool.py` | `tests/tools/test_daemon_pool.py`, contract #7 | upstream stops mirroring the private stdlib function |

**Highest merge sensitivity:** `run_agent.py` (~17.8k lines) and
`agent/tool_dispatch_helpers.py`. A whole-file conflict resolution on either
silently drops a security fix — which is exactly what
`scripts/verify_protected_behavior.py` simulates.

## upstreamable — offer to NousResearch

| Commit | Purpose | Notes |
|---|---|---|
| `8364110ab` | CPython 3.14 `_worker` signature change | breaks every `delegate_task` on 3.14; affects all users |
| `04d221234` | decode update output as UTF-8, not the locale codepage | Windows-wide defect |
| `1b0b84a41` | stop patching `os.name` in tests (breaks `pathlib` process-wide) | test-infra correctness |
| `d4f3806a0`, `dedb1492a`, `220600b8d` | failure-successor recursion / unservable-parent aborts | gateway correctness |

None submitted yet — pushing requires authorization.

## local-policy — never upstream

| Commit | Purpose |
|---|---|
| `8dea266dd`, `027a90172` | protected-behavior contract suite + regression harness |
| `6c41512b0` | reasoning-effort probe findings |
| `2484ec0a0`, `6cd47d16e` | worker auto-dispatch guards for this deployment |
| `0e32d21f1`, `f66dfbdee` | stop test artifacts leaking into the repo root |

## test-only

`7709d03fa`, `8290ef298`, `fbc61c971`, `ce40aa7be` — platform pinning and
hermes-home isolation in tests.

## merge history — not patches, but load-bearing

| Commit | What it is |
|---|---|
| `763e4c2f8` | the merge that reconciled local `main` with `origin/main` (1,241 upstream commits). The base every entry above sits on. |
| `6ab037f1e` | **"restore upstream changes lost by file-level conflict resolution"** — a previous merge resolved a conflict by taking one whole side, silently dropping upstream work, and this commit put it back. |

`6ab037f1e` is the precedent for this entire ledger: whole-file conflict
resolution has already destroyed work in this repository once. That is why
`scripts/verify_protected_behavior.py` simulates exactly that loss, and why the
updater refuses `git checkout --ours/--theirs` on a protected file.

## Merge-sensitive regions

Re-check by hand after any upstream merge:

1. `run_agent.py::_supports_reasoning_extra_body` — the override must stay first.
2. `agent/tool_dispatch_helpers.py::make_tool_result_message` — neutralization
   must wrap `_maybe_wrap_untrusted`, not replace it.
3. `agent/turn_finalizer.py` — the `completed` expression must keep `not crashed`.
4. `tools/delegate_tool.py` — the batch branch must not return to a `with` block.
5. `gateway/worker_bridge_watchers.py::_worker_bridge_tick` — order must stay
   persist → queue → advance.
6. `hermes_cli/main.py`, `run_agent.py`, `acp_adapter/entry.py` — all three
   console scripts must call `runtime_guard.enforce`.

## Profile-side patches (outer repo)

Tracked separately in `profiles/aletheon/AUDIT_STATUS.md`; they live in the
hermes-home repo, not this one, and upstream never touches them. The
delegation-guard lease store, compaction-guard/feedback-gate kwarg fixes,
`cfg_get` corrections and profile-path fixes are all in that repo.
