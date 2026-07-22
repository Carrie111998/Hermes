# TGG bounded backplay — cross-provider review verdict

Reviewed: `5a5d4da4c9f5f713db6de58ea5a1b77923f69075` ("feat(gateway): add bounded capture-only backplay") against parent `a071a6cdcaa115f29f3bbce1cdf74da3bc48ce84` (repo: hermes-pcl). 2 files, +562/-6: `gateway/durable_jsonl_consumer.py`, `tests/gateway/test_durable_jsonl_consumer.py`.

Spec: edna `specs/2026-07-21-tgg-turn-on-backplay-investigation/report.md` §3, §1-2. Parent WB `846c8f04-b3c0-4d5f-900a-16901a8ae6f7` (six DoD criteria) via review WB `7efdecca-df63-4449-830f-3a8479d355c7`.

## Verdict: BLOCKED

Two concrete gaps against the parent WB's own DoD criteria. Everything else checked out — see §3 for what's clear.

## 1. Audit is incomplete on partial-run failure — BLOCKING

DoD criterion 5: *"Audit binds 4 JIDs, cutoff, selected message IDs, pre/post case counts, mutations, failures, zero-real-sends."* This holds only on the success path.

`run_bounded_backplay` (`gateway/durable_jsonl_consumer.py:1921-2050`) accumulates `processed`, `mutations`, and computes `case_after`/`case_count_delta`/`conservation` locally inside the `if not dry_run:` block, but only writes them into the `audit` dict at lines 2025-2040 — reached *only* if every batch in the loop succeeds. The per-batch handler at lines 2015-2017 re-raises after `inbox.finish(batch, status="failed", ...)`; the outer handler at lines 2044-2050 persists `audit` with just `ok: false` and the failure string. If batch N of a multi-batch run fails, batches `1..N-1` already committed real mutations (case DB writes via `process_live_records`, inbox rows flipped to `completed`/`skipped` via `finish_processed_batch`) that never appear in the persisted audit file — no `processed_message_ids`, no `mutations`, no `case_counts_after`/`case_count_delta`, no `conservation` block.

Reproduced independently (not from author claims): seeded 4 pending rows across 2 chats, batch size 2, faked `process_live_records` so batch 1 (amk) succeeds and writes to `case_db`, batch 2 (hg) raises. Result:
- Live DB truth: `cases` table 1→2 rows, inbox `{'completed': 2, 'failed': 2}`.
- Persisted `audit.json`: `ok: false`, `failures: [...]`, `case_counts_before`, `reconciliation` — and **no** `processed_message_ids`, `mutations`, `case_counts_after`, or `conservation` key at all.

This is exactly the report's own bar for this build ("audit completeness" is explicitly one of the properties this review was asked to check). An operator or Teren reading the failure-path audit after a live run has no persisted evidence of which rows/mutations actually happened before the failure — they'd have to re-derive it by hand from the live DBs, which is the condition the audit contract exists to avoid. Row-count conservation is also never checked on this path (the `row_total_after != row_total_before` guard at line 2020 only runs after the full batch loop completes without exception), so the one property most worth proving after a failure is the one left unproven.

Repro script: `/tmp/edna-repro-partial-failure.py` (uses the real `run_bounded_backplay`, `DurableInbox`, and `_message`/JSONL staging path — no hand-rolled reimplementation).

Fix shape (not applied — review-only): capture `processed`/`mutations`/`case_after` into the `audit` dict incrementally per batch (or in a `finally`), not only at the tail of the success path.

## 2. No code-level exclusivity against the ordinary consumer — BLOCKING

Review brief and DoD criterion 5 both name *"live one-shot exclusivity with ordinary consumer paused."* The existing module already has a reusable primitive for exactly this — `SingletonLock` (`gateway/durable_jsonl_consumer.py:202-231`, non-blocking `flock`-backed process singleton guard) — used by the ordinary consumer's `run` subcommand via a required `--lock-file` (`run(...)` at line 1578; subparser arg at line 2069).

`run_bounded_backplay` (lines 1921-2050) never acquires `SingletonLock`, and the `bounded-backplay` subparser (lines 2090-2107) has no `--lock-file` argument at all. Nothing in the new code checks whether the ordinary consumer's lock is currently held, or refuses to start a live (non-dry-run) bounded execution while the ordinary consumer is running. The exclusivity invariant is enforced by zero code — it rests entirely on an operator remembering to keep the demo pause / `pa.enabled` flag on, which is the same "trust operator discipline instead of a guard" shape this build was explicitly built to close for the token-hash, denominator, and orphan checks (all three of which *do* have code-level refusal guards).

