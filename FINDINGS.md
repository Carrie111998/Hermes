# Gauntlet Audit — Gateway/System Log Cleanup

**Type:** audit-verify (post-fix continuous run)
**Goal:** zero ERROR/WARNING noise in gateway + system logs; find every remaining issue.
**Commit under review:** `247a2bdbd` (gateway env strip + skill-scan RLock) + follow-up deep root-cause fix (uncommitted at audit start).

## Bar (falsifiable)

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Zero `kanban dispatcher: tick failed` since 01:52 restart | ✅ | gateway.log: 0 errors on 08-01 after 01:52 (correct date-aware filter); last ever 01:50 pre-restart |
| 2 | Zero `WARNING agent.skill_commands` "already claimed" after new serve backend | ✅ | agent.log: 0 after 04:19:18; last ever 04:18:47 = old process dying |
| 3 | gateway/run.py marker strip correct, commented WHY | ✅ | strip at gateway startup; env verified `HERMES_DELEGATED_CHILD_CONTEXT = NOT SET` on new processes |
| 4 | skill_commands.py RLock correct, no deadlock/recursion | ✅ | 4-thread concurrent scan test: 0 warnings, 342 commands consistent |
| 5 | No new error/warning classes since restart | ✅ | only transient pre-existing "Synthetic event source unresolvable" (seen 07-30, startup wake noise) |
| 6 | Targeted tests pass, no new failures | ✅ | 21 passed / 3 pre-existing inline-shell failures (identical on stashed baseline) |
| 7 | Snapshot root-cause fix (base.py) verified | ✅ | snapshot exclusion suite: 3 passed; new serve backend spawned clean post-kill |
| 8 | Live system clean across ALL surfaces incl. desktop sessions | ✅ | serve backend 10688 (pre-fix, started 07-31 17:57) killed 04:19; desktop auto-respawned 13900/12540; 0 warnings since |

## Findings

### F1 (fixed): Kanban dispatcher false-positive every 60s
- **Root cause:** gateway inherited `HERMES_DELEGATED_CHILD_CONTEXT=1` from desktop session env snapshot; `kanban_db._assert_not_delegated_child_mutation` (line 159) fell back to env var when ContextVar unset, tripping on the gateway's own `connect()` → `_migrate_add_optional_columns()` write.
- **Fix:** `gateway/run.py` strips stale marker at startup; **deep fix:** `tools/environments/base.py` excludes the marker from the shared bash snapshot (root of the leak).

### F2 (fixed): ~200 "already claimed" skill warnings per boot
- **Root cause:** concurrent startup scans raced (`seen_names` call-local, `_skill_commands` process-global, reset per scan); second scanner logged every skill as "claimed by itself".
- **Fix:** `agent/skill_commands.py` RLock-serializes scans; atomic check-then-scan in `get_skill_commands()`.

### F3 (found): stale-process recurrence
- **Root cause:** serve backend PID 10688 (started 07-31 17:57, pre-fix) held old module in memory; every desktop session scanned through it → warnings recurred 03:52/04:07/04:09/04:12.
- **Fix:** killed 10688/15764; desktop auto-respawned clean process (13900/12540); 0 warnings since.

### F4 (environmental, not ours): 3 inline-shell test failures
- `TestInlineShellExpansion` failures identical on stashed baseline; `agent/skill_preprocessing.py` inline-shell subprocess path, Windows-host-bound. Not a regression.

## Critics (deleg_567afc37 — batch errored on owner exit; substance recovered from transcripts)
- task-0 (code review): verified commit diff, consumers, ran stash baseline — confirmed 3 failures pre-existing.
- task-1 (log evidence): confirmed gateway clean post-restart; attributed bursts to serve process; confirmed commit timestamp.
- task-2 (root cause): confirmed serve tree pre-fix; mapped desktop supervision/respawn path.

## Verdict: 10/10
All bar items green after serve-backend restart. Both code layers fixed and committed; live system clean on every surface (gateway + desktop sessions).
