# Cross-provider review — TGG outbound policy hook verify-only settlement v2

- **Verdict: CLEAR**
- **Reviewed commit:** `9a57abfb6a468147c36aad73cb2cca22e47c2d17` (`fix(deploy): make outbound policy hook verify-only [WB:d17d42d8]`, branch `worker/d17d42d8-hook-checkonly`)
- **Parent:** `0c601b0993b600e18ec1d170f36a4d6d919d37aa` (`origin/main` head; verified direct parent + ancestor)
- **Reviewer:** edna clone, Claude Fable 5 (cross-provider vs codex maker), session `1e7dadc3`
- **WB:** `e749d63b-6900-48ef-9ed9-8fdba4dfcbed`
- **Date:** 2026-07-22 19:15 SGT

## Scope of change

Single-file diff: `pa-agent.manifest.json` only (`git diff --name-only` parent..reviewed = 1 file).
- `services[0].preRestartHooks[0]`: renamed `configure-outbound-policy` → `verify-outbound-policy-pre-restart`; command gains `--check-only` + explicit `--env-file ×2 / --config-file / --constitution-file / --policy-env-file` args.
- `verifyHooks[7]` (`outbound-policy-check`): gains the same explicit file args (already had `--check-only`).
- Structural JSON comparison with both changed commands + hook name nulled out: **rest of manifest byte-equivalent to parent** (`om==nm` True). No allowlist, file-set, service, or verify-hook changes beyond the two commands.

## Falsifier results

**1. Pre-restart hook performs zero mutation; no unmanaged missing config path — PASS.**
`configure_christopher_outbound.py` (unchanged in this commit) `--check-only` branch calls only `_check_env_file`/`_check_policy_env`/`_check_config`/`_check_constitution` (+ optional `/proc` env read) then `return` — pure reads: no `_backup`, no `write_text`, no `mkdir`. Explicit `--config-file` overrides `DEFAULT_CONFIG_FILES` (which includes the nonexistent `/home/pclaw/.hermes-christopher-tgg-state/config.yaml` that caused the original UNKNOWN_LIVE_STATE incident). That path appears **nowhere** in the reviewed manifest.

**2. Explicit file set matches live TGG runtime — PASS (live-verified on tgg-app-1, not maker self-report).**
- `EXISTS .hermes-christopher-tgg/.env`, `EXISTS .hermes-christopher-tgg-state/.env`, `EXISTS config.yaml`, `MISSING .hermes-christopher-tgg-state/config.yaml`, `EXISTS constitution`, `EXISTS outbound-policy.env` — exactly the brief's claimed state.
- Ran the exact hook command live (repo script piped to VPS, `--check-only`): **"Christopher/TGG outbound policy verified"**, `management_allowed` = the 6 mgmt JIDs, `ops_silent_count=9`. Exit 0.
- Note: `/home/pclaw/apps/hermes-pcl/scripts/deploy/configure_christopher_outbound.py` is absent on the VPS right now (rollback restored pre-candidate state). Not a defect: manifest `files[27]` deploys it, and `transaction.ts:422` confirms preRestartHooks run **post-promotion**, so the script exists when the hook fires.

**3. Management + ops JIDs byte-identical to parent — PASS.**
Regex-extracted per command: pre-restart 6 mgmt + 9 ops identical to parent (order + bytes); verify hook likewise; new pre==verify JID sets. No allowlist widening or narrowing.

**4. Transaction classification `idempotent` truthful — PASS.**
A pure read check is trivially idempotent; `systemctl daemon-reload` is idempotent. Engine treats `read-only` and `idempotent` identically (only `compensated` gets special recovery handling — `transaction.ts:433,445`). Advisory nit, non-blocking: `read-only` would be the more precise class for the check hook; behaviorally equivalent.

**5. Bundle dry-run accepts; no production mutation — PASS.**
`pcl pa-agent bundle --client tgg --agent christopher --repo <detached worktree @ 9a57abf> --dry-run` → `ok:true`, 33 files, registry `gapCount:0`, bundled script sha256 `cba45b1a…` matches `git show <sha>:script | shasum -a 256`. No deploy executed; the only remote action was the read-only check via a `/tmp` copy, removed after.

**Residual-risk check (beyond brief): future deploys won't wedge on the check.** The check-only hook now runs against promoted candidate files, so the policy must be baked into the candidate config/constitution. Verified: `_check_config` + `_check_constitution` PASS against the repo's `deploy/tgg/christopher/config.yaml` and `christopher_tgg_constitution.yaml` at the reviewed SHA. Future deploys pass the hook without mutation; a genuine drift fails the deploy and routes through canonical recovery (restore preimage) — which is exactly the incident fix intent: config can no longer become a third state.

## Verdict

**CLEAR** — `9a57abfb6a468147c36aad73cb2cca22e47c2d17` is safe to merge. The pre-restart hook is verify-only and bounded to the live-verified file set, allowlist is unchanged byte-for-byte, classifications are truthful, and bundle validation accepts.
