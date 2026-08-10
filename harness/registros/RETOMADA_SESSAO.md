# Retomada de Sessão — Harness Hermes Agent

**Salvo em:** 2026-08-10 (fim de sessão — handoff completo)  
**Objetivo:** reiniciar Cursor em outra janela sem perder contexto.

---

## Prompt para colar na nova janela

```
Retomar harness Hermes Agent — ler harness/registros/RETOMADA_SESSAO.md e executar próximo passo.
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_patch_guard.py"
```

---

## O que foi feito nesta sessão (completo)

### Harness + upstream
1. Retomada: patch-guard ✅ + `hermes doctor` ✅ (exit 0)
2. Branch **`local/harness`** criada — patches + harness commitados
3. Skills implementadas:
   - `hermes-gateway-ops` — status/logs/restart gateway
   - `hermes-credential-audit` — inventário pool sem expor tokens

### Limpeza de credenciais (pedido do usuário)
4. **OpenRouter** removido — `.env` + pool em 4 scopes (default + 3 profiles)
5. **Gemini** removido — `GEMINI_API_KEY` + `GOOGLE_API_KEY` + pool em 4 scopes
6. **Codex** consolidado:
   - Duplicata `openai-codex-oauth-2` removida
   - Tokens importados de `~/.codex/auth.json` → sessão Hermes própria
   - `model.provider=openai-codex` em `%LOCALAPPDATA%\hermes\config.yaml`
   - 1 entry: `device_code` — **logged in** ✅

---

## Estado atual (snapshot)

| Item | Valor |
|---|---|
| Repo | `C:\Users\User\AppData\Local\hermes\hermes-agent` |
| Branch | `local/harness` @ `6f77797197` |
| Upstream base | `3139a30e52` (= `origin/main`) |
| HERMES_HOME | `%LOCALAPPDATA%\hermes\` |
| Active provider | `openai-codex` |
| patch-guard | ✅ ok |
| doctor | ✅ exit 0 |
| Gateway | ✅ running (telegram + slack + api_server) |

### Providers ativos (default)

| Provider | Estado |
|---|---|
| **openai-codex** | ✅ logged in — `device_code` (importado Codex CLI) |
| **minimax-oauth** | ✅ 2 entries ok (backup) |
| **deepseek** | env key presente |
| **copilot** | gh_cli (sem token ativo) |
| ~~openrouter~~ | removido |
| ~~gemini~~ | removido |

### Profiles

| Profile | Gateway | Model |
|---|---|---|
| default | Scheduled Task `Hermes_Gateway` | openai-codex |
| code-reviewer | — | MiniMax-M3 |
| data-analyst | PID ativo | MiniMax-M3 |
| security-auditor | PID ativo | MiniMax-M3 |

OpenRouter/Gemini removidos de **todos** os profiles.

### Patches Hermes One (commitados em `local/harness`)

| Patch | Arquivo |
|---|---|
| Model library API | `hermes_cli/web_server.py` |
| OpenRouter prune | `agent/credential_pool.py` |

Ver: `harness/pesquisa/PATCHES_LOCAIS.md`

---

## Comandos rápidos (PowerShell)

```powershell
# Ativar venv
& "$env:LOCALAPPDATA\hermes\hermes-agent\.venv\Scripts\Activate.ps1"

# Validar patches
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_patch_guard.py"

# Auditoria credenciais (sem expor tokens)
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_credential_audit.py" --all-profiles

# Status gateway
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_gateway_ops.py" status

# Test slice
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_test_slice.py" tests/agent/test_credential_pool_provider_boundary.py

# Doctor / fix config
hermes doctor
hermes doctor --fix

# Auth Codex (se precisar re-login)
hermes auth add openai-codex
```

---

## Skills Cursor (`~/.cursor/skills/`)

| Skill | Script |
|---|---|
| hermes-patch-guard | `harness/scripts/hermes_patch_guard.py` |
| hermes-test-slice | `harness/scripts/hermes_test_slice.py` |
| hermes-gateway-ops | `harness/scripts/hermes_gateway_ops.py` |
| hermes-credential-audit | `harness/scripts/hermes_credential_audit.py` |

---

## Pendências (próxima sessão)

| # | Item | Prioridade |
|---|---|---|
| 1 | `hermes doctor --fix` — migrar config v33→v34 | Média |
| 2 | Skill **`hermes-cron-audit`** | Média |
| 3 | `hermes update` — SQLite WAL-reset bug | Baixa |
| 4 | Pluginizar patches Hermes One (D-004) | Baixa |

---

## Avisos do doctor (não bloqueantes)

- SQLite 3.50.4 WAL-reset → `hermes update`
- Config v33 → v34 → `hermes doctor --fix`
- Toolsets opcionais sem deps (web, discord, x_search, etc.)

---

## Restrições (não esquecer)

- Nunca `git add .`
- Testes via `scripts/run_tests.sh` (Git Bash), não pytest direto
- Segredos só em `.env` / auth.json — nunca em docs/commits
- Branch `local/harness` é **local only** — não PR upstream NousResearch
- Restart gateway exige `--confirm` no script gateway-ops

---

## Commits desta sessão (branch `local/harness`)

```
6f77797197 feat(harness): add hermes-credential-audit skill and script
f83d8c7515 feat(harness): add hermes-gateway-ops skill and script
3961e620c2 docs(harness): atualizar STATUS após branch local/harness
052f52ab30 local(harness): versionar patches Hermes One e docs do harness
```
