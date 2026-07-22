# TGG bounded backplay — cross-provider review verdict

Reviewed: `5a5d4da4c9f5f713db6de58ea5a1b77923f69075` ("feat(gateway): add bounded capture-only backplay") against parent `a071a6cdcaa115f29f3bbce1cdf74da3bc48ce84` (repo: hermes-pcl). 2 files, +562/-6: `gateway/durable_jsonl_consumer.py`, `tests/gateway/test_durable_jsonl_consumer.py`.

Spec: edna `specs/2026-07-21-tgg-turn-on-backplay-investigation/report.md` §3, §1-2. Parent WB `846c8f04-b3c0-4d5f-900a-16901a8ae6f7` (six DoD criteria) via review WB `7efdecca-df63-4449-830f-3a8479d355c7`.

## Round 1 verdict: BLOCKED (superseded below)

Two concrete gaps against the parent WB's own DoD criteria were found against `5a5d4da4c`. See §1-§3 below for the original findings and what was already clear.

## Round 2 — correction diff `f0b08c54d..479005e8e` — Verdict: CLEAR

Correction commit `479005e8e2733ef93a56ac1fb64534b796421ead` ("fix(tgg): close bounded replay audit and lock gaps"). 3 files, +389/-110: `gateway/durable_jsonl_consumer.py`, `tests/gateway/test_durable_jsonl_consumer.py`, `specs/2026-07-22-tgg-bounded-backplay/dry-run-evidence.json`.

Both round-1 blockers are fixed. Independently verified — not from author claims or the shipped tests alone:

**Finding 1 (audit incomplete on partial failure) — CLOSED.** `run_bounded_backplay` (now `gateway/durable_jsonl_consumer.py:1969-2050`ish, see `git show 479005e8e`) restructures `processed`/`mutations` as list objects referenced directly by the `audit` dict from construction (not copied in only on success), so in-loop `mutations.append(...)`/`processed.extend(...)` update the same object the audit will persist even if a later batch fails. The per-batch failure handler now also appends a `{"status": "failed", "chat_id", "message_ids", "error_class"}` entry. The outer exception handler recomputes `case_counts_after`/`case_count_delta`, a new `business_mutations` (structural `ps_audit_log` row delta via new `_business_audit_cursor`/`_business_audit_delta`, lines 1921-1955ish), and `conservation` (now checking the actual before/after row total rather than assuming `preserved: true`) directly from the live DBs before persisting.

Re-ran my own independent partial-failure repro (not the shipped tests) against the new code: seeded 2 chats × 2 messages, batch 1 (amk) succeeds and writes a `cases` row + a `ps_audit_log` row, batch 2 (hg) raises `RuntimeError`. Persisted `audit.json` now correctly shows `ok:false`, `processed_message_ids:["m-amk@g.us-0","m-amk@g.us-1"]`, `mutations:[{status:completed,...},{status:failed,chat_id:hg@g.us,error_class:RuntimeError}]`, `case_counts_after:{cases:2,ps_audit_log:1}`, `case_count_delta:{cases:1,ps_audit_log:1}`, `business_mutations:[{action:update,target_kind:case,target_id:case-1,...}]`, `conservation:{preserved:true}` — matching live DB truth exactly (`cases` 1→2, inbox `{completed:2, failed:2}`).

`ps_audit_log` is a real table, not invented: cross-checked against this workstream's own investigation evidence at `~/pcl-biz/_agents/edna/specs/2026-07-21-tgg-turn-on-backplay-investigation/evidence/backup-restore.json:36` (`"ps_audit_log": 3856` from the restore-verified tenant DB snapshot) and `~/pcl-biz/_agents/edna/knowledge/tgg-systems-runtime.md:95` (listed as an independent/keep table). The submitted `dry-run-evidence.json`'s `business_mutation_count:0`/`case_delta_nonzero:{}` under dry-run is consistent with a real successful query against a real table returning nothing new (not a swallowed schema error — an `OperationalError` on a bad column/table name would have raised uncaught and produced `ok:false`, not the observed `ok:true`).

