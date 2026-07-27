# Controller acceptance — flash-dispatch SOL-FD repair

**Status:** `approved after re-review`  
**Recorded at:** 2026-07-27T15:58:00Z (approx; post Sol re-review `deleg_cea39dde`)  
**Branch:** `wt/flash-dispatch-sol-repair-v1`  
**HEAD:** `0473e8d`  
**Worktree:** clean  

## Gate path

```text
Terra execute (SOL-FD-001..005)
→ Packet v6
→ Sol review (deleg_fd20833f) → needs_changes (2 major on AC-2)
→ Terra repair (6a73143)
→ Packet v7
→ Sol re-review (deleg_cea39dde) → approve (findings: [])
→ Controller final acceptance (this record)
```

## Accepted Sol findings (first pass) and repairs

| Finding ID | Severity | Repair |
|---|---|---|
| `SOL-AC2-LOCK-SCOPE` | major | Documented that tick recording applies only to lock-acquired passes; lock losers remain no-DB-write with `skipped_locked`; lock test asserts zero `dispatcher_ticks` rows. |
| `SOL-AC2-TICK-ERROR-SURFACE` | major | CLI JSON/human output surfaces `tick_error` (+ `skipped_locked`); `_finalize_tick` failure path tested end-to-end. |

## Evidence the controller relied on

- Sol re-review decision: **approve**, `findings: []` (full text: `/home/allen/.hermes/cache/delegation/subagent-summary-0-20260727_235741_343822.txt`)
- Terra post-repair suite: **538 passed / 0 failed** via `scripts/run_tests.sh` (lock + CLI + db + diagnostics + core)
- Sol independent focused re-run: **12 passed** on lock + CLI passthrough tests
- Sol independent production-path negative: forced tick insert failure → CLI `--json` shows `tick_error`, exit 0, zero durable tick rows
- Packets: `review_readiness_packet.v6.json`, `review_readiness_packet.v7.json` (field_presence PASS)

## Known gaps (accepted, not blockers)

1. Gateway embedded-dispatcher logging does not separately print `tick_error` (CLI + dashboard `asdict` covered; declared in v7).
2. No live dual-dispatcher / multi-board production soak.
3. Retention defaults (14d / 2000 rows) not soak-tested under high-frequency dispatch.

## Non-authorization (explicit)

This acceptance is **branch/worktree quality acceptance only**. It does **not** authorize:

- merge to `main`
- PR publication
- live gateway activation / config change
- deploy or promotion

Those remain separate controller decisions.

## Commit range in scope

```
c413a22 SOL-FD-001
7eecd1d SOL-FD-002
f76a722 SOL-FD-003
205aa41 SOL-FD-004
17b9202 SOL-FD-005
1bc993d packet v6
6a73143 Sol AC-2 repairs
0473e8d packet v7
```
