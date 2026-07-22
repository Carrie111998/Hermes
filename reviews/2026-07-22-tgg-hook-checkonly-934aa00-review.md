# Cross-provider review: TGG outbound policy hook verify-only

- **Verdict: CLEAR**
- Commit under review: `934aa009c92175c418c705327e1ac00d60fb0a1e` (`origin/worker/d17d42d8-hook-checkonly`)
- Supersedes in-flight review of `9a57abfb6a468147c36aad73cb2cca22e47c2d17` (its direct parent on the same branch) per spawner-verified PHAROS_DIRECTIVE; delta between the two is a single line (`idempotent` → `read-only` on the check hook), so all findings were re-anchored to `934aa009c`.
- Parent lineage: `9a57abfb6` → `0c601b0993b600e18ec1d170f36a4d6d919d37aa` (`origin/main`)
- Reviewer: edna review clone (claude), session `7608c139`, 2026-07-22 19:15 SGT
- WB: `e76e4842-c407-42a4-9ecb-af2e96803657`

## Scope

Incident context: deployment `tgg-christopher-20260722-105719-d5bf2bc56e`
promoted candidate files, then the mutating `configure-outbound-policy`
preRestartHook rewrote managed `config.yaml` (comment-stripping via YAML
round-trip) producing a third state, and failed on the nonexistent
`/home/pclaw/.hermes-christopher-tgg-state/config.yaml` default path.
Canonical rollback initially refused `UNKNOWN_LIVE_STATE`; recovery required
manual restore of the single drifted file. The commits under review convert
the preRestartHook to explicit bounded `--check-only` (classified
`read-only`) and add the same explicit file bounds to the
`outbound-policy-check` verify hook.

## Findings against the five falsifiers

### 1. preRestartHook performs zero mutation; no unmanaged missing path — CONFIRMED

Read `scripts/deploy/configure_christopher_outbound.py` at the review commit.
The `--check-only` branch of `main()` calls only `_check_env_file`,
`_check_policy_env`, `_check_config`, `_check_constitution` (and optionally
`_check_systemd_process_env`, not enabled here). All are pure reads
(`read_text`, `yaml.safe_load`, `systemctl show`, `/proc` read); no
`write_text`, no `mkdir`, no `_backup` on any check path. Explicit
`--config-file` populates `args.config_file`, so `DEFAULT_CONFIG_FILES`
(which contains the nonexistent `-state/config.yaml`) is unreachable —
`config_files = args.config_file or DEFAULT_CONFIG_FILES`.

### 2. Exact file args match live TGG runtime — CONFIRMED live

SSH `tgg-app-1` (resolved via `pcl service locate --system christopher`),
2026-07-22 ~19:10 SGT:

- `EXISTS /home/pclaw/.hermes-christopher-tgg/.env`
- `EXISTS /home/pclaw/.hermes-christopher-tgg-state/.env`
- `EXISTS /home/pclaw/.hermes-christopher-tgg/config.yaml`
- `EXISTS /home/pclaw/.hermes-christopher-tgg/christopher_tgg_constitution.yaml`
- `EXISTS /home/pclaw/.hermes-christopher-tgg/outbound-policy.env`
- `MISSING /home/pclaw/.hermes-christopher-tgg-state/config.yaml` (as expected;
  no hook references it any more)

### 3. Management and ops JIDs byte-identical to parent — CONFIRMED

Programmatic extraction of `--management-chat` / `--ops-chat` sequences from
both command sites in `pa-agent.manifest.json` at `0c601b099` (main) and the
review branch: 6 mgmt + 9 ops JIDs, order-identical, byte-identical in all
four (2 sites × 2 commits) occurrences. The `9a57abfb6..934aa009c` delta
touches only the `transaction.behavior` line — commands untouched. The only
other manifest references to the script are the file-sync
`sourcePath`/`targetPath` pair. No allowlist change.

### 4. Transaction classifications truthful — CONFIRMED

`verify-outbound-policy-pre-restart` is now `{"behavior": "read-only"}` —
the precise schema label ("observes state without mutation",
`manifest-schema.md`), confirmed truthful from source (finding 1).
`daemon-reload` stays `idempotent`, which is correct (it mutates systemd's
in-memory unit graph but is convergent on re-run). Engine treatment: marshal
`src/lib/pa-agent/transaction.ts` branches only on `compensated`;
`read-only` is a valid classification per `types.ts:19` and accepted by the
gate (finding 5).

### 5. Bundle dry-run accepts; no production mutation — CONFIRMED

`pcl pa-agent bundle --client tgg --agent christopher --dry-run` from a
detached worktree at the exact review SHA `934aa009c`: `ok:true`, 33 files,
`gapCount:0`, `source.commit=934aa009c…`, preRestartHooks materialized as
`verify-outbound-policy-pre-restart {behavior: read-only}` +
`daemon-reload {behavior: idempotent}`. No registry event, no files written
to production.

## Forward check: will the hook pass on next deploy?

Not in the falsifier list but verified to close the redeploy question.
preRestartHooks run post-promotion (`transaction.ts:422` comment), so the
hook sees promoted candidate files:

- Candidate `deploy/tgg/christopher/config.yaml` + constitution: ran the
  script's own `_check_config` / `_check_constitution` against the worktree
  candidates locally — both PASS.
- Live env surfaces (not in bundle, untouched by deploy): all six expected
  key/value checks PASS on `tgg-app-1` (`.env`, `-state/.env`,
  `outbound-policy.env`; `WHATSAPP_OUTBOUND_DISABLED=false`,
  `WHATSAPP_OUTBOUND_ALLOWED_CHATS=<6 mgmt JIDs>`).

Note: the deployed script is currently absent on the VPS
(`/home/pclaw/apps/hermes-pcl/scripts/deploy/` empty) — expected, since
recovery restored the pre-deploy preimage; promotion re-places it before the
hook fires.

## Verdict

**CLEAR** for `934aa009c92175c418c705327e1ac00d60fb0a1e`. No maker code
edited. No production mutation performed during review (all remote actions
read-only).
