# Controller acceptance — flash-dispatch SOL-FD repair

**Status:** `approved after re-review`
**Recorded at:** 2026-07-27T15:58:00Z (Sol re-review)
**Digest refresh at:** 2026-07-27T16:49:27Z
**Branch:** `wt/flash-dispatch-sol-repair-v1`
**HEAD:** `876d97afda29f843bd458928692d6ec689334dba`
**Worktree:** clean after packet digest refresh

## Gate path

```text
Terra execute (SOL-FD-001..005)
→ Packet v6
→ Sol review (deleg_fd20833f) → needs_changes (2 major on AC-2)
→ Terra repair (AC2)
→ Packet v7
→ Sol re-review (deleg_cea39dde) → approve (findings: [])
→ Controller final acceptance
→ Resume brief (this session): verified green + refreshed v6 digests only
```

## Accepted Sol findings and repairs

| Finding ID | Severity | Repair |
|---|---|---|
| `SOL-FD-001` | accepted | Scoped exemption semantics; route no longer authorizes controller worker tools |
| `SOL-FD-002` | accepted | Production diagnostics callers wire dispatcher tick reads |
| `SOL-FD-003` | accepted | Lock-acquired passes record ticks; tick_error surfaced |
| `SOL-FD-004` | accepted | Ready age from latest ready-transition events |
| `SOL-FD-005` | accepted | Board-local bounded dispatcher_ticks retention |
| `SOL-AC2-LOCK-SCOPE` | major | Tick recording only on lock-acquired passes; losers skipped_locked + zero rows |
| `SOL-AC2-TICK-ERROR-SURFACE` | major | CLI JSON/human surfaces tick_error |

## Fresh resume evidence

- Suite: **636 passed / 0 failed** via `scripts/run_tests.sh` on enforcement/integration/lock/cli/diagnostics/core/db
- Packet: `review_readiness_packet.v6.json` digests re-resolved to HEAD `876d97afda29`
- Prior Sol re-review decision remains **approve**

## Known gaps (accepted, not blockers)

1. Gateway embedded-dispatcher logging does not separately print `tick_error`.
2. No live dual-dispatcher / multi-board production soak.
3. Retention defaults not soak-tested under high-frequency dispatch.

## Non-authorization (explicit)

This acceptance is **branch/worktree quality acceptance only**. It does **not** authorize merge, PR publication, live gateway activation, config change, deploy, or promotion.

## Commit range in scope

```
abe31ce SOL-FD-001
7172a09 SOL-FD-002
9068493 SOL-FD-003
10a7292 SOL-FD-004
5ea9107 SOL-FD-005
451406c Sol AC-2 repairs
876d97a current HEAD (includes docs + digest-refreshed v6)
```
