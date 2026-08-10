# Status — Harness Hermes Agent

**Última atualização:** 2026-08-10 11:15 (hermes update + SQLite 3.53.1 confirmado)

## Fase atual

| Fase | Status |
|---|---|
| 1–12 | ✅ (ver histórico) |
| 13 — hermes update / SQLite | ✅ prod `venv` + runtime = **3.53.1**; git already up to date |
| Revisão plano | ✅ `REVISAO_PLANO.md` |

## Repositório

| Campo | Valor |
|---|---|
| Path | `C:\Users\User\AppData\Local\hermes\hermes-agent` |
| Branch | `local/harness` @ `b34f1a28b9` |
| HERMES_HOME | `%LOCALAPPDATA%\hermes\` |
| Active provider | `minimax-oauth` / `MiniMax-M3` |
| Config | **v34** |
| Working tree | limpa após commit docs |

## Ambiente runtime

| Item | Estado |
|---|---|
| patch-guard | ✅ OK |
| SQLite **prod** (`venv` / `.hermes-runtime`) | ✅ **3.53.1** |
| SQLite **dev** (`.venv`) | ⚠️ ainda 3.50.4 — não usar para gateway/doctor operacional |
| gateway | ✅ telegram + api_server + slack; profiles data-analyst + security-auditor |
| cron | ✅ 6 jobs, 0 overdue, ticker healthy |
| PATH `hermes.cmd` | ✅ corrigido → `venv\Scripts\hermes.exe` |

## Skills operacionais

patch-guard · test-slice · gateway-ops · credential-audit · cron-audit

## Próximo passo

1. Sync 5 commits de `origin/main` (patch-guard antes/depois)
2. Opcional: alinhar `.venv` dev ao SQLite seguro
3. Revalidar `avaliacao-agente-dande` (stale `tool_delay`)
4. Pluginizar patches Hermes One (D-004)

## Boot

Ler: **`harness/registros/RETOMADA_SESSAO.md`**