**Finding 2 (no exclusivity guard) — CLOSED.** `run_bounded_backplay` now wraps its entire body in `with lock_context:`, where `lock_context = contextlib.nullcontext() if dry_run else SingletonLock(Path(args.lock_file).resolve())` — the exact `SingletonLock` class (`gateway/durable_jsonl_consumer.py:202-231`) the ordinary consumer's `run` subcommand already uses. New required `--lock-file` argument added to the `bounded-backplay` subparser. Dry-run correctly stays lock-free (read-only, no exclusivity needed); live execution now acquires the same non-blocking `flock`-backed singleton the ordinary consumer holds.

Independently reproduced (not the shipped test): held a `SingletonLock` on a given path from a separate context (simulating the ordinary consumer running), then invoked `run_bounded_backplay` with `--lock-file` pointed at that same path — it raised `ConsumerError: consumer singleton already holds ...` before any claim, audit-file write, or inbox mutation (`audit.json` never created, inbox counts unchanged).

**Operational note, not a code defect:** this guard is only correct if the deployment runbook passes the *same* `--lock-file` path to both `run` and `bounded-backplay`. The code enforces mutual exclusion on whatever path it's given; it can't verify the operator picked the same one. Worth calling out explicitly in the exact-conditional-enable-sequence runbook (report §"Flip package status") when this gets wired into the real deploy, but it's a runbook-parameter concern, not something this diff can or should encode further.

**Tests.** Re-ran `tests/gateway/test_durable_jsonl_consumer.py` independently at sane concurrency (`-o addopts="" -n 8`): 21/21 pass, matching the author's claim. At the default `addopts` (`-n auto`, which resolves to ~28 workers on this shared, heavily-loaded Mac Studio), `test_bounded_dry_run_is_read_only_and_predicts_reconciliation` flakes on a byte-identity assertion — but this reproduces identically on the *original* pre-correction commit `5a5d4da4c` too (checked out into a throwaway worktree, ran 3×, failed 2/3 at `-n auto`, passed 21/21 consistently at `-n 1/2/4/8/16`), so it's pre-existing high-parallelism test-environment sensitivity on a shared machine, not something this correction introduced or should be blocked on. No new regressions found in the diff beyond what round 1 already cleared (window selector, denominator/orphan/token guards, read-only dry-run, capture-only zero-send path, FIFO/batching, ordinary-consumer code paths untouched).

## Round 1 findings (for the record)

### 1. Audit is incomplete on partial-run failure — BLOCKING (fixed in round 2)

DoD criterion 5: *"Audit binds 4 JIDs, cutoff, selected message IDs, pre/post case counts, mutations, failures, zero-real-sends."* This held only on the success path in `5a5d4da4c`.

`run_bounded_backplay` (`gateway/durable_jsonl_consumer.py:1921-2050` in `5a5d4da4c`) accumulated `processed`, `mutations`, and computed `case_after`/`case_count_delta`/`conservation` locally inside the `if not dry_run:` block, but only wrote them into the `audit` dict at lines 2025-2040 — reached *only* if every batch in the loop succeeded. The per-batch handler at lines 2015-2017 re-raised after `inbox.finish(batch, status="failed", ...)`; the outer handler at lines 2044-2050 persisted `audit` with just `ok: false` and the failure string. If batch N of a multi-batch run failed, batches `1..N-1` already committed real mutations that never appeared in the persisted audit file.

Reproduced independently (not from author claims): seeded 4 pending rows across 2 chats, batch size 2, faked `process_live_records` so batch 1 (amk) succeeds and writes to `case_db`, batch 2 (hg) raises. Result: live DB truth showed `cases` table 1→2 rows, inbox `{'completed': 2, 'failed': 2}`; persisted `audit.json` showed only `ok: false`, `failures`, `case_counts_before`, `reconciliation` — no `processed_message_ids`, `mutations`, `case_counts_after`, or `conservation` key.

