# Patches Locais — Hermes One vs Upstream

**Classificação:** Confirmada (git diff + leitura de código, 2026-08-10)

Estes patches existem no checkout local e **não estão no upstream** NousResearch. Risco alto em merges de `main`.

---

## 1. HERMES_ONE_MODEL_LIBRARY_COMPAT_V1

**Arquivo:** `hermes_cli/web_server.py` (~linhas 19942–20103)

**Propósito:** Endpoints REST para biblioteca de modelos remotos — consumidor **Hermes One externo** ou SSH remote picker. O `apps/desktop` upstream usa `/api/model/options` e `/api/model/set` (**não** consome `/api/model/library`).

| Endpoint | Método | Função |
|---|---|---|
| `/api/model/library` | GET | Lista modelos + modelo ativo |
| `/api/model/library` | POST | Adiciona shortcut |
| `/api/model/library/{id}` | PATCH | Atualiza entrada |
| `/api/model/library/{id}` | DELETE | Remove entrada |

**Persistência:** `%LOCALAPPDATA%\hermes\models.json` (HERMES_HOME-aware via `get_hermes_home()`)

**Design intencional:**

- Atalhos ficam no host remoto (SSH), não no desktop
- Não altera semântica upstream de `/api/model/set` e `/api/model/options`
- Escrita atômica via `.tmp` + `replace()`

**Autenticação:** `/api/model/library` **não** está em `PUBLIC_API_PATHS` — requer `X-Hermes-Session-Token` ou `Authorization: Bearer` (ver `hermes_cli/dashboard_auth/public_paths.py` + middleware em `web_server.py`).

**Backups locais:** `hermes_cli/web_server.py.orig` (untracked)

---

## 2. OpenRouter credential pool prune

**Arquivo:** `agent/credential_pool.py` (~linhas 2589–2597)

**Propósito:** Quando `OPENROUTER_API_KEY` está ausente em `.env`/env, remove entries do pool com `source=env:OPENROUTER_API_KEY`.

**Motivação (comentário no código):**

> Sem isso, entry zumbi fica pra sempre (re-seed não acontece mas a entry antiga não some). Ver INCIDENTE-AUTH-JSON-REWRITE.

**Backups locais:** `agent/credential_pool.py.bak.patch-20260729_120216` (untracked)

---

## 3. package-lock.json drift

**Arquivo:** `package-lock.json` (~24 linhas)

**Classificação:** Side-effect de `npm install` local — provavelmente não intencional como patch.

**Ação recomendada:** Reverter ou regenerar após merge limpo.

---

## Estratégias futuras (D-004 pendente)

| Opção | Prós | Contras |
|---|---|---|
| **Manter patches manuais** | Rápido, zero refactor | Conflito a cada merge |
| **Extrair para plugin** | Alinhado AGENTS.md, mergeável | Esforço; web_server pode precisar hook genérico |
| **PR upstream** | Mantém fork limpo | Model library pode ser escopo Hermes One only; prune OpenRouter pode ser aceito |

---

## Checklist pós-merge

- [ ] Grep `HERMES_ONE` — bloco intacto?
- [ ] Grep `INCIDENTE-AUTH-JSON-REWRITE` / prune OpenRouter intacto?
- [ ] `scripts/run_tests.sh tests/agent/` (diretório — **sem glob**; ver `run_tests_parallel.py`)
- [ ] Smoke GET/POST `/api/model/library` com `hermes serve` + token de sessão (ver Fluxo 3)
- [ ] Cliente Hermes One / SSH picker lê shortcuts (se aplicável — **não** validar via `apps/desktop` upstream)
- [ ] Smoke manual até existir teste HTTP automatizado do bloco `HERMES_ONE_*`
