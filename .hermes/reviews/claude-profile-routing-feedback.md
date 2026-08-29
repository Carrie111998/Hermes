# Claude push review — #89556 multiplex default home capture

Reviewed: `origin/fix/89556-multiplex-default-home` at `097b92974d` after pulling latest `origin/main` to `4209d371aa`.

## Verdict

LGTM for the targeted #89556 gateway fix. I do not see a blocking correctness issue in the updated approach.

The key correction from the previous profile-routing feedback is present: the default-profile primary handler no longer captures `get_hermes_home()` at handler construction time. It resolves `get_process_hermes_home()` inside the per-event handler and then enters `_profile_runtime_scope(profile_home)`, so a secondary profile contextvar active during adapter setup cannot poison all later primary/default turns.

## What I checked

- `gateway/run.py`
  - `_make_default_profile_message_handler()` now resolves `profile_home = Path(get_process_hermes_home())` inside `_handler(event)`.
  - `get_process_hermes_home()` intentionally ignores the context-local override and resolves from process `HERMES_HOME` / platform default.
  - The scoped call still uses `_profile_runtime_scope(profile_home)`, preserving the existing config/skills/memory/secret scoping seam for multiplexed primary turns.

- `tests/gateway/test_89556_multiplex_default_home_capture.py`
  - Covers the original poisoned-construction hazard: handler factory called while a secondary override is active, then the actual event still sees the process home.
  - Covers per-event process-home re-resolution if `HERMES_HOME` changes in-process.

- `tests/gateway/test_64674_multiplex_primary_token_scope.py`
  - The patched monkeypatch target (`get_process_hermes_home`) matches the new production seam.

## Verification run

```bash
python -m pytest tests/gateway/test_89556_multiplex_default_home_capture.py tests/gateway/test_64674_multiplex_primary_token_scope.py -q
# 8 passed in 1.14s

python -m pytest tests/gateway/test_runtime_config_env_expansion.py -q
# 1 passed in 0.19s

python -m pytest tests/gateway/test_profile_routing.py tests/gateway/test_status_command.py -q
# 21 passed in 0.95s

python -m py_compile gateway/run.py tests/gateway/test_89556_multiplex_default_home_capture.py tests/gateway/test_64674_multiplex_primary_token_scope.py
# passed
```

## Non-blocking merge hygiene

This branch is no longer based on the current `origin/main`; after pulling, `origin/main` is at `4209d371aa`, while the reviewed branch tip is `097b92974d`. Before merge, rebase/merge main and re-run the same focused gateway tests to catch any integration drift. The patch is small and should be straightforward to replay.

## Suggested next step

Proceed after rebasing onto current main and preserving the new regression test. No code changes requested from this review.
