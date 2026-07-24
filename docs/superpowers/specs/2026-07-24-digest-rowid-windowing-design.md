# DigestComposer rowid-windowing — design

**Date:** 2026-07-24
**Status:** Approved (design) → implementation
**Author:** agent session (branch `claude/brave-cerf-f3d8fd`)

## Problem

`DigestComposer.compose()` windows the event bus by timestamp:

```python
# events/subscribers/digest_composer.py:62-63
query_since = since or self._last_digest_at
events = self.bus.query(since=query_since) if query_since else self.bus.query()
```

`EventBus.query(since=...)` (`events/bus.py` ~line 342) emits:

```sql
SELECT * FROM events WHERE timestamp >= ? ORDER BY rowid ASC
```

On the live 420 MB / ~195k-row `event_bus.db`, this plans as `SCAN events` and
runs ~4.3 s warm (worse cold) for a ~1-day, ~3.8k-row window. It is the last
open item from the R61 event-bus performance work; the sibling fixes
(`f50bcdad4` covering index, `7cbf70867` daily ANALYZE, plus `~/.hermes`
`7d55e6b9f`/`d77195986`) shipped on branch `claude/exciting-blackburn-765f75`
and did **not** touch this path — it was deferred because changing the window
touches a live daily digest's semantics.

### Why an index does not fix it

Measured directly on a copy of the live DB:

| Variant | Plan | Warm |
|---|---|---|
| `timestamp >= ?` + `ORDER BY rowid` (current) | `SCAN events` | 4342 ms |
| new `idx_events_ts` + `ORDER BY timestamp` | `SEARCH … USING INDEX idx_events_ts` | 19 ms |
| new `idx_events_ts` + `ORDER BY rowid` | `SCAN events` | 622 ms |
| `rowid > ?` + `ORDER BY rowid` | `SEARCH … USING INTEGER PRIMARY KEY (rowid>?)` | 33 ms |
| `rowid > ? AND rowid <= ?` + `ORDER BY rowid` | `SEARCH … (rowid>? AND rowid<?)` | 26 ms |

Root cause: `ORDER BY rowid ASC` combined with `SELECT *` makes SQLite prefer
the table scan over index-seek-plus-sort. Adding a `timestamp` index is inert
unless you also reorder by `timestamp` — which changes the digest's ordering
column on a live user-facing feed. rowid-space windowing keeps
`ORDER BY rowid ASC` unchanged and is a covering seek.

### Ordering is preserved

Measured across the live ~1-day window: **0 inversions** between
timestamp-ascending order and rowid-ascending order. The digest already emits
`ORDER BY rowid ASC`; rowid-windowing keeps that byte-for-byte. This is a pure
access-path change, not a semantic one.

## Design

Window the digest by rowid instead of timestamp. rowid is a monotonic insert
order that subscriber cursors already use, so this aligns the digest with the
rest of the bus.

### 1. `events/bus.py` — new `query_rowid_range(after, through)`

A distinct, single-purpose method (not new kwargs on `query()`, which has 40+
ad-hoc test callers we must leave untouched):

```python
def query_rowid_range(self, after: int, through: int) -> List[Event]:
    """Events with `after < rowid <= through`, ascending.

    Half-open lower / closed upper bound: `after` is an exclusive
    watermark (the prior digest's high-water rowid), `through` is a head
    snapshot taken before reading so a write landing mid-compose is not
    double-counted next run. Plans as an INTEGER PRIMARY KEY seek.
    """
```

- SQL: `SELECT rowid, * FROM events WHERE rowid > ? AND rowid <= ? ORDER BY rowid ASC`
- Mirrors `query()`'s version-skew tolerance: skip-and-`warn` on rows that
  `_row_to_event` can't parse (protects against the 2026-07-10
  `weekly_analytics_summary` crash class).
- Also add a helper to snapshot the current head:
  `head_rowid() -> int` returning `SELECT MAX(rowid)` (0 when empty), reused by
  both compose and first-run seeding.

### 2. `events/subscribers/digest_composer.py` — rowid watermark

State (`digest_state.json`) gains `last_digest_rowid` alongside the existing
`last_digest_at`.

