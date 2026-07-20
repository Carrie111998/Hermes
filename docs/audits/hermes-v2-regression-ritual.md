# Hermes-v2 Post-`git pull` Regression Ritual

After pulling new commits on `main` (or merging `hermes-v2-work`),
run the regression set for the hermes-v2 Core-Patches in **one
command**:

```bash
cd ~/.hermes/hermes-agent
./venv/bin/pytest -m h61_regression -v
```

Expected: all tests pass (currently 46 passed, 2 skipped — the
skipped ones are tagged `real_concurrent_gate` / `real_agent_prewarm`
which are env-dependent).

## What's covered

The `h61_regression` marker is the umbrella; sub-markers let you
narrow to a single patch if you suspect a specific regression:

| Marker | Patch | Tests |
|---|---|---|
| `h61_regression` | All hermes-v2 Core-Patches | 46 (auto-collected) |
| `h10_regression` | MiniMax-Interleaved-Thinking (H-10) | ~28 |
| `h11_regression` | `THINKING_BUDGET` ultra tier (H-11) | ~10 |
| `h12_regression` | `top_p` passthrough (H-12) | ~6 |

To run only one patch:

```bash
./venv/bin/pytest -m h10_regression -v   # H-10 only
./venv/bin/pytest -m h11_regression -v   # H-11 only
./venv/bin/pytest -m h12_regression -v   # H-12 only
```

## If a regression appears

1. **Identify the patch:** the failure traceback mentions a test
   file under `tests/agent/test_minimax_*`,
   `tests/run_agent/test_thinking_budget_ultra.py`, or
   `tests/agent/test_chat_completions_top_p.py`. Map back to the
   patch table above.
2. **Inspect the diff:** `git log -p <commit-sha>` for the patch.
3. **Rollback option A — selective revert:** if only one patch
   regressed, `git revert <commit-sha>` on `hermes-v2-work`.
4. **Rollback option B — full rollback:** `git reset --hard
   pre-hermes-v2` resets the whole branch to the H-01 baseline
   anchor.
5. **Verify:** re-run `pytest -m h61_regression`. If green, the
   rollback worked.

## Pre-pull baseline tag

The `pre-hermes-v2` git tag is the H-01 baseline snapshot. Use it
as the rollback anchor whenever the hermes-v2 branch gets into a
state that no individual revert can recover.

```bash
git diff pre-hermes-v2 HEAD --stat     # see what changed
git checkout pre-hermes-v2 -- agent/  # restore single-file
git reset --hard pre-hermes-v2          # full rollback (destructive)
```

## Adding new patches to the regression set

When a new `[CORE-PATCH]` lands (e.g. H-60, H-63), add its tests to
the umbrella by:

1. Adding the test file path with `pytestmark = pytest.mark.<N>_regression`
   at module level.
2. Adding `pytestmark = pytest.mark.h61_regression` to the umbrella.
3. Adding `<N>_regression` to the marker list in `pyproject.toml`
   so `pytest -m h61_regression` recognizes the new sub-marker.

This keeps the post-pull ritual a single command even as more
patches accumulate.

## CI integration (future)

The same command is CI-ready:

```yaml
# .github/workflows/hermes-v2-regression.yml
- name: H-61 regression
  run: ./venv/bin/pytest -m h61_regression -v --tb=short
```

Not currently wired into the project's CI — manual invocation is
the contract until H-62 (Ops-Runbook) lands the formal CI config.
