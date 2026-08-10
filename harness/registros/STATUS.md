# Status — Harness Hermes Agent

**Última atualização:** 2026-08-10 11:50 (sync origin/main + patch-guard OK)

## Fase atual

| Fase | Status |
|---|---|
| 1–13 | ✅ |
| 14 — Sync origin/main | ✅ merge limpo (5 commits desktop) |
| Revisão plano | ✅ |

## Repositório

| Campo | Valor |
|---|---|
| Path | `C:\Users\User\AppData\Local\hermes\hermes-agent` |
| Branch | `local/harness` @ `667c85c773` |
| vs origin/main | 0 atrás · 8 à frente |
| HERMES_HOME | `%LOCALAPPDATA%\hermes\` |
| Active provider | `minimax-oauth` / `MiniMax-M3` |
| Config | **v34** |
| Working tree | limpa (docs sync a commitar se dirty) |

## Ambiente runtime

| Item | Estado |
|---|---|
| patch-guard pré/pós merge | ✅ OK |
| SQLite prod | ✅ **3.53.1** |
| SQLite `.venv` | ⚠️ 3.50.4 (dev only) |
| PATH `hermes.cmd` | ✅ → `hermes.exe` |
| gateway | (não reiniciado neste sync — desktop-only commits) |

## Próximo passo

1. Revalidar `avaliacao-agente-dande` (stale `tool_delay`)
2. Pluginizar patches Hermes One (D-004)
3. Opcional: alinhar `.venv` SQLite

## Boot

Ler: **`harness/registros/RETOMADA_SESSAO.md`**