`__init__` loads it: `self._last_digest_rowid = state.get("last_digest_rowid")`
(may be `None`).

`compose()` becomes:

1. `through = self.bus.head_rowid()` — snapshot head *before* reading.
2. Resolve the floor `after`:
   - if `self._last_digest_rowid is not None`: use it directly.
   - **else (first run after deploy — seed path (b)):** derive the floor from
     the existing timestamp watermark with one bounded lookup —
     `SELECT MIN(rowid) FROM events WHERE timestamp >= last_digest_at` minus 1
     (so that first matching row is included), falling back to `0` when
     `last_digest_at` is absent or matches nothing. This one-time `timestamp`
     lookup is the only remaining timestamp-based access and never repeats.
3. `events = self.bus.query_rowid_range(after, through)`.
4. Persist **both** `last_digest_at` (kept for the gateway restart-merge and
   any external reader) and `last_digest_rowid = through`. Persisting the
   snapshotted head — not "max rowid among returned rows" — makes an empty
   window still advance the watermark.
5. Update in-memory `self._last_digest_rowid = through` and
   `self._last_digest_at = now`.

The existing gateway merge (`gateway_integration.py` ~line 674: reload state,
re-set `fired_digest_keys`, save) already round-trips the full state dict, so
it preserves `last_digest_rowid` with no change needed there — verified: it
does `load_state()` then only mutates `fired_digest_keys`/legacy-key.

`since=` parameter on `compose()` is retained for tests/manual calls; when a
caller passes an explicit `since` (a timestamp), compose derives a one-shot
rowid floor from it via the same seed lookup rather than special-casing.

### 3. Correctness bonus (gap fix)

The current code sets `self._last_digest_at = datetime.now()` **before**
delivery. Any event inserted during compose/deliver with an earlier timestamp
is silently excluded from every future digest. A rowid head snapshot taken
before the read is gap-free by construction: everything with
`rowid > through` is picked up next run regardless of its timestamp.

## Testing

Following the 2026-07-23 rule (assert the index **by name**, and falsify the
plan test by degrading the query):

1. **Plan pinned by name.** `query_rowid_range` on a populated fixture shows
   `SEARCH` + `INTEGER PRIMARY KEY` and **not** `SCAN`. Falsify: a variant that
   reintroduces `timestamp >= ?` must scan — confirm the assertion fails, then
   keep the seek version.
2. **Watermark advance.** Two successive `compose()` calls: the second's window
   excludes rows the first consumed (no overlap, no re-report).
3. **Empty window advances.** `compose()` with no new events still writes
   `last_digest_rowid = head` and reports "no activity"; a subsequent event is
   picked up.
4. **Gap-free.** An event whose timestamp predates `now` but is inserted after
   the head snapshot appears in the **next** digest, not dropped (this fails on
   the old timestamp-watermark code — it's the regression the fix closes).
5. **First-run seed (b).** Empty `digest_state` with only `last_digest_at`:
   floor is derived from the timestamp, first digest keeps its content, and
   `last_digest_rowid` is written for subsequent runs.
6. **State round-trip through the gateway merge** stays intact (existing
   `test_restart_semantics.py` must still pass; extend it to assert
   `last_digest_rowid` survives the reload-merge).

## Blast radius

`digest_composer.py:63` is the only non-test `query(since=...)` caller in the
repo (`rg 'query(since' → 1 hit`). `EventBus.query()` is untouched, so its 40+
test callers are unaffected. New surface: two small `bus.py` methods and the
compose rewrite.

## Non-goals

- No new index, no `ANALYZE` change (those are the shipped R61 siblings; not on
  `main` yet but out of scope here).
- No change to `query()` semantics or signature.
- No change to digest content or ordering (proven identical in-window).

## Repo notes

- Two separate local-only git repos: `~/.hermes/agent-src` (this change) and
  `~/.hermes`. Never push either.
- This worktree (`angry-bell-af62d9`, branch `claude/brave-cerf-f3d8fd`) is at
  `main` tip `f039e007c`, which lacks the R61 sibling index/ANALYZE — do not
  assume `idx_events_priority_ts` or `bus.analyze()` exist here.
