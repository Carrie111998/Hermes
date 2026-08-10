# Status — Harness Hermes Agent

**Última atualização:** 2026-08-10 (fim de sessão — handoff)

## Fase atual

| Fase | Status |
|---|---|
| 1 — Descoberta | ✅ |
| 2 — Proposta | ✅ |
| 3 — Documentação | ✅ |
| 4 — Skills | ✅ patch-guard + test-slice + gateway-ops + credential-audit |
| 5 — Sync upstream | ✅ merge 3282 commits + patches reaplicados |
| 6 — Deps dev | ✅ `.venv` + pytest |
| 7 — Smoke pós-sync | ✅ `hermes doctor` exit 0 |
| 8 — Branch local | ✅ `local/harness` |
| 9 — Credenciais | ✅ openrouter/gemini removidos; codex restaurado |
| Revisão plano | ✅ `REVISAO_PLANO.md` |

## Repositório

| Campo | Valor |
|---|---|
| Path | `C:\Users\User\AppData\Local\hermes\hermes-agent` |
| Branch | `local/harness` @ `6f77797197` |
| main upstream | `3139a30e52` (= `origin/main`) |
| Versão pkg | 0.20.0 |
| HERMES_HOME | `%LOCALAPPDATA%\hermes\` |
| Active provider | `openai-codex` |
| venv dev | `.venv\` (pytest 9.1.1) |

## Ambiente runtime

| Item | Estado |
|---|---|
| patch-guard | ✅ OK |
| doctor | ✅ OK (SQLite, config v34, toolsets opcionais pendentes) |
| gateway | ✅ telegram + slack + api_server connected |
| codex | ✅ logged in (`device_code`, importado ~/.codex/auth.json) |
| openrouter | ❌ removido (4 scopes) |
| gemini | ❌ removido (4 scopes) |

## Skills operacionais

| Skill | Script | Skill Cursor |
|---|---|---|
| patch-guard | `harness/scripts/hermes_patch_guard.py` | `~/.cursor/skills/hermes-patch-guard/` |
| test-slice | `harness/scripts/hermes_test_slice.py` | `~/.cursor/skills/hermes-test-slice/` |
| gateway-ops | `harness/scripts/hermes_gateway_ops.py` | `~/.cursor/skills/hermes-gateway-ops/` |
| credential-audit | `harness/scripts/hermes_credential_audit.py` | `~/.cursor/skills/hermes-credential-audit/` |

## Próximo passo (retomada)

1. **`hermes doctor --fix`** — migrar config v33→v34
2. Skill **`hermes-cron-audit`**
3. Pluginizar patches Hermes One (D-004 follow-up)

## Boot da próxima sessão

Ler: **`harness/registros/RETOMADA_SESSAO.md`**

Prompt:

> Retomar harness Hermes Agent — ler `harness/registros/RETOMADA_SESSAO.md` e executar próximo passo.
