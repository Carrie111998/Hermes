# Status — Harness Hermes Agent

**Última atualização:** 2026-08-10 (fim de sessão — cron-audit + fix Codex gpt-5.5)

## Fase atual

| Fase | Status |
|---|---|
| 1 — Descoberta | ✅ |
| 2 — Proposta | ✅ |
| 3 — Documentação | ✅ |
| 4 — Skills | ✅ patch-guard + test-slice + gateway-ops + credential-audit + **cron-audit** |
| 5 — Sync upstream | ✅ merge 3282 commits + patches reaplicados |
| 6 — Deps dev | ✅ `.venv` + pytest |
| 7 — Smoke pós-sync | ✅ `hermes doctor` exit 0 |
| 8 — Branch local | ✅ `local/harness` |
| 9 — Credenciais | ✅ openrouter/gemini removidos; codex restaurado |
| 10 — Config v34 | ✅ `hermes doctor --fix` |
| Revisão plano | ✅ `REVISAO_PLANO.md` |

## Repositório

| Campo | Valor |
|---|---|
| Path | `C:\Users\User\AppData\Local\hermes\hermes-agent` |
| Branch | `local/harness` @ `6e280e85e6` |
| main upstream | `3139a30e52` (= `origin/main`) |
| Versão pkg | 0.20.0 |
| HERMES_HOME | `%LOCALAPPDATA%\hermes\` |
| Active provider | `openai-codex` |
| venv dev | `.venv\` (pytest 9.1.1) |
| **Uncommitted** | cron-audit script + 5 docs harness |

## Ambiente runtime

| Item | Estado |
|---|---|
| patch-guard | ✅ OK |
| doctor | ✅ OK — config **v34** |
| SQLite | ⚠️ 3.50.4 (WAL-reset bug — `hermes update` pendente) |
| gateway | ✅ telegram + api_server; slack retrying |
| codex | ✅ logged in (`device_code`) |
| cron ticker | ✅ healthy |
| openrouter | ❌ removido |
| gemini | ❌ removido |

## Skills operacionais

| Skill | Script | Skill Cursor |
|---|---|---|
| patch-guard | `harness/scripts/hermes_patch_guard.py` | `~/.cursor/skills/hermes-patch-guard/` |
| test-slice | `harness/scripts/hermes_test_slice.py` | `~/.cursor/skills/hermes-test-slice/` |
| gateway-ops | `harness/scripts/hermes_gateway_ops.py` | `~/.cursor/skills/hermes-gateway-ops/` |
| credential-audit | `harness/scripts/hermes_credential_audit.py` | `~/.cursor/skills/hermes-credential-audit/` |
| **cron-audit** | `harness/scripts/hermes_cron_audit.py` | `~/.cursor/skills/hermes-cron-audit/` |

## Próximo passo (retomada)

1. **Commit** harness na `local/harness` (cron-audit + docs)
2. Definir `model.default: gpt-5.5` em config.yaml
3. `hermes update` (parar gateway antes — SQLite)
4. Pluginizar patches Hermes One (D-004 follow-up)

## Boot da próxima sessão

Ler: **`harness/registros/RETOMADA_SESSAO.md`**

Prompt:

```
Retomar harness Hermes Agent — ler harness/registros/RETOMADA_SESSAO.md e executar próximo passo.
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_patch_guard.py"
```
