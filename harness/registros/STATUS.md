# Status — Harness Hermes Agent

**Última atualização:** 2026-08-10 (retomada — doctor OK)

## Fase atual

| Fase | Status |
|---|---|
| 1 — Descoberta | ✅ |
| 2 — Proposta | ✅ |
| 3 — Documentação | ✅ |
| 4 — Skills | ✅ `hermes-patch-guard` + `hermes-test-slice` |
| 5 — Sync upstream | ✅ merge 3282 commits + patches reaplicados |
| 6 — Deps dev | ✅ `.venv` + pytest |
| 7 — Smoke pós-sync | ✅ `hermes doctor` exit 0 |
| Revisão plano | ✅ `REVISAO_PLANO.md` |

## Repositório

| Campo | Valor |
|---|---|
| Path | `C:\Users\User\AppData\Local\hermes\hermes-agent` |
| Branch | `main` @ `3139a30e52` (= `origin/main`) |
| Versão pkg | 0.20.0 |
| HERMES_HOME | `%LOCALAPPDATA%\hermes\` |
| venv dev | `.venv\` (pytest 9.1.1) |

## Git working tree

```
 M agent/credential_pool.py       ← patch Hermes One
 M hermes_cli/web_server.py      ← patch Hermes One
?? harness/                       ← docs + scripts (local, untracked)
?? *.bak.patch, *.orig            ← backups manuais
```

**patch-guard:** ✅ OK (retomada 2026-08-10)
**doctor:** ✅ OK — 3 avisos acionáveis (SQLite, config v34, API keys opcionais)

## Skills operacionais

| Skill | Script | Skill Cursor |
|---|---|---|
| patch-guard | `harness/scripts/hermes_patch_guard.py` | `~/.cursor/skills/hermes-patch-guard/` |
| test-slice | `harness/scripts/hermes_test_slice.py` | `~/.cursor/skills/hermes-test-slice/` |

## Decisões confirmadas (D-001..D-006)

- Harness dentro do repo (`harness/`)
- Objetivo: operação **Hermes One** local
- Patches manuais (plugin depois)
- Prioridade: desktop + gateway Telegram
- Windows nativo + Git Bash para testes

## Próximo passo

1. Branch **`local/harness`** — versionar patches + harness (opcional)
2. Skill **`hermes-gateway-ops`** — próxima na fila do catálogo
3. **`hermes doctor --fix`** — migrar config v33→v34 (opcional)

## Boot da próxima sessão

```
@harness-architect mapear C:\Users\User\AppData\Local\hermes\hermes-agent
```
ou ler: **`harness/registros/RETOMADA_SESSAO.md`**
