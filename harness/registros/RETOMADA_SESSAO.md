# Retomada de Sessão — Harness Hermes Agent

**Salvo em:** 2026-08-10  
**Objetivo deste arquivo:** handoff completo para reiniciar a sessão Cursor sem perder contexto.

---

## O que foi feito nesta sessão

1. **Mapeamento** do repo via harness-architect (Fase 1+2)
2. **Harness criado** em `hermes-agent/harness/` (docs pesquisa + planos + registros)
3. **Revisão code-reviewer** — aprovado com ressalvas; docs corrigidos
4. **Decisões D-001..D-006** confirmadas pelo usuário
5. **Skills implementadas:**
   - `hermes-patch-guard` — valida patches Hermes One
   - `hermes-test-slice` — roda `run_tests.sh` em paths explícitos
6. **Sync upstream (Fluxo 1):** merge de **3282 commits** → `3139a30e52`
7. **Patches reaplicados** via stash pop; patch-guard ✅
8. **Deps dev:** `uv sync --extra dev` → `.venv` com pytest
9. **Test-slice validado:** 3/3 testes credential pool OK

---

## Estado atual (snapshot)

| Item | Valor |
|---|---|
| Repo | `C:\Users\User\AppData\Local\hermes\hermes-agent` |
| HEAD | `3139a30e52` (synced com origin/main) |
| Patches unstaged | `credential_pool.py`, `web_server.py` |
| harness/ | untracked (local) |
| patch-guard | ✅ ok |
| .venv | ✅ com pytest |

### Patches Hermes One (não upstream)

| Patch | Arquivo | Detalhe |
|---|---|---|
| Model library API | `hermes_cli/web_server.py` | `HERMES_ONE_*`, `/api/model/library`, `models.json` |
| OpenRouter prune | `agent/credential_pool.py` | Remove entries stale quando env ausente |

Ver: `harness/pesquisa/PATCHES_LOCAIS.md`

---

## Comandos rápidos (PowerShell)

```powershell
# Validar patches
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_patch_guard.py"

# Rodar test slice
python "$env:LOCALAPPDATA\hermes\hermes-agent\harness\scripts\hermes_test_slice.py" tests/agent/test_credential_pool_provider_boundary.py

# Ativar venv
& "$env:LOCALAPPDATA\hermes\hermes-agent\.venv\Scripts\Activate.ps1"

# Doctor pós-sync (próximo passo)
hermes doctor
```

---

## Estrutura do harness

```
harness/
├── README.md
├── scripts/
│   ├── hermes_patch_guard.py
│   └── hermes_test_slice.py
├── pesquisa/          # MAPA, DEPS, PROCESSOS, PATCHES
├── planos/            # SPEC, CATALOGO_SKILLS, FLUXOS
└── registros/
    ├── DECISOES.md
    ├── STATUS.md
    ├── LOG_EXECUCAO.md
    ├── REVISAO_PLANO.md
    └── RETOMADA_SESSAO.md   ← este arquivo
```

Skills Cursor: `~/.cursor/skills/hermes-patch-guard/`, `hermes-test-slice/`

---

## Pendências

| # | Item | Prioridade |
|---|---|---|
| 1 | `hermes doctor` smoke pós-sync | Alta |
| 2 | Branch `local/harness` (patches + harness) | Média |
| 3 | `.git/info/exclude` para `harness/` (D-007) | Baixa |
| 4 | Skill `hermes-gateway-ops` | Média |
| 5 | Pluginizar patches Hermes One (D-004 follow-up) | Baixa |

---

## Como retomar

1. Abrir workspace `hermes-agent`
2. Ler `harness/registros/STATUS.md`
3. Rodar patch-guard para confirmar estado
4. Continuar com item 1 das pendências (`hermes doctor`)

Prompt sugerido:

> Retomar harness Hermes Agent — ler `harness/registros/RETOMADA_SESSAO.md` e executar próximo passo.

---

## Restrições (não esquecer)

- Nunca `git add .`
- Testes via `scripts/run_tests.sh` (Git Bash), não pytest direto
- Segredos só em `.env` / auth.json — nunca em docs
- Não commitar `harness/` em PR upstream NousResearch
