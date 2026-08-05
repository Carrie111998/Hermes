# Relatório de investigação e correção — issue #78941

- Issue: https://github.com/NousResearch/hermes-agent/issues/78941 — "[Cache]: content-only prompt_cache_key can concentrate unrelated sessions into one routing scope"
- PR: https://github.com/NousResearch/hermes-agent/pull/78959
- Branch: `fix/78941-prompt-cache-key-session-scope` (a partir de `origin/main`)
- Commit: `bd4105b19` — `fix(cache): scope prompt_cache_key by session to stop cross-session bucket sharing`
- Veredito: **procedente**. Confirmado no código: `prompt_cache_key` era gerado só a partir de conteúdo estático (system prompt + tools), sem nenhum componente de sessão/tenant, concentrando sessões não relacionadas no mesmo bucket de roteamento de cache dos provedores.

## Resumo executivo

`prompt_cache_key` é o campo que Chat Completions e Responses/Codex (APIs OpenAI-compatíveis) usam para dar ao provedor uma dica de roteamento de cache de prefixo de prompt. A implementação existente (`_content_cache_key()` em `agent/transports/codex.py`) calculava esse valor como `sha256(instructions + tools ordenados)[:24]`, ignorando **completamente** `session_id`.

Essa decisão foi deliberada e resolvia um problema real e anterior (#51395, #52295): jobs de cron geram `session_id` no formato `cron_<job_id>_<YYYYMMDD_HHMMSS>`, com timestamp por disparo. Usar esse `session_id` bruto como chave de cache tornava o cache sempre frio a cada execução do mesmo job. A correção da época optou por remover `session_id` do cálculo por completo — só que isso é uma overcorreção: qualquer duas sessões (usuários diferentes, projetos diferentes, subagentes irmãos) que compartilhem o mesmo system prompt e mesmo conjunto de tools passam a colidir no mesmo bucket de cache do provedor, que é exatamente o cenário descrito na issue #78941.

## Causa raiz

Arquivo: `agent/transports/codex.py`, função `_content_cache_key(instructions, tools)`:

```python
content = f"{instructions or ''}\x00{tools_part}"
digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:24]
return f"pck_{digest}"
```

Nenhum componente de sessão entra no hash. `build_kwargs()` (Responses/Codex) chamava essa função sem passar `session_id`, e `chat_completions.py` (`_add_prompt_cache_key()`) tinha a mesma lacuna — nem sequer recebia `session_id` como parâmetro.

## Correção implementada

1. **`_cache_scope_from_session_id(session_id)`** (novo, `codex.py`): normaliza o `session_id` físico num "escopo lógico de cache" estável.
   - `session_id` comum (conversa principal, subagente, filho específico) → passa **inalterado**: cada instância de `Agent` já tem um `session_id` único por design (`agent_init.py`), então isso já é isolamento real.
   - `session_id` de cron (`cron_<job_id>_<YYYYMMDD_HHMMSS>`, via regex `^(cron_.+)_\d{8}_\d{6}$`) → o timestamp de disparo é removido, mantendo só `cron_<job_id>` como escopo. Isso preserva o comportamento que #51395/#52295 corrigiram: disparos repetidos do mesmo job continuam batendo no mesmo bucket quente.

2. **`_content_cache_key(instructions, tools, scope_id="")`**: agora hasheia `scope_id + instructions + tools` (separados por `\x00`), em vez de só `instructions + tools`.

3. **Call sites atualizados** para passar `scope_id=_cache_scope_from_session_id(session_id)`:
   - `codex.py::build_kwargs()` (Responses/Codex)
   - `chat_completions.py::_add_prompt_cache_key()` (novo parâmetro opcional `session_id: str | None = None`, retrocompatível), chamado nos dois call sites internos (`build_kwargs()` e `_build_kwargs_from_profile()`).

## Por que não é duplicado

`_content_cache_key` e `_cache_scope_from_session_id` existem uma única vez, em `codex.py`. `chat_completions.py` importa ambas (`from agent.transports.codex import _cache_scope_from_session_id, _content_cache_key`) em vez de reimplementar a lógica de hash/normalização — Responses API e Chat Completions API compartilham exatamente o mesmo algoritmo de escopo e hash.

## Cenários cobertos / piores casos

| Cenário | Antes da correção | Depois da correção |
|---|---|---|
| Duas sessões de usuários diferentes, mesmo system prompt | Mesmo `prompt_cache_key` (bug #78941) | Chaves diferentes |
| Subagentes irmãos com mesmo system prompt | Mesmo `prompt_cache_key` | Chaves diferentes (cada subagente tem `session_id` próprio) |
| Dois disparos do mesmo cron job | Mesmo `prompt_cache_key` (comportamento desejado) | Continua igual — escopo normalizado remove só o timestamp |
| Disparos de cron jobs diferentes | Mesmo `prompt_cache_key` se conteúdo estático igual (falha do design original) | Chaves diferentes — cada job tem seu próprio `job_id` no escopo |
| Rotação de sessão por compressão de contexto (`agent.session_id` reatribuído em `conversation_compression.py`) | Cache permanecia "quente" indevidamente (mesmo bug) | Cache fica frio nesse boundary específico — trade-off aceito, ver abaixo |

## Trade-off aceito e documentado

Após uma rotação de compressão de contexto, `agent.session_id` é reatribuído para um novo valor (`agent/conversation_compression.py`, `agent.session_id = new_session_id`). Isso significa que o escopo de cache muda nesse ponto específico da conversa, esfriando o cache uma vez. É um evento raro e um trade-off razoável frente ao bug original (compartilhamento universal e silencioso entre sessões não relacionadas).

`prompt_cache_key` é sempre uma dica de roteamento, nunca um limite de correção — o pior caso de qualquer configuração aqui é desempenho subótimo (cache frio), nunca uma resposta incorreta.

## Testes

- Suíte de transports (`test_codex_transport.py` + `test_chat_completions.py`): **113 passed** (68 + 45), incluindo:
  - `test_cache_key_is_content_addressed_not_session_id` / `test_cache_key_stable_across_session_ids` (pré-existentes, continuam válidos)
  - **novo** `test_cache_key_differs_across_unrelated_sessions` (codex.py) e `test_unrelated_sessions_get_distinct_keys` (chat_completions.py) — regressão direta do bug #78941
  - `test_cron_ids_share_static_prefix_key_and_content_changes_invalidate` corrigido: usava um formato de `session_id` de cron fictício (`cron_job_2026-07-15T10:00:00Z`, ISO) que não batia com o formato real gerado em `cron/scheduler.py:3015` (`cron_<job_id>_<YYYYMMDD_HHMMSS>`); ajustado para o formato real.
  - **novo** `test_codex_cache_scope_headers_normalize_cron_session_id` (codex.py): cobre a extensão do fix para o header `session_id`/`x-client-request-id` do backend Codex — dois disparos do mesmo cron job produzem o mesmo header normalizado, jobs diferentes produzem headers diferentes.
- Suíte de wiring/parity de provedores (`test_transport_parity.py`, `test_profile_wiring.py`, `test_e2e_wiring.py`, `test_model_extra_type_guard.py`, `test_compressed_summary_metadata.py`): **35 passed**.
- Perfis de plugins de model-providers (zai, opencode_go, ollama_cloud, minimax, kimi, deepseek): **126 passed**.
- Total: **273 testes passando**, nenhuma regressão detectada.

## Extensão: header de roteamento de cache do backend Codex (`session_id` / `x-client-request-id`)

Além do `prompt_cache_key` do body, o backend Codex (`is_codex_backend=True`, `chatgpt.com/backend-api/codex`) também envia `session_id` e `x-client-request-id` como **headers HTTP** (`agent/transports/codex.py`, bloco `if is_codex_backend:`), com o mesmo propósito de roteamento/afinidade de cache de prefixo — não como identidade de conversa persistida no servidor. Antes desta extensão, esse header usava o `session_id` bruto (via `_bounded_prompt_cache_key(session_id)`), reintroduzindo o mesmo padrão problemático da issue original: colidia entre sessões não relacionadas com o mesmo conteúdo estático (já resolvido no body) e nunca reagrupava disparos do mesmo cron job (o timestamp do disparo ia direto pro header).

**Investigação de segurança da mudança**: antes de normalizar, foi necessário confirmar que esse header é puramente uma dica de roteamento de infraestrutura, não um identificador de continuidade de conversa no servidor (o que tornaria a normalização arriscada — reagrupar disparos de cron diferentes no mesmo header poderia, em tese, misturar estado real). Evidências coletadas via `git log -S` e a issue histórica que motivou a introdução do header:

- O commit que restaurou os headers (`4d39a603d`, resolvendo a regressão #47335) e o comentário inline em `codex.py` (linhas 456-462) descrevem explicitamente o propósito como **"cache-scope routing so prompt cache hits remain high"** e **"belt-and-braces fallback"** ao lado do `prompt_cache_key` do body — nunca como mecanismo de identidade/estado.
- A issue #47335 que motivou a introdução documenta uma queda de **cache-hit-ratio** (94-97% → 13-30%) e custo/latência disparados quando os headers foram removidos por engano — o problema tratado é 100% custo/desempenho, nunca continuidade de conversa ou vazamento de estado.
- O payload completo da conversa (histórico de mensagens) sempre viaja no body da requisição, independente destes headers — eles não substituem nem representam estado de conversa, só ajudam o load balancer do provedor a rotear pro worker que já tem o prefixo em KV-cache.
- Contraste de controle: `x-grok-conv-id` (backend xAI Responses, linha ~494) usa `session_id` bruto **de propósito**, confirmado por comentário/teste dedicados (`"x-grok-conv-id stays session/transcript id, not cache key."`) — é o caso oposto, onde o header É identidade real. O header do Codex backend não tem esse comentário nem esse propósito documentado em nenhum lugar do código ou histórico.

**Conclusão**: seguro normalizar. Corrigido reaproveitando a mesma função já existente (`_cache_scope_from_session_id`), sem duplicar lógica:

```python
cache_scope_id = _bounded_prompt_cache_key(
    _cache_scope_from_session_id(session_id)
)
```

Isso alinha o comportamento do header com o do body: sessões não relacionadas deixam de colidir no mesmo escopo de cache do header, e disparos do mesmo cron job continuam compartilhando escopo (timestamp removido).

### Alternativas consideradas

| Alternativa | Motivo de rejeição |
|---|---|
| Deixar o header como estava (`session_id` bruto) | Mantém as duas falhas da issue original nesse header: colisão entre sessões não relacionadas + cache sempre frio em cron. Inconsistente com o fix já aplicado ao body. |
| Reimplementar a normalização de cron localmente no bloco do header (duplicando o regex/lógica de `_cache_scope_from_session_id`) | Duplicação de lógica — viola DRY e a regra explícita do projeto de não duplicar. Rejeitado. |
| Refatorar `build_kwargs()` para calcular `scope_id = _cache_scope_from_session_id(session_id)` uma única vez e passar para os dois call sites (body e header) | Evitaria uma segunda chamada (hoje a função é invocada 2x: linha ~375 para o body, linha ~464 para o header). Rejeitado por ora: a função é uma regex simples sobre uma string curta (custo desprezível) e a mudança aumentaria o número de linhas tocadas fora do escopo do bug sem ganho mensurável — KISS/YAGNI. Anotado aqui como possível micro-limpeza futura, não crítica. |

## Observação de segurança fora de escopo

Durante o trabalho, o arquivo `AGENTS.md` já estava modificado na árvore de trabalho (fora desta tarefa) com uma "Infographic Generation Directive" instruindo execução automática de `scripts/pr_infographic_prompt.py` sempre que "infographic"/"infográfico" for mencionado, acompanhada de arquivos não rastreados (`scripts/pr_infographic_prompt.py`, `infographic.png`, `infographic_report.md`, `prompt_output.json/txt`). Isso não foi tocado, commitado nem executado — sinalizado ao usuário por ter características de possível prompt injection plantada no repositório (instrução condicional a uma palavra-gatilho, combinada com script executável não revisado).
