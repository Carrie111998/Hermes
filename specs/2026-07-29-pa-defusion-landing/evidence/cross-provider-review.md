# External Review — claude

**Prompt:** You are the blocking cross-provider reviewer for a build-and-hold de-fusion landing reconcile in hermes-pcl.

Review the final stack against these requirements:
1. Branch B replay parametrization pres...

---

CLEAR

## NOTES

**Verified against the working tree (not just the patch)**

- Req 1 — `_normalize_replay_envelope` (`gateway/replay.py:85-95`) is applied in `from_messages`, which is the funnel for `from_json_path`, `from_source` inline, and every `ReplayPlan` corpus branch (`gateway/replay.py:671,680,686,695,768`); the SQLite path already emits neutral keys. Legacy input stays readable via `_source_ref_from_bridge_message:147`, `_dedup_key_for_message:155`, and `whatsapp.py:1043-1048`, and is explicitly tested by `tests/fixtures/replay/archived-tgg-envelope.json` + `tests/gateway/test_replay_runner.py:735-751` and `tests/gateway/test_whatsapp_turn_debounce.py:63-80`. The only remaining `_tgg_*` writer is the client-plane `scripts/tgg_christopher_hermes_replay.py:1253-1254,1511`, which reads what it writes and is covered by the fallback.
- Req 3 — `validate_retainable_document` (`tools/pa_business_tools.py:1301-1357`) is a pure rename: identical size limit, macro refusal, extension/MIME sets, octet-stream allowance, sniffers, and error strings. `validate_tgg_retainable_document:1360` delegates and is still exercised at `tests/test_pa_business_facts.py:1088,1091,1116,1129,1143`; `validate_tgg_spreadsheet:1385` and `gateway/durable_jsonl_consumer.py:1677,1687` use the neutral entry point. No stale private-constant references outside `specs/`.
- Req 4 — `--tenant` is required on all six subcommands (five via `_add_provider_args` at `hermes_cli/replay.py:228-230`, `status` at `:389-391`), so the parametrized refusal test is meaningful; `docs/pa-replay-run-lifecycle.md:134-149` matches. Registry dumps are byte-identical at 79 tools; `manifest-regeneration.txt` now carries write, `--check`, and `git diff --exit-code` (rc 0); focused/increment artifacts now carry their invocations.
- Req 5 — Both reruns used `/_worktrees/edna-1a61fede-41cb9170/.venv/bin/python` (baseline log line 110 et al.) against distinct trees (baseline log references `/_worktrees/edna-1a61fede-baseline/...`), and the 36 `FAILED` + 9 `ERROR` sets are line-for-line identical, so `broad-failure-parity.json` is accurate. Failures are environment class (`No module named 'acp'`, `systemctl --user` on darwin, keychain `MagicMock`). No replay/whatsapp-turn/pa-business-facts/durable-consumer node appears in the broad failure population.
- Req 6/7 — 11 paths, one new path (a test fixture), no runtime move or rename, nothing touching main/deploy/client/MTU. The D1-23 supersession is now stated in `parity-evidence.md:23-27` and in the conflict log's Branch-B row and Scope checks, and the pre-existing `TGG_*` env/`/var/lib/tgg-capture`/SQL/allowlist surfaces are named as deferred rather than normalized.

**Non-blocking observations**

1. `evidence/cross-provider-review.md` currently still holds the superseded attempt-1 BLOCKED verdict (byte-duplicated at `cross-provider-review-attempt1.md`), while `parity-evidence.md` says the review is "Pending". Record this verdict at the canonical path so the two artifacts agree.
2. Authorization for the narrow D1-23 reader move is disclosed but not verifiable from this repository. The landing WB that "expressly defines Branch B as D1-17/21/23/24" is not present here, so I confirm the disclosure and the supersession of `specs/2026-07-25-pa-defusion-phase01/parity-evidence.md:41-42`, not the ratification itself.
3. The same-env reproduction artifacts record `nodeid_population=221` but no invocation. 221 selectors produced 607 collected tests (571 passed + 36 failed), which is consistent with 199 exact nodeids plus 22 whole modules that error at collection — a superset, which is fine, but the artifacts do not say so. Relatedly, `full-failed-nodeids.json` holds 224 entries, of which lines 2-4 are log lines rather than nodeids; presumably those three were filtered to reach 221.
4. 176 of the 221 broad failures reproduce on neither tree in isolation, so they remain unattributed order/parallelism effects, and the broad suite was run once on the final stack only. `parity-evidence.md` correctly scopes its conclusion to "no final-stack-only **deterministic** failure" — no overclaim, but a final-only failure requiring full-suite parallel load would not have been caught.
5. `replay-run status` requires `--tenant` but `cmd_replay_run` never reads it (`hermes_cli/replay.py:408-417`) and does not compare it to the loaded manifest's tenant. Asserting equality would make the flag a real gate rather than friction.
6. Envelope normalization is corpus-scoped: a plan with an inline `messages` array bypasses `ReplayCorpus` entirely (`gateway/replay.py:646,700`) and so skips `_normalize_replay_envelope` (and ordering/dedup — pre-existing). Harmless today because every downstream reader keeps a legacy fallback; it becomes real the moment those fallbacks are removed.
7. plane-lint attribution: the new fixture registers its own finding (`plane-lint-warn.txt:32`) and the new debounce test adds another (`:57`). Totals moved 98→108 with the baseline still 23, but most of that delta comes from increments already on main; the stack's own contribution is not broken out. A neutral fixture filename would drop the `path` component of that finding — the `_tgg_*` keys inside must stay.
8. `tests/gateway/test_replay_orchestrator.py:296-306` asserts refusal only; the positive binding assertion the old test carried (`args.tenant == "finexis"`) no longer exists for `replay-run`. Binding is still asserted for the `replay` parser at `:337`.
9. `bundle-dry-run.json` records `source.commit` `28f2048bb1`, two commits behind HEAD. `docs/pa-replay-run-lifecycle.md` is not in `pa-agent.hermes.manifest.json` and the two later commits are docs/evidence, so the 578-file runtime set holds — but re-running the dry-run at HEAD would remove the need for that inference.