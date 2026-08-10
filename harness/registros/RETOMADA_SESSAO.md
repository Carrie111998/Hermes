# Retomada de Sessão — Harness Hermes Agent

**Salvo em:** 2026-08-10 (fim de sessão — cron-audit + v34 + fix Codex)  
**Objetivo:** reiniciar Cursor em outra janela sem perder contexto.

---

## Prompt para colar na nova janela

```
Retomar harness Hermes Agent — ler harness/registros/RETOMADA_SESSAO.md e executar próximo passo.
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_patch_guard.py"
```

---

## O que foi feito nesta sessão (completo)

### Harness + config
1. **patch-guard** ✅
2. **`hermes doctor --fix`** — config v33→v34 ✅
3. Skill **`hermes-cron-audit`** — script + skill Cursor ✅
4. **`hermes update`** — tentado; interrompido em um momento; SQLite ainda 3.50.4 ⚠️
5. **Gateway restart** — default running (telegram + api_server)

### Cron jobs — fix model Codex
6. Jobs `4709e6e007c8` e `0c6cbfc15cae` pinados com **`openai-codex` / `gpt-5.5`**
   - `gpt-5.2-codex` **não funciona** com conta ChatGPT (HTTP 400)
7. **`hermes cron run 4709e6e007c8`** — ✅ succeeded (validado 10:26)

### Erros corrigidos nesta sessão
- `tool_delay` nos jobs MiniMax → gateway restart resolveu (`cliente-perfil-dande` ok)
- Model inválido `gpt-5.2-codex` → trocado para `gpt-5.5`

---

## Estado atual (snapshot)

| Item | Valor |
|---|---|
| Repo | `C:\Users\User\AppData\Local\hermes\hermes-agent` |
| Branch | `local/harness` @ `6e280e85e6` |
| HERMES_HOME | `%LOCALAPPDATA%\hermes\` |
| Config | **v34** |
| Active provider | `openai-codex` (model default ainda não setado globalmente) |
| patch-guard | ✅ ok |
| doctor | ✅ exit 0 |
| Gateway | ✅ running — telegram + api_server |
| Slack | ⚠️ retrying (não bloqueante) |

### Cron fleet (default — 6 jobs)

| Job | Nome | Model | Status |
|---|---|---|---|
| ed20e10042e4 | relatorio-repos-18h | MiniMax-M3 | ✅ ok |
| 05725b06c694 | inbox-listar-dande | MiniMax-M3 | ⏸ pausado |
| 89874976ef5b | cliente-perfil-dande | MiniMax-M3 | ✅ ok |
| 619f7053817f | avaliacao-agente-dande | MiniMax-M3 | ⚠️ erro stale (`tool_delay` 09:00; semanal) |
| 4709e6e007c8 | v1-inbox-snapshot-30min | **gpt-5.5** | ✅ ok (validado manual) |
| 0c6cbfc15cae | cerebro-faxina | **gpt-5.5** | ✅ ok (próximo 01/09) |

Ticker cron: ✅ healthy (~1s heartbeat)

### Git (não commitado)

```
?? harness/scripts/hermes_cron_audit.py
 M harness/README.md
 M harness/registros/DECISOES.md
 M harness/registros/LOG_EXECUCAO.md
 M harness/registros/RETOMADA_SESSAO.md
 M harness/registros/STATUS.md
```

Skill Cursor (fora do repo): `~/.cursor/skills/hermes-cron-audit/SKILL.md`

---

## Comandos rápidos (PowerShell)

```powershell
# Ativar venv
& "$env:LOCALAPPDATA\hermes\hermes-agent\.venv\Scripts\Activate.ps1"

# Validar patches
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_patch_guard.py"

# Audit cron
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_cron_audit.py" --include-disabled

# Status gateway
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_gateway_ops.py" status

# Auditoria credenciais
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_credential_audit.py" --all-profiles

# Doctor
hermes doctor
```

---

## Skills Cursor (`~/.cursor/skills/`)

| Skill | Script |
|---|---|
| hermes-patch-guard | `harness/scripts/hermes_patch_guard.py` |
| hermes-test-slice | `harness/scripts/hermes_test_slice.py` |
| hermes-gateway-ops | `harness/scripts/hermes_gateway_ops.py` |
| hermes-credential-audit | `harness/scripts/hermes_credential_audit.py` |
| **hermes-cron-audit** | `harness/scripts/hermes_cron_audit.py` |

---

## Pendências (próxima sessão)

| # | Item | Prioridade |
|---|---|---|
| 1 | **Commit** harness (cron-audit + docs) na `local/harness` | Alta |
| 2 | Definir `model.default: gpt-5.5` no config.yaml | Média |
| 3 | `hermes update` — SQLite WAL-reset (parar gateway antes) | Média |
| 4 | Re-subir gateway profile `security-auditor` se necessário | Baixa |
| 5 | Pluginizar patches Hermes One (D-004) | Baixa |

---

## Modelos Codex — conta ChatGPT

**Usar:** `gpt-5.5`, `gpt-5.4`, `gpt-5.3-codex`, `gpt-5.4-mini`  
**Não usar:** `gpt-5.2-codex`, `gpt-5.1-codex-max`, `gpt-5.1-codex-mini` (HTTP 400)  
Referência: `hermes_cli/codex_models.py`

---

## Restrições (não esquecer)

- Nunca `git add .`
- **`hermes update` pode trocar para `main`** — voltar para `local/harness` depois
- Restart gateway exige `--confirm` no script gateway-ops
- Branch `local/harness` é **local only**
- Testes via `scripts/run_tests.sh`, não pytest direto
