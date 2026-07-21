# TGG bootstrap gate-preservation — cross-provider review verdict

Reviewed: `e739c0412e97f6a7b3199b3cf05b7db9df452297` ("fix(tgg): preserve live gate across deploy bootstrap") against its parent `3882f6a12` (repo: hermes-pcl). 4 files, +129/-28: `deploy/tgg/christopher/pa-agent.hermes.manifest.json`, `deploy/tgg/christopher/scripts/bootstrap_runtime.sh`, `deploy/tgg/christopher/scripts/ensure_processing_gate.py` (new), `tests/deploy/test_tgg_ensure_processing_gate.py` (new).

## Verdict: CLEAR (with one non-blocking, pre-existing finding below)

### 1. Existing valid gate preserved byte-exact — CLEAR
`ensure_processing_gate()` (`ensure_processing_gate.py:14-22`) reads the file, validates shape, and **returns the parsed dict without rewriting the file at all** when it already exists — no write path executes on the exists-and-valid branch. Test `test_existing_valid_gate_is_preserved_byte_exact` (parametrized `enabled=True/False`) writes a gate with an extra unrecognized key (`marker`) and asserts `path.read_text() == before` post-call — proves no re-serialization occurs, not just that semantic fields match.

### 2. Malformed state refuses, including bool generation — CLEAR
`enabled` must be `isinstance(..., bool)`; `generation` is checked with `type(state.get("generation")) is not int` — this is the correct guard against Python's `bool`-is-`int` subclass trap (`isinstance(True, int) is True`, so an `isinstance` check would silently accept `generation: true`). `type(...) is not int` rejects it. `test_invalid_existing_gate_refuses` covers all four cases explicitly: `enabled="true"` (string), `generation=-1`, `generation="3"`, and `generation=False` — all raise `RuntimeError`. Verified live: 17/17 tests pass.

### 3. Missing gate creates disabled/gen0 — CLEAR
Same fail-closed literal as the code this replaced (`enabled: False, generation: 0, source: "ClientAgentDeployment"`), written via `O_CREAT | O_EXCL` (refuses to clobber a concurrently-created file) + `fsync`. `test_missing_gate_is_created_disabled` confirms both in-memory return and on-disk JSON match.

### 4. Bootstrap is truthfully idempotent — CLEAR
`bootstrap_runtime.sh` diff replaces the inline heredoc (which raised `SystemExit` — i.e. **refused deploy outright** — whenever the live gate was `enabled: true`) with a delegated call to `ensure_processing_gate.py`, which now preserves either boolean state. That is the actual fix: deploy no longer fights a live-enabled gate. The manifest's new `"transaction": {"behavior": "idempotent"}` annotation on `bootstrap-hermes-runtime` matches this — reruns are safe (create-once via `O_EXCL`, no-op preserve thereafter, and the rest of the script's `install -m 0640 -o root -g pclaw ...` / `chown` / `chmod` calls are all idempotent filesystem operations). `bash -n` on the full script: clean.

### 5. Quick verifier is read-only — CLEAR
`quick-runtime-invariants` postRestartHook now carries `"transaction": {"behavior": "read-only"}`; the target (`verify_runtime.sh --quick`) is unchanged by this commit — the diff only adds the classification, consistent with the script's actual read-only behavior (invoked identically pre- and post-commit).

### 6. Tests + lint — CLEAR
`uv run pytest -q -n 0 tests/deploy/test_tgg_ensure_processing_gate.py tests/deploy/test_tgg_processing_activation.py`: **17 passed**. `uv run ruff check deploy/tgg/christopher/scripts/ensure_processing_gate.py tests/deploy/test_tgg_ensure_processing_gate.py`: all checks passed. `bash -n deploy/tgg/christopher/scripts/bootstrap_runtime.sh`: clean.

### Non-blocking finding — pre-existing, NOT introduced by this commit
`pcl pa-agent deploy --client tgg --agent christopher --bundle <fresh-bundle-at-e739c0412> --dry-run` refuses: `PA_AGENT_TRANSACTION_HOOK_UNCLASSIFIED — transaction hook configure-outbound-policy must declare read-only, idempotent, or compensated behavior`.

This hook is defined in **`pa-agent.manifest.json`** (repo root), last touched by `eb527576b` on 2026-06-08 ("PA Phase 1: add missing engine files to pa-agent deploy manifest") — a completely separate file from the one `e739c0412` modified (`deploy/tgg/christopher/pa-agent.hermes.manifest.json`). `e739c0412`'s diff does not touch, reference, or interact with `configure-outbound-policy` in any way; a fresh bundle built at `e739c0412` (deployId `tgg-christopher-20260721-115248-a4c3deadee`, registry `gapCount: 0`, `instanceState: live`) reproduces the same hook-classification error that would occur on any bundle built from current `main`, with or without this commit. This is a real, currently-live gap blocking actual `pa-agent deploy` (not `--dry-run`-only — the same validation gates real deploys), but it is out of scope for this commit and does not reflect on the correctness of the gate-preservation fix reviewed above. Recommend a follow-up WB to classify `configure-outbound-policy` (and `daemon-reload`, currently also unclassified) in `pa-agent.manifest.json` before the next real TGG deploy is attempted.

No correctness issues found in the reviewed diff.
