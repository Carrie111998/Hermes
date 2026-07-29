# External Review — claude

**Prompt:** You are the blocking cross-provider reviewer for a build-and-hold de-fusion landing reconcile in hermes-pcl.

Review the final stack against these requirements:
1. Branch B replay parametrization pres...

---

BLOCKED

## Blocking

**1. The stack modifies the D1-23 live WhatsApp renderer, which ratified Phase 1 explicitly excluded, and no landing artifact discloses it.**

- `gateway/platforms/whatsapp.py:1043-1048` now prefers `raw.get("_pa_local_time")` in `_build_turn_event`. That is the consumer move D1-23 was deferred for: the removed comment at `gateway/replay.py` (see `evidence/final-stack.patch:107-110`) read *"D1-23 (the live WhatsApp renderer) is intentionally outside this phase. Dual-write its legacy replay keys until that consumer moves."*
- `specs/2026-07-25-pa-defusion-phase01/parity-evidence.md:5,41-42` states D1-23 remains excluded and that the dual-write exists *because* of that exclusion; the prior cross-provider CLEAR (`.../evidence/cross-provider-review.md:15`) rested criterion (2) on `gateway/platforms/whatsapp.py` being **absent from the diff**. This stack removes the dual-write (`gateway/replay.py:547-550`) and compensates by editing that exact file.
- Neither `parity-evidence.md` ("Safety and scope", "Manifest and moved-file check") nor `conflict-resolution-log.md` ("Scope checks", "Explicitly not treated as 2026-07-28 intersections") names `whatsapp.py`, D1-23, or the changed exclusion. The only trace is the oblique phrase "reads neutral keys first with legacy fallback" in the Branch-B row of the conflict log.

Requirement 7 (scope limited to ratified Phase 1, deferrals explicit rather than normalized by prose) is not met as evidenced. The code change itself is minimal and backward-compatible (legacy `_tgg_sgt` fallback retained, covered by `tests/gateway/test_whatsapp_turn_debounce.py:63-80`), so the remedy is either (a) record in `parity-evidence.md`/`conflict-resolution-log.md` that the landing plan authorizes this partial D1-23 reader move, with the prior exclusion superseded explicitly, or (b) restore the dual-write and leave the renderer untouched. If the WB landing plan already ratified it, that plan is not in this repository — I cannot confirm it from the supplied context, and the evidence bundle must say so.

**2. Focused/increment test evidence does not identify what was run.**
`evidence/focused-replay.txt` ("49 passed") and `evidence/increment-intersection-tests.txt` ("191 passed, 9 skipped") contain no command line, no `-k`/path selection, and no nodeids — unlike `evidence/full-pytest.txt:1-9`, which records rootdir, config, and item count. The parity table's "Focused replay PASS" and "2026-07-28 intersection suite PASS across the durable consumer, PA business facts, and Python sandbox surfaces" claims therefore cannot be checked against their artifacts. Re-capture with the invocation header (and, for the intersection suite, the selected paths).

## Verified clean

- **Req 1 (partial):** `_normalize_replay_envelope` (`gateway/replay.py:85-95`) is applied on every non-SQLite load path (`from_messages`, reached by `from_json_path` and inline sources); the SQLite path already emits neutral keys. Legacy readability retained in `_source_ref_from_bridge_message:147` and `_dedup_key_for_message:155`, and explicitly tested via `tests/fixtures/replay/archived-tgg-envelope.json` + `tests/gateway/test_replay_runner.py:735-751`. No remaining `_tgg_*` consumer is orphaned: the only other writers/readers are `scripts/tgg_christopher_hermes_replay.py:1253,1254,1511`, which is self-contained (it builds and reads its own bridge messages) and is a client-plane script.
- **Req 2/3:** `validate_retainable_document` (`tools/pa_business_tools.py:1319`) is a pure rename with unchanged limits, sniffers, MIME sets, and error strings; `validate_tgg_retainable_document:1360` is a delegating facade still exercised by `tests/test_pa_business_facts.py:1088,1091,1116,1129,1143`; `validate_tgg_spreadsheet:1385` and `gateway/durable_jsonl_consumer.py:1677,1687` use the neutral entry point. No stale references to the renamed private constants remain outside `specs/`.
- **Req 4 (CLI):** `--tenant` is genuinely required on all six subcommands — five via `_add_provider_args` (`hermes_cli/replay.py:228-230`), `status` via the new `:389-391` — so the parametrized refusal test is meaningful, and `docs/pa-replay-run-lifecycle.md` now matches for `status`/`dirty`.
- **Req 5:** The same-interpreter comparison is honestly framed. Both reruns used the final worktree's interpreter against their own trees (`baseline-...txt:110` and `final-...txt:109` both show `.../edna-1a61fede-41cb9170/.venv/bin/python`, with baseline test paths under `edna-1a61fede-baseline`), and both land on identical totals (`36 failed, 571 passed, 9 errors`). The failures are environment class: `ModuleNotFoundError: No module named 'acp'`, `systemctl --user` on darwin, keychain `MagicMock` decode. `parity-evidence.md` does not claim green and scopes its conclusion to "no final-stack-only **deterministic** failure" — correct, since only 45 of the 221 broad failures reproduced in isolation on either tree.
- **Req 6:** Diff is the 11 paths claimed; the only new path is the test fixture; no runtime file moved or renamed, so nothing is missing from the manifest. Nothing in the stack touches main, deploy, client hosts, or MTU.

## NOTES (non-blocking)

- `evidence/manifest-regeneration.txt` shows `"check": false, "ok": true` — that is a write-mode run, not proof of "produced no diff". The working tree being clean at HEAD corroborates it, but a `--check` run (or the `git diff --stat` that was actually consulted) would be the direct artifact.
- `tests/gateway/test_replay_orchestrator.py` lost its positive assertion (`args.tenant == "finexis"`) in the parametrization. The refusal path is now much better covered; the binding path is not asserted anywhere in that test.
- `evidence/plane-lint-warn.txt:32` shows the new fixture `tests/fixtures/replay/archived-tgg-envelope.json` itself adds an unsuppressed `client-token :: tgg` finding. The 108/23/85 numbers in the parity table match the artifact exactly, but the stack's own contribution to the new-findings count is not attributed (Phase 1 recorded 98/23/75).
- Dropping `_tgg_source_ref`/`_tgg_sgt` from `_bridge_message_from_log_row` changes `messages_digest` in `ReplayCorpus.manifest()` for bridge-log corpora, so runs started on the Phase-1 code will not verify against this stack. `docs/pa-replay-run-lifecycle.md:53-57` already gives generic "finish or abandon an in-flight run with its original code version" guidance, but it is worded as *"this contract change"* (the Phase-1 tenant/context one); a second sentence naming the envelope change would keep it unambiguous for operators.
- The "Explicitly not treated as 2026-07-28 intersections" list matches what is actually present in the shared plane (`gateway/durable_jsonl_consumer.py:2206,2621,3235,3431,3869,4325`, etc.), and deferral is the conservative direction. I could not verify the "predate the seven increments" claim without git history access.