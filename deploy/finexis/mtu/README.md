# hermes-mtu — Finexis / Melody Tan Unit (MTU) BOR assistant — Telegram pilot

Runtime source-of-truth for the **MTU BOR-generation** PA agent (Melody Tan Unit, Finexis financial advisory), deployed as a **Studio-local hermes pilot on Telegram**. Second Finexis PA deployment; reuses the `amelia-finexis` shared standard. Patterned on `deploy/tgg/christopher/`.

**Status (2026-07-13):** **DEPLOYED + synthetic-E2E tested** on Telegram bot **@pcl_mtu_bor_bot** (READY, not DEBUT). Bot-cap blocker resolved (deleted one retired throwaway, minted the MTU bot). Gateway running + telegram connected; ROP and non-ROP synthetic cases pass through the production stack. See `test-transcripts/2026-07-13-synthetic-e2e.md` + OPS-NOTE. Remaining before a real advisor: Amelia's DEBUT go + Melody's BOR-table confirm + persistence (launchd/VPS).

## What this agent does
Advisor DMs a rough case (existing plan, proposed plan, is-it-a-replacement) → the agent classifies the replacement path, runs the required BOR checks, asks for anything missing, then **drafts a copy-pasteable BOR (Basis of Recommendation)** from the template with the right compliance disclosures inserted. It DRAFTS for advisor review — never advises or signs off compliance. See `mtu_constitution.yaml` + `knowledge/`.

## Files (source-of-truth; deployed to `$HERMES_HOME`)
| File | Role |
|---|---|
| `mtu_constitution.yaml` | PA constitution (CONFIGURE): identity + `bor_generation` job brief. Model = pilot `gpt-5.4-mini` (design intent gemini-3.1-flash-lite; see OPS-NOTE). |
| `config.yaml` | Gateway config: model (openai-direct-primary/gpt-5.4-mini) + `pa.enabled/job_type/constitution_path` + `platforms.telegram`. |
| `SOUL.md` | Generated: the 4 knowledge files concatenated. **This is the load-bearing KB channel** — the constitution's `knowledge:` key is NOT parsed by the engine (verified). |
| `knowledge/` | The 4 source KB files (provenance): BOR checks table, replacement-path taxonomy, draft template, standard disclosures. |
| `scripts/bootstrap_local.sh` | Builds `~/.hermes-mtu` (HERMES_HOME) from this dir + writes `.env` (token + allowlist + OPENAI key sourced from secrets). Idempotent. Secrets never committed. |
| `OPS-NOTE.md` | Deploy specifics + what remains + rollback. |

## Run (once a free bot token is available)
```bash
# 1. token -> ~/.hermes-mtu-token.tmp (chmod 600), then:
deploy/finexis/mtu/scripts/bootstrap_local.sh ~/.hermes-mtu-token.tmp "<advisor_tg_user_id>"
# 2. run (needs network egress):
HERMES_HOME=~/.hermes-mtu ~/pcl-dev/hermes-pcl/.venv/bin/python ~/pcl-dev/hermes-pcl/hermes gateway run
```
Then DM the bot from an allowlisted Telegram account. Full research/recipe: `~/pcl-biz/_agents/edna/specs/2026-07-05-fa-mtu-assistant/deploy-research.md`.

## Safety
- No client PII here. The constitution + knowledge are abstracted (checklist/template/disclosures) — no real case data. Melody's real PDFs stay out of any repo (`client-raw/`, gitignored).
- Secrets (`.env`) live only in `~/.hermes-mtu/`, never committed.
- READY, not DEBUT: first real advisor use is Amelia's gate (WB 16f2b3ae) + Melody's BOR-table confirm.