Row-level double-claim is still safe at the SQLite layer (`claim()`'s `UPDATE ... WHERE seq=? AND status='pending'` compare-and-swap, `gateway/durable_jsonl_consumer.py:633-647`, would fail loudly rather than double-process a row the ordinary consumer already claimed) — so this is not a data-corruption risk. It is a missing refusal guard for a named DoD property, and the fix is a few lines given the primitive already exists in this file.

## 3. What is CLEAR

- **Window selector / bounded JID+cutoff selection** (`bounded_window`, lines 528-553): filters by chat_id membership and `_record_ingress_timestamp(record) >= cutoff`; cutoff normalization (`_parse_ingress_timestamp`, lines 40-64) correctly handles unix seconds/ms (ms-threshold `>10_000_000_000`) and refuses naive ISO-8601 strings — matches the spec's explicit timezone-aware requirement.
- **Preclaim print ordering**: `preflight` is computed and printed (line 1947) strictly before `assert_bounded_selection`/token check/reconciliation/claim inside the `try:` block — genuinely before any claim, not just before writes.
- **Live denominator equality**: `assert_bounded_selection` (lines 271-289) refuses on count mismatch, duplicate message ids, or any selected row outside the 4 JIDs/cutoff. Verified reachable via the shipped tests (`test_bounded_refusal_guards_cover_window_count_orphan_and_token`) and independently by reading the guard body.
- **Scoped reconciliation + conservation**: `reconcile_window_processing` (lines 545-603) only touches `processing` rows inside the selected window (never the other ~1000 orphaned rows outside the target 4-JID/cutoff window), reconciles against `pa_turns.message_refs_json` by message id, and hard-aborts (`ConsumerError`) if the row total changes across the transaction. `assert_no_window_orphans` runs on the reconciliation's *predicted* status map even in dry-run, so the orphan-refusal check fires before any live claim regardless of mode.
- **Token guard**: `assert_service_token_hash` (lines 73-84) compares SHA-256 hashes of the canonical `.env` value and the running process's `os.environ` value, never stores or prints the raw secret — only the hash lands in the audit (`audit["service_token_hash"]`). Matches "no token leakage."
- **Strictly read-only dry-run**: `DurableInbox(..., read_only=True)` opens via `file:...?mode=ro` + `PRAGMA query_only=ON` (lines 94-121), and `reconcile_window_processing`/the execution loop are both gated on `dry_run` so no write path is reachable. Verified independently, not just via the shipped byte-identity test — read-only SQLite URI mode is a real DB-level enforcement, not just an application-level `if`.
- **Live one-shot capture-only, zero real sends**: no call to `deliver_management_replies` anywhere in `run_bounded_backplay`; `process_live_records` (pre-existing, lines 1043-1135) hardcodes `delivery_mode="capture"` and returns `outbound_sent: 0` unconditionally, and the new code additionally asserts `outbound_sent == 0` per batch (line 1997-1998) as defense in depth. Test `test_bounded_execution_never_calls_delivery` patches `deliver_management_replies` to raise if called and confirms it never fires.
- **FIFO per chat, fixed batch size**: `selected` preserves ascending `seq` order (source query is `ORDER BY seq`); `grouped` per chat preserves that order; slicing `chat_records[start:start+batch_size]` yields fixed-size FIFO batches per chat.
- **Audit binding (success path)**: binds run_id, chat_ids, cutoff, selected_message_ids, per-chat/status counts, reconciliation, service_token_hash, case counts before/after + delta, processed_message_ids, mutations, conservation, zero_real_sends — complete on the happy path (the gap is failure-path only, see §1).
- **Ordinary consumer regression check**: `DurableInbox.__init__`/`connect()` diff (lines 94-121) is behavior-preserving for the non-read-only branch (same WAL/synchronous/foreign_keys pragmas, same schema init); `main()`'s new `bounded-backplay` dispatch branch (line 2124-2125) is additive and does not touch the `run`/`fixture` dispatch paths. No changes to `process_live_records`, `deliver_management_replies`, or the demo-pause/mutation-gate logic described in the spec's §1.
- **Tests**: ran the 4 new tests plus the full `test_durable_jsonl_consumer.py` file independently (`.venv/bin/python -m pytest tests/gateway/test_durable_jsonl_consumer.py`) — 19/19 pass. Ran the full `tests/gateway/` suite — 68 failed / 8 errors, but confirmed by checking out parent `a071a6cdc` into a throwaway worktree and running the same failing tests there: identical failures pre-exist at the parent commit (unrelated pre-existing environment/dependency breakage in `test_status_command`, `test_google_chat`, `test_tts_media_routing`, `test_api_server*` import errors, etc. — none touch `durable_jsonl_consumer` or this diff's surface). Not a regression introduced by this commit.

## Evidence

- Diff read in full: `gateway/durable_jsonl_consumer.py` (+416/-6), `tests/gateway/test_durable_jsonl_consumer.py` (+152).
- Independent test run: `tests/gateway/test_durable_jsonl_consumer.py` 19 passed.
- Full-suite baseline comparison: parent-commit throwaway worktree, same 22-test subset run, same failures — pre-existing, unrelated.
- Independent partial-failure repro: `/tmp/edna-repro-partial-failure.py`, output captured above in §1.
- No live host access, no deploy, no mutation of production state. All checks ran against the local worktree checkout.
