# hermes-mtu — Ops Note (Telegram pilot)

**Owner:** edna · **Sponsor:** amelia · **WB:** ad8e3abd · **Built:** 2026-07-13

## Deploy specifics
| Item | Value |
|---|---|
| **Host** | Studio-local pilot (`HERMES_HOME=~/.hermes-mtu`), runs on the `~/pcl-dev/hermes-pcl` main checkout venv. Reversible; Carbon target is client-VPS (Amelia's call — flagged). |
| **Runtime** | hermes `gateway run` (interactive two-way). Isolated state at `~/.hermes-mtu/{state.db,sessions/}`. |
| **Channel** | Telegram (native hermes platform). Thin/swappable — constitution+knowledge are channel-agnostic; WA can be added later by enabling the WA platform, no rebuild. |
| **Model** | `gpt-5.4-mini` via `openai-direct-primary` (OPENAI_API_KEY). **FLAGGED swap** from the constitution's design intent `gemini-3.1-flash-lite`: (a) no gemini key on this Studio, (b) christopher runs gpt-5.4-mini after dropping gemini for extraction accuracy. Not silent — revert by setting `model.provider: gemini` once a shared PA gemini key is on the host. |
| **Bot token** | `~/.hermes-mtu/.env` → `TELEGRAM_BOT_TOKEN` (chmod 600, NOT committed). **Currently unresolved — see What remains.** |
| **Advisor allowlist** | `~/.hermes-mtu/.env` → `TELEGRAM_ALLOWED_USERS`. Pilot self-test uses the controlled tester account `276672685`. Melody's / Amelia's Telegram user IDs must be added before they can message it (DMs bypass `allowed_chats`; the user allowlist is the gate). |
| **engagement_id** | TBD — set at DEBUT (constitution `client.engagement_id`). Ties to the `amelia-finexis` Finexis engagement (deal `179de144`). |
| **Escalation contact** | Melody Tan (constitution `escalation.escalation_to`). |

## What remains (before a real advisor can use it)
1. **[BLOCKER — needs a human decision] A free Telegram bot.** Teren's Telegram account is at the hard **20-bot cap**. New-bot creation via BotFather is refused. Reuse is unsafe to auto-pick: a bot's freeness can't be verified without `getUpdates`, which disrupts whatever is polling it — the first candidate (`@pcl_tggtest_bot`, which looked dead: no webhook, 0 pending, no local binding) turned out to be **actively long-polled by another consumer**. Need one of: (a) Teren deletes one genuinely-dead bot → I mint `@pcl_mtu_bor_bot`; or (b) Teren names an existing bot safe to repurpose; or (c) a bot token from another Telegram account with free slots. Once provided: `bootstrap_local.sh <token-file> <allowlist>` + `gateway run` → live + tested in minutes.
2. **[Amelia gate]** DEBUT go (first real advisor) — WB 16f2b3ae.
3. **[Melody gate]** Confirm/edit the BOR table (`knowledge/bor-required-checks.yaml`) + approved disclosure wording.
4. Set `engagement_id`, add Melody's/advisors' Telegram IDs to the allowlist.
5. (Optional) launchd unit `com.pcl.hermes-mtu` for persistence (currently foreground/nohup pilot run).
6. (Optional) register hermes-mtu in the PA runtime registry so `pcl service locate --system mtu` resolves — do once it's persistently running.

## Rollback
| Change | Revert |
|---|---|
| `~/.hermes-mtu/` (config + state + .env) | `rm -rf ~/.hermes-mtu` |
| Running gateway | `kill <pid>` / `pkill -f "hermes gateway run"` (with HERMES_HOME=~/.hermes-mtu) |
| Deploy artifacts (this dir) | `git revert <sha>` on the worker branch |
| (If a bot is reused) display-name rename | BotFather `/setname` back to original |
No data-loss risk: synthetic-only pilot; no client data ingested.

## Verification status
- Stack: gateway boots, loads MTU constitution + SOUL KB, authenticates a Telegram token, reaches `getUpdates` — **validated** (blocked only on the token conflict above).
- E2E BOR flow (ROP + non-ROP synthetic): **pending a free bot** (DoD #2, #3, #5).