Repro script: `/tmp/edna-repro-partial-failure.py`.

### 2. No code-level exclusivity against the ordinary consumer — BLOCKING (fixed in round 2)

Review brief and DoD criterion 5 both named *"live one-shot exclusivity with ordinary consumer paused."* The module already had a reusable primitive for exactly this — `SingletonLock` (`gateway/durable_jsonl_consumer.py:202-231`) — used by the ordinary consumer's `run` subcommand via a required `--lock-file`. `run_bounded_backplay` in `5a5d4da4c` never acquired it, and the `bounded-backplay` subparser had no `--lock-file` argument at all. Row-level double-claim was still safe at the SQLite layer (`claim()`'s compare-and-swap), so this was not a data-corruption risk — it was a missing refusal guard for a named DoD property.

### 3. What was CLEAR in `5a5d4da4c`

- **Window selector / bounded JID+cutoff selection**: filters by chat_id membership and `_record_ingress_timestamp(record) >= cutoff`; cutoff normalization correctly handles unix seconds/ms and refuses naive ISO-8601 strings.
- **Preclaim print ordering**: preflight computed and printed strictly before any refusal check or claim.
- **Live denominator equality**: `assert_bounded_selection` refuses on count mismatch, duplicate message ids, or any selected row outside the 4 JIDs/cutoff.
- **Scoped reconciliation + conservation**: `reconcile_window_processing` only touches `processing` rows inside the selected window, reconciles against `pa_turns.message_refs_json` by message id, hard-aborts on row-total change. Orphan-refusal fires on the *predicted* status map even in dry-run.
- **Token guard**: SHA-256 hash comparison, no raw secret ever stored/printed — only the hash lands in the audit.
- **Strictly read-only dry-run**: DB-level enforcement (`mode=ro` URI + `PRAGMA query_only=ON`), not just an application-level `if`.
- **Live one-shot capture-only, zero real sends**: no call to `deliver_management_replies` anywhere in the new path; `process_live_records` hardcodes `delivery_mode="capture"`; test confirms `deliver_management_replies` never fires.
- **FIFO per chat, fixed batch size**: confirmed via seq-ordering through the whole pipeline.
- **Ordinary consumer regression check**: `DurableInbox.__init__`/`connect()` diff behavior-preserving for the non-read-only branch; new dispatch branch additive only.

## Evidence

- Round 1 diff read in full: `gateway/durable_jsonl_consumer.py` (+416/-6), `tests/gateway/test_durable_jsonl_consumer.py` (+152).
- Round 2 diff read in full: `gateway/durable_jsonl_consumer.py` (+359/-110 net), `tests/gateway/test_durable_jsonl_consumer.py` (+81), `specs/2026-07-22-tgg-bounded-backplay/dry-run-evidence.json` (+59, author-submitted, cross-checked against independent investigation evidence).
- Independent test runs: `tests/gateway/test_durable_jsonl_consumer.py` 19/19 (round 1), 21/21 at sane concurrency (round 2).
- Full-suite baseline comparison: parent-commit throwaway worktree, same 22-test subset, same pre-existing failures — unrelated to this file.
- Independent partial-failure repro (round 1, reproduces the gap): `/tmp/edna-repro-partial-failure.py`.
- Independent partial-failure repro (round 2, confirms the fix): `/tmp/edna-repro-partial-failure-v2.py`.
- Independent lock-exclusivity repro (round 2, confirms the fix): `/tmp/edna-repro-lock.py`.
- Independent dry-run byte-identity flake isolation: confirmed pre-existing on parent-of-round-1 commit via throwaway worktree at `-n auto`; clean 21/21 at `-n 1/2/4/8/16` on both commits.
- No live host access, no deploy, no mutation of production state at any point in either round. All checks ran against local worktree checkouts.
