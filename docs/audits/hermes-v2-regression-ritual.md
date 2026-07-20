# Hermes-v2 Post-`git pull` Regression Ritual

After pulling new commits on `main` (or merging `hermes-v2-work`),
run the regression set for the hermes-v2 Core-Patches in **one
command**:

```bash
cd ~/.hermes/hermes-agent
./scripts/run_tests.sh -m h61_regression -v
```

Expected: all tests pass (currently 46 passed across four files —
`tests/agent/test_minimax_anthropic_thinking.py`,
`tests/agent/test_chat_completions_top_p.py`,
`tests/run_agent/test_minimax_tool_reasoning.py`,
`tests/run_agent/test_thinking_budget_ultra.py`).

## What's covered

The `h61_regression` marker is the umbrella; sub-markers let you
narrow to a single patch if you suspect a specific regression:

| Marker | Patch | Tests |
|---|---|---|
| `h61_regression` | All hermes-v2 Core-Patches | 46 (auto-collected) |
| `h10_regression` | MiniMax-Interleaved-Thinking (H-10) | 30 |
| `h11_regression` | `THINKING_BUDGET` ultra tier (H-11) | 10 |
| `h12_regression` | `top_p` passthrough (H-12) | 6 |

To run only one patch:

```bash
./scripts/run_tests.sh -m h10_regression -v   # H-10 only
./scripts/run_tests.sh -m h11_regression -v   # H-11 only
./scripts/run_tests.sh -m h12_regression -v   # H-12 only
```

> **H-10 marker coverage:** `tests/run_agent/test_minimax_tool_reasoning.py`
> is double-marked with both `h61_regression` and `h10_regression`
> so the standard `-m h10_regression` lane picks up the
> `_needs_thinking_reasoning_pad()` regression alongside its H-10
> siblings (`tests/agent/test_minimax_anthropic_thinking.py`).
> Without the dual marker a `-m h10_regression` invocation would
> only see 8 tests instead of 30 and silently miss the
> `_needs_thinking_reasoning_pad()` regression. Tests are
> collected/verified via the four-marker-file command, not direct
> `pytest` calls — the count above is the live
> `pytest --collect-only -q` total of those four files filtered by
> the corresponding marker.

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
5. **Verify:** re-run `./scripts/run_tests.sh -m h61_regression`.
   If green, the rollback worked.

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
  run: ./scripts/run_tests.sh -m h61_regression -v --tb=short
```

Not currently wired into the project's CI — manual invocation is
the contract until H-62 (Ops-Runbook) lands the formal CI config.
