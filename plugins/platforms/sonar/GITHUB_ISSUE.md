## Feature request: Native Sonar gateway platform (`sonar-cli`)

### Problem

Today, Sonar + Hermes integration is typically a **standalone bridge** that shells out to `hermes chat -q` per message. That works but:

- No first-class **gateway sessions** (resume/continuity is ad-hoc)
- Duplicate stack when users already run `hermes gateway` for Telegram
- Two places to maintain (skill bridge vs core gateway)

### Proposed solution

Ship a **platform plugin** under `plugins/platforms/sonar/` (same pattern as IRC / ntfy):

- Transport: `sonar-cli listen` / `sonar-cli send`
- Config: `gateway.platforms.sonar.enabled` + `authorized_senders` npub allowlist
- Cron: `deliver=sonar` with `SONAR_HOME_CHANNEL`

### Reference implementation

A complete plugin + tests is ready to contribute via PR:

- `plugins/platforms/sonar/adapter.py`
- `plugins/platforms/sonar/plugin.yaml`
- `plugins/platforms/sonar/README.md`
- `tests/gateway/test_sonar_platform.py`

See `PR_DESCRIPTION.md` in that directory for full PR text.

### External dependency

**sonar-cli** from https://github.com/hedwig-corp/bitchat-to-sonar — Hermes should not vendor the Rust binary; document install + stable JSON line protocol (`sender` / `content` fields).

### Acceptance criteria

- [ ] `hermes gateway` connects Sonar when enabled and allowlist configured
- [ ] Authorized npub DM → agent reply on Sonar
- [ ] Long replies split across multiple DMs (no silent truncation)
- [ ] `pytest tests/gateway/test_sonar_platform.py` passes in CI

### Labels (suggested)

`enhancement`, `gateway`, `plugin`