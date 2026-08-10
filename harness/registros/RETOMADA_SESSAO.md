# Retomada de Sessão — Harness Hermes Agent

**Salvo em:** 2026-08-10 11:50 (sync origin/main + MiniMax + SQLite 3.53.1)  
**Objetivo:** reiniciar Cursor sem perder contexto.

---

## Prompt para colar na nova janela

```
Retomar harness Hermes Agent — ler harness/registros/RETOMADA_SESSAO.md e executar próximo passo.
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_patch_guard.py"
```

CLI no PATH (`hermes`) já aponta para `venv\Scripts\hermes.exe`. Fallback:

```powershell
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\hermes.exe" gateway status
```

---

## Feito nesta sessão (update)

1. Gateways parados (default + data-analyst + security-auditor)
2. `hermes update` — already up to date; trocou temporariamente para `main`; auto-start gateway
3. Branch restaurada: `local/harness` @ `b34f1a28b9`
4. patch-guard ✅
5. Descoberta: **SQLite prod já era 3.53.1** (`venv` + `.hermes-runtime`); aviso do doctor vinha do **`.venv` (3.50.4)**
6. Gateways re-subidos via `venv\Scripts\hermes.exe` — telegram + api_server + slack + 2 profiles
7. cron-audit ✅ 0 overdue

---

## Estado atual

| Item | Valor |
|---|---|
| Branch | `local/harness` @ `667c85c773` (0 atrás / 8 à frente de origin/main) |
| Config | v34 · `minimax-oauth` / `MiniMax-M3` |
| SQLite prod | **3.53.1** |
| Gateway | ✅ PID principal + profiles |
| Cron | ✅ healthy |

### Cron fleet

| Job | Model | Nota |
|---|---|---|
| relatorio-repos-18h | MiniMax-M3 | ok |
| inbox-listar-dande | MiniMax-M3 | pausado |
| cliente-perfil-dande | MiniMax-M3 | ok |
| avaliacao-agente-dande | MiniMax-M3 | last_error stale `tool_delay` |
| v1-inbox-snapshot-30min | gpt-5.5 | ok |
| cerebro-faxina | gpt-5.5 | ok |

---

## Pendências

| # | Item | Prioridade |
|---|---|---|
| 1 | Revalidar `avaliacao-agente-dande` (stale `tool_delay`) | Baixa |
| 2 | Evitar doctor/gateway via `.venv` (SQLite 3.50.4) | Baixa |
| 3 | Fix/revalidar `avaliacao-agente-dande` | Baixa |
| 4 | Pluginizar patches Hermes One (D-004) | Baixa |

---

## Modelos Codex (ChatGPT)

**Usar:** `gpt-5.5`, `gpt-5.4`, `gpt-5.3-codex`, `gpt-5.4-mini`  
**Não usar:** `gpt-5.2-codex`, `gpt-5.1-codex-max`, `gpt-5.1-codex-mini`

---

## Restrições

- Nunca `git add .`
- `hermes update` pode checkoutar `main` — voltar para `local/harness`
- Restart gateway: `--confirm` no gateway-ops
- `hermes.cmd` já corrigido; se voltar a abrir agent chat, apontar de novo para `hermes.exe`
