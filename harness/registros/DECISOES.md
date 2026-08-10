# Decisões — Harness Hermes Agent

Registro de decisões do harness agêntico local. Atualizar a cada portão de aprovação.

## Resolvidas

| ID | Data | Decisão | Evidência |
|---|---|---|---|
| D-001 | 2026-08-10 | Harness vive **dentro** do repo `hermes-agent`, prefixo `harness/` | Resposta usuário: opção 1 |
| D-002 | 2026-08-10 | Documentação inicial nível **completo** (pesquisa + planos + registros) | Mapeamento harness-architect |
| D-003 | 2026-08-10 | Objetivo: operação **Hermes One** local; upstream só patch genérico | Usuário: ok |
| D-004 | 2026-08-10 | Patches: **manter manuais**; pluginização follow-up | Usuário: ok |
| D-005 | 2026-08-10 | Superfície prioritária: **Desktop** + **gateway** (Telegram) | Usuário: ok |
| D-006 | 2026-08-10 | Ambiente: **Windows nativo** + testes via Git Bash/MinGit | Usuário: ok |
| D-008 | 2026-08-10 | Revisão code-reviewer: plano aprovado com ressalvas; docs corrigidos | Agent abad2f36 |
| D-009 | 2026-08-10 | Skills `hermes-patch-guard` + `hermes-test-slice` implementadas | Esta sessão |
| D-010 | 2026-08-10 | Fluxo 1 sync: merge `origin/main` (3282 commits); patches reaplicados; patch-guard OK | LOG_EXECUCAO.md |
| D-007 | 2026-08-10 | Versionar `harness/` em branch local `local/harness` (não upstream) | Resposta usuário: opção 1 |
| D-011 | 2026-08-10 | Branch `local/harness` criada com patches Hermes One + harness/ | Esta sessão |
| D-012 | 2026-08-10 | Skill `hermes-gateway-ops` implementada (status/logs/restart) | Esta sessão |

## Pendentes

(nenhuma)

## Restrições invioláveis

- Nunca copiar tokens/API keys para documentos do harness
- Nunca `git add .` — stage explícito por caminho
- Testes via `scripts/run_tests.sh`, não pytest direto
- Deploy VPS/Swarm só com aprovação explícita separada
- Respeitar `AGENTS.md`: prompt caching sagrado, core estreito

## Próximo passo

1. Skill **`hermes-credential-audit`**
2. **`hermes doctor --fix`** — migrar config v33→v34 (opcional)
3. Pluginizar patches Hermes One (D-004 follow-up)
