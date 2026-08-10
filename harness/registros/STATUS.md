# Status — Harness Hermes Agent

**Última atualização:** 2026-08-10 (branch `local/harness` criada)

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
| Revisão plano | ✅ `REVISAO_PLANO.md` |

## Repositório

| Campo | Valor |
|---|---|
| Path | `C:\Users\User\AppData\Local\hermes\hermes-agent` |
| Branch | `local/harness` @ `052f52ab30` (patches + harness commitados) |
| main upstream | `3139a30e52` (= `origin/main`) |
| Versão pkg | 0.20.0 |
| HERMES_HOME | `%LOCALAPPDATA%\hermes\` |
| venv dev | `.venv\` (pytest 9.1.1) |

## Git working tree

```
branch local/harness @ 052f52ab30
 M harness/registros/STATUS.md   ← atualização pós-commit (unstaged)
```

**patch-guard:** ✅ OK
**doctor:** ✅ OK — 3 avisos acionáveis (SQLite, config v34, API keys opcionais)

## Skills operacionais

| Skill | Script | Skill Cursor |
|---|---|---|
| patch-guard | `harness/scripts/hermes_patch_guard.py` | `~/.cursor/skills/hermes-patch-guard/` |
| test-slice | `harness/scripts/hermes_test_slice.py` | `~/.cursor/skills/hermes-test-slice/` |
| gateway-ops | `harness/scripts/hermes_gateway_ops.py` | `~/.cursor/skills/hermes-gateway-ops/` |
| credential-audit | `harness/scripts/hermes_credential_audit.py` | `~/.cursor/skills/hermes-credential-audit/` |

## Decisões confirmadas (D-001..D-006)

- Harness dentro do repo (`harness/`)
- Objetivo: operação **Hermes One** local
- Patches manuais (plugin depois)
- Prioridade: desktop + gateway Telegram
- Windows nativo + Git Bash para testes

## Próximo passo

1. **`hermes doctor --fix`** — migrar config v33→v34 (opcional)
2. Skill **`hermes-cron-audit`** — próxima na fila do catálogo
3. Pluginizar patches Hermes One (D-004 follow-up)

## Boot da próxima sessão

```
@harness-architect mapear C:\Users\User\AppData\Local\hermes\hermes-agent
```
ou ler: **`harness/registros/RETOMADA_SESSAO.md`**
