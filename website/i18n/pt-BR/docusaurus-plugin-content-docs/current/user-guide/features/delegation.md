---
sidebar_position: 7
title: "Delegação de Subagentes"
description: "Crie child agents isolados para fluxos de trabalho paralelos com delegate_task"
---

# Delegação de Subagentes

A ferramenta `delegate_task` cria instâncias filhas de AIAgent com contexto isolado, acesso herdado às ferramentas e sessões de terminal próprias. Cada filho recebe uma conversa nova e trabalha de forma independente — apenas o resumo final entra no contexto do pai.

Chamadas de modelo no nível superior rodam em background automaticamente. O Hermes retorna um handle imediatamente para a conversa continuar e depois publica o resultado como uma nova mensagem. Um subagente orquestrador espera seus próprios workers para sintetizar os resultados antes de retornar.

## Tarefa única {#single-task}

```python
delegate_task(
    goal="Debug why tests fail",
    context="Error: assertion in test_foo.py line 42"
)
```

## Lote paralelo {#parallel-batch}

Até 3 subagentes concorrentes por padrão (configurável, sem teto rígido):

```python
delegate_task(tasks=[
    {"goal": "Research topic A", "context": "Focus on recent primary sources"},
    {"goal": "Research topic B", "context": "Compare the leading explanations"},
    {"goal": "Fix the build", "context": "Project root: /home/user/project"}
])
```

## Como funciona o contexto do subagente {#how-subagent-context-works}

:::warning Crítico: subagentes não sabem de nada
Subagentes começam com uma **conversa completamente nova**. Eles não têm nenhum conhecimento do histórico de conversa do pai, de chamadas de ferramentas anteriores ou de qualquer coisa discutida antes da delegação. O único contexto do subagente vem dos campos `goal` e `context` que o agente pai preenche ao chamar `delegate_task`.
:::

Isso significa que o agente pai deve passar **tudo** que o subagente precisa na chamada:

```python
# BAD - subagent has no idea what "the error" is
delegate_task(goal="Fix the error")

# GOOD - subagent has all context it needs
delegate_task(
    goal="Fix the TypeError in api/handlers.py",
    context="""The file api/handlers.py has a TypeError on line 47:
    'NoneType' object has no attribute 'get'.
    The function process_request() receives a dict from parse_body(),
    but parse_body() returns None when Content-Type is missing.
    The project is at /home/user/myproject and uses Python 3.11."""
)
```

O subagente recebe um system prompt focado construído a partir do seu goal e context, instruindo-o a completar a tarefa e fornecer um resumo estruturado do que fez, do que encontrou, de quaisquer arquivos modificados e de quaisquer problemas encontrados.

## Exemplos práticos {#practical-examples}

### Pesquisa paralela {#parallel-research}

Pesquise vários tópicos simultaneamente e colete resumos:

```python
delegate_task(tasks=[
    {
        "goal": "Research the current state of WebAssembly in 2025",
        "context": "Focus on: browser support, non-browser runtimes, language support"
    },
    {
        "goal": "Research the current state of RISC-V adoption in 2025",
        "context": "Focus on: server chips, embedded systems, software ecosystem"
    },
    {
        "goal": "Research quantum computing progress in 2025",
        "context": "Focus on: error correction breakthroughs, practical applications, key players"
    }
])
```

### Revisão de código + correção {#code-review--fix}

Delegue um fluxo de revisão e correção para um contexto novo:

```python
delegate_task(
    goal="Review the authentication module for security issues and fix any found",
    context="""Project at /home/user/webapp.
    Auth module files: src/auth/login.py, src/auth/jwt.py, src/auth/middleware.py.
    The project uses Flask, PyJWT, and bcrypt.
    Focus on: SQL injection, JWT validation, password handling, session management.
    Fix any issues found and run the test suite (pytest tests/auth/)."""
)
```

### Refatoração multi-arquivo {#multi-file-refactoring}

Delegue uma refatoração grande que inundaria o contexto do pai:

```python
delegate_task(
    goal="Refactor all Python files in src/ to replace print() with proper logging",
    context="""Project at /home/user/myproject.
    Use the 'logging' module with logger = logging.getLogger(__name__).
    Replace print() calls with appropriate log levels:
    - print(f"Error: ...") -> logger.error(...)
    - print(f"Warning: ...") -> logger.warning(...)
    - print(f"Debug: ...") -> logger.debug(...)
    - Other prints -> logger.info(...)
    Don't change print() in test files or CLI output.
    Run pytest after to verify nothing broke."""
)
```

## Detalhes do modo batch {#batch-mode-details}

Quando um agente de nível superior fornece um array `tasks`, o Hermes retorna um handle de background, executa os subagentes em paralelo e publica um resultado consolidado depois que todos os filhos terminam. Um subagente orquestrador espera seu batch no turno atual para sintetizar os resultados.

- **Concorrência máxima:** 3 tarefas por padrão (configurável via `delegation.max_concurrent_children` ou a env var `DELEGATION_MAX_CONCURRENT_CHILDREN`; mínimo 1, sem teto rígido). Batches maiores que o limite retornam erro de ferramenta em vez de serem truncados silenciosamente.
- **Thread pool:** Usa `ThreadPoolExecutor` com o limite de concorrência configurado como max workers
- **Exibição de progresso:** No modo CLI, uma tree view mostra chamadas de ferramentas de cada subagente em tempo real com linhas de conclusão por tarefa. No gateway, o progresso é agrupado e repassado ao callback de progresso do pai
- **Ordem dos resultados:** Resultados são ordenados por índice de tarefa para corresponder à ordem de entrada, independentemente da ordem de conclusão
- **Cancelamento:** Mensagens de follow-up não cancelam um batch de background de nível superior. `/stop` ou fechar/redefinir a sessão proprietária cancela seus filhos ativos. Filhos síncronos de orquestrador ainda seguem o estado de interrupção do pai

Delegação síncrona de tarefa única a partir de um orquestrador roda diretamente, sem overhead de thread pool.

### Conclusões duráveis em background {#durable-background-completions}

Quando uma delegação em background termina, o Hermes armazena seu evento de conclusão no `state.db` do profile ativo antes de publicá-lo na fila normal de fresh-turn. Se o Hermes reiniciar após a conclusão mas antes da entrega, o evento pendente é restaurado e roteado pelas mesmas verificações de propriedade. Consumidores concorrentes usam um claim durável, então apenas o consumidor que aceita com sucesso o turno sintético confirma a entrega; tentativas falhas liberam o claim para retry.

Isso não retoma a execução do filho após um crash. Uma delegação cujo processo proprietário desaparece enquanto ainda está rodando é registrada como `unknown`, porque o Hermes não consegue provar se seus efeitos colaterais externos aconteceram. Registros pendentes e entregues são limitados e locais ao profile.

## Override de modelo {#model-override}

Você pode configurar um modelo diferente para subagentes via `config.yaml` — útil para delegar tarefas simples a modelos mais baratos/rápidos:

```yaml
# In ~/.hermes/config.yaml
delegation:
  model: "google/gemini-flash-2.0"    # Cheaper model for subagents
  provider: "openrouter"              # Optional: route subagents to a different provider
```

Se omitido, subagentes usam o mesmo modelo do pai.

## Acesso herdado às ferramentas {#inherited-tool-access}

`delegate_task` não aceita um parâmetro `toolsets` voltado ao modelo. Cada subagente herda os toolsets habilitados do pai, para que o modelo não possa conceder a um filho capacidades que o pai não tem. Configure as ferramentas do pai antes de iniciar a conversa se o trabalho delegado precisar de capacidades adicionais.

Certas ferramentas são bloqueadas para subagentes mesmo quando o pai as tem:
- `delegate_task` — bloqueado para subagentes leaf (o padrão). Mantido para filhos com `role="orchestrator"`, limitado por `max_spawn_depth` — veja [Limite de profundidade e orquestração aninhada](#depth-limit-and-nested-orchestration) abaixo.
- `clarify` — subagentes não podem interagir com o usuário
- `memory` — sem gravações em memória persistente compartilhada
- `send_message` — sem efeitos colaterais cross-platform
- `cronjob` — sem agendar mais trabalho em nome do pai

Ambos os roles mantêm `execute_code` (programmatic tool calling) para que filhos possam agrupar trabalho mecânico.

## Máximo de iterações {#max-iterations}

Cada subagente tem um limite de iterações (padrão: 50) que controla quantos turnos de tool-calling ele pode fazer:

```python
delegate_task(
    goal="Quick file check",
    context="Check if /etc/nginx/nginx.conf exists and print its first 10 lines",
    max_iterations=10  # Simple task, don't need many turns
)
```

## Timeout do filho {#child-timeout}

Por padrão **não há timeout de wall-clock** em subagentes. Filhos falham apenas pelo que estão realmente fazendo — erros de API, erros de ferramenta ou atingir seu orçamento de iterações — nunca por um cronômetro no nível da delegação. Versões anteriores tinham um teto rígido (300s, depois 600s), que continuava matando filhos legitimamente ocupados no meio da tarefa: revisões de código profundas, fan-outs grandes de pesquisa e modelos de raciocínio lentos rotineiramente precisam de mais de 10 minutos enquanto fazem progresso constante o tempo todo.

Filhos genuinamente travados ainda são detectados: o monitor de staleness de heartbeat para de atualizar a atividade do pai quando um filho não faz progresso (sem chamadas de API, sem inícios de ferramenta), permitindo que o timeout de inatividade do gateway dispare em um worker realmente emperrado.

Se você quiser um teto rígido mesmo assim (ex.: controle de custo em delegação não supervisionada via cron), opte por instalação:

```yaml
delegation:
  child_timeout_seconds: 0     # default: 0 = no timeout
  # child_timeout_seconds: 1800  # opt-in hard cap (floor 30s)
```

Um valor positivo impõe um limite rígido de wall-clock em cada filho; `0` ou um valor negativo desabilita.

Quando um teto configurado dispara, o resultado do filho carrega metadados estruturados de timeout junto com a mensagem de erro para que pais e hooks distingam um kill por cronômetro de outras falhas sem parsear texto: `timeout_seconds` (o teto configurado), `timed_out_after_seconds` (wall clock real) e `timeout_phase` (`before_first_llm_call` quando o filho nunca chegou à primeira requisição, `after_llm_calls` caso contrário). Os três são `null` em erros que não são timeout.

:::tip Dump de diagnóstico em timeout com zero chamadas
Com um teto rígido configurado, se um subagente expira tendo feito **zero** chamadas de API (geralmente: provider inacessível, falha de auth ou rejeição de tool schema), `delegate_task` grava um diagnóstico estruturado em `~/.hermes/logs/subagent-timeout-<session>-<timestamp>.log` contendo snapshot de config do subagente, trace de resolução de credenciais, quaisquer mensagens de erro precoces e stack traces de **todas** as threads vivas (não só a do filho) — um filho parado esperando uma thread helper aninhada é indistinguível de um provider lento sem o quadro completo.
:::

## Detecção de stall para subagentes em background {#stall-detection-for-background-subagents}

Delegações em background (`delegate_task(background=true)`) são monitoradas por um
**monitor de stall baseado em progresso** — ligado por padrão, zero config. Diferente de um
timeout de wall-clock, nunca toca um filho que está fazendo progresso, não importa
quanto tempo rode.

O monitor amostra os sinais de progresso de cada filho detached — contagem de chamadas de API,
ferramenta atual e timestamp da última atividade (que avança em **cada token
streamed**, transição de ferramenta e limite de chamada de API, então um filho no meio de uma
resposta longa sempre conta como vivo):

1. **Filhos em progresso nunca são tocados.** Qualquer sinal avançando
   reseta o relógio.
2. Um filho cujo progresso está completamente congelado além do limiar stale
   (450s ocioso, 1200s dentro de uma ferramenta — comandos de terminal e fetches web
   legitimamente lentos recebem o teto maior) é **interrompido** e
   recebe uma janela de graça de 120s. Um filho que desenrola a tempo entrega seus
   resultados parciais pelo caminho normal de conclusão.
3. Um filho que nunca retorna é finalizado à força com um evento terminal de conclusão `stalled`,
   para que a sessão proprietária ouça um desfecho em vez de
   ficar em silêncio, e o slot async libera para novo trabalho.

O evento `stalled` carrega metadados estruturados espelhando os campos de timeout
do caminho sync: `stalled_after_quiet_seconds`, `stall_threshold_seconds`,
`stall_phase` (`idle` / `in_tool`) e `stall_grace_seconds`.

Isso fechou um failure mode antigo em que um filho em background emperrado
deixava sua sessão parecendo morta até um restart de processo. A wedge subjacente
(filhos travados na primeira chamada de API após uptime de gateway de vários dias)
também foi corrigida na raiz: filhos delegados agora rodam suas requisições de API
OpenAI-wire inline na própria thread de conversa em vez de uma thread worker
aninhada — a camada onde a wedge vivia. O monitor de stall permanece
como rede de segurança para qualquer outra coisa.


## Monitorando subagentes em execução (`/agents`) {#monitoring-running-subagents-agents}

A TUI inclui um overlay `/agents` (alias `/tasks`) que transforma fan-out recursivo de `delegate_task` em uma superfície de auditoria de primeira classe:

- Tree view ao vivo de subagentes rodando e recém-finalizados, agrupados por pai
- Rollups de custo, tokens e arquivos tocados por branch
- Controles de kill e pause — cancele um subagente específico no meio do voo sem interromper os irmãos
- Revisão pós-hoc: percorra o histórico turno a turno de cada subagente mesmo depois que retornaram ao pai

O CLI clássico apenas imprime `/agents` como um resumo em texto; a TUI é onde o overlay brilha. Veja [TUI — Slash commands](/user-guide/tui#slash-commands).

No CLI clássico e em toda plataforma de gateway (Telegram, Discord, Slack, ...),
`/agents` também lista **delegações em background com atividade ao vivo por filho**,
amostrada diretamente de cada filho em execução:

```
Background delegations: 1 running
- deleg_ab12cd34 · running · research the delegation stall monitor
  - child 1: 4 api calls · in web_search · active 12s ago
  - child 2: 7 api calls · between turns · active 3s ago
```

Uma delegação que o monitor de stall sinalizou aparece como
`stalling · no progress 450s — interrupting`, e filhos longamente quietos mas saudáveis
mostram seu tempo quieto para você distinguir "lento" de "travado" de
relance.

## Transcrições ao vivo {#live-transcripts}

Cada dispatch de `delegate_task` também cria um **log append-only legível por humanos por tarefa** para que você (ou o agente pai) possa acompanhar um subagente trabalhando em tempo real em vez de esperar o resumo consolidado:

```
<hermes_home>/cache/delegation/live/<delegation_id>/task-<n>.log
```

A resposta do dispatch inclui os caminhos como `live_transcripts`, e os arquivos são pré-criados no momento do dispatch, então isso funciona imediatamente:

```bash
tail -f ~/.hermes/cache/delegation/live/deleg_ab12cd34/task-0.log
```

Cada linha é timestamped e mostra o texto assistant do filho, snippets de thinking, chamadas de ferramenta (`-> tool_name({args})`), resultados de ferramenta e um marcador de status final. Um `manifest.json` no mesmo diretório descreve o batch (goals, contagem de tarefas, status por tarefa). Os logs persistem após a conclusão — também servem como registro operacional de fidelidade total junto ao resumo — e diretórios com mais de 7 dias são podados automaticamente em novos dispatches. Como vivem sob `cache/delegation`, também são legíveis de backends de terminal remotos (Docker/Modal/SSH).

## Limite de profundidade e orquestração aninhada {#depth-limit-and-nested-orchestration}

Por padrão, a delegação é **plana**: um pai (profundidade 0) cria filhos (profundidade 1), e esses filhos não podem delegar mais. Isso previne delegação recursiva descontrolada.

Para workflows multi-estágio (pesquisa → síntese, ou orquestração paralela sobre subproblemas), um pai pode criar filhos **orquestradores** que *podem* delegar seus próprios workers:

```python
delegate_task(
    goal="Survey three code review approaches and recommend one",
    role="orchestrator",  # Allows this child to spawn its own workers
    context="...",
)
```

- `role="leaf"` (padrão): filho não pode delegar mais — idêntico ao comportamento de delegação plana.
- `role="orchestrator"`: filho mantém o toolset `delegation`. Limitado por `delegation.max_spawn_depth` (padrão **1** = plano, então `role="orchestrator"` é no-op nos defaults). Aumente `max_spawn_depth` para 2 para permitir que filhos orquestradores criem netos leaf; 3+ para árvores mais profundas. Não há teto superior — custo é o limite prático.
- `delegation.orchestrator_enabled: false`: kill switch global que força todo filho a `leaf` independentemente do parâmetro `role`.

**Aviso de custo:** Com `max_spawn_depth: 3` e `max_concurrent_children: 3`, a árvore pode atingir 3×3×3 = 27 agentes leaf concorrentes. Cada nível extra multiplica o gasto — aumente `max_spawn_depth` intencionalmente.

## Ciclo de vida e durabilidade {#lifetime-and-durability}

:::warning Durabilidade de conclusão em background não é execução durável
Chamadas de `delegate_task` voltadas ao modelo no nível superior rodam em background automaticamente onde a sessão suporta entrega posterior. O Hermes retorna um handle imediatamente, e o resultado reentra na conversa depois que o filho ou batch termina. Subagentes orquestradores esperam seus workers no turno atual porque devem sintetizar esses resultados antes de retornar. Endpoints stateless request/response caem para execução síncrona quando não conseguem entregar um resultado detached depois.

- Mensagens de follow-up normais não cancelam filhos em background. `/stop` cancela delegações em background em execução, e fechar ou redefinir a sessão proprietária descarta seus filhos ativos.
- Fechamento/redefinição explícita de sessão interrompe os filhos em background dessa sessão. Fechar um viewer TUI de uma sessão do gateway não mata o trabalho do gateway.
- Um restart de processo do Hermes **não** retoma um filho em execução. Sua tentativa vira `unknown` porque o Hermes não consegue provar quais efeitos colaterais aconteceram.
- Um filho que completou antes do restart mas cujo resultado não foi entregue é restaurado e roteado de volta pelas verificações normais da sessão proprietária.
- Filhos cancelados retornam um resultado estruturado (`status="interrupted"`, `exit_reason="interrupted"`), mas como o pai também foi interrompido, esse resultado muitas vezes nunca entra em uma resposta visível ao usuário.

Para **execução durável** que deve sobreviver ao fechamento de sessão ou restart de processo, use:

- `cronjob` (action=`create`) — agenda uma execução de agente separada; imune a interrupções de turno do pai.
- `terminal(background=True, notify_on_complete=True)` — comandos shell de longa duração que continuam rodando enquanto o agente faz outras coisas.
:::

## Propriedades principais {#key-properties}

- Cada subagente recebe sua **própria sessão de terminal** (separada do pai)
- Subagentes herdam os toolsets habilitados do pai; o modelo não pode selecioná-los ou ampliá-los por chamada
- **Delegação aninhada é opt-in** — apenas filhos com `role="orchestrator"` podem delegar mais, e só quando `max_spawn_depth` é aumentado do padrão 1 (plano). Desabilite globalmente com `orchestrator_enabled: false`.
- Subagentes leaf **não podem** chamar: `delegate_task`, `clarify`, `memory`, `send_message`, `cronjob`. Subagentes orquestradores mantêm `delegate_task` mas conservam os outros bloqueios. Ambos os roles mantêm `execute_code` (programmatic tool calling) para que filhos possam agrupar trabalho mecânico em vez de queimar iterações de raciocínio.
- **Cancelamento segue propriedade** — `/stop` ou fechar/redefinir a sessão proprietária cancela seus filhos em background; descendentes síncronos sob orquestradores seguem o estado de interrupção do pai
- Apenas o resumo final entra no contexto do pai, mantendo o uso de tokens eficiente
- Subagentes herdam a **API key, configuração de provider e credential pool** do pai (permitindo rotação de chave em rate limits)

## Delegação vs execute_code {#delegation-vs-execute_code}

| Fator | delegate_task | execute_code |
|--------|--------------|-------------|
| **Raciocínio** | Loop completo de raciocínio LLM | Apenas execução de código Python |
| **Contexto** | Conversa isolada nova | Sem conversa, só script |
| **Acesso a ferramentas** | Todas as ferramentas não bloqueadas com raciocínio | 7 ferramentas via RPC, sem raciocínio |
| **Paralelismo** | 3 subagentes concorrentes por padrão (configurável) | Script único |
| **Melhor para** | Tarefas complexas que precisam de julgamento | Pipelines mecânicos multi-etapa |
| **Custo de tokens** | Maior (loop LLM completo) | Menor (só stdout retornado) |
| **Interação com usuário** | Nenhuma (subagentes não podem clarificar) | Nenhuma |

**Regra prática:** Use `delegate_task` quando a subtarefa exige raciocínio, julgamento ou resolução de problemas multi-etapa. Use `execute_code` quando precisar de processamento mecânico de dados ou workflows scriptados.

## Configuração {#configuration}

```yaml
# In ~/.hermes/config.yaml
delegation:
  max_iterations: 50                        # Max turns per child (default: 50)
  # max_concurrent_children: 3              # Parallel children per batch (default: 3)
  # max_spawn_depth: 1                      # Tree depth (floor 1, no ceiling, default 1 = flat). Raise to 2 to allow orchestrator children to spawn leaves; 3+ for deeper trees.
  # orchestrator_enabled: true              # Disable to force all children to leaf role.
  model: "google/gemini-3-flash-preview"             # Optional provider/model override
  provider: "openrouter"                             # Optional built-in provider
  api_mode: anthropic_messages                       # optional; auto-detected from base_url for anthropic_messages endpoints

# Or use a direct custom endpoint instead of provider:
delegation:
  model: "qwen2.5-coder"
  base_url: "http://localhost:1234/v1"
  api_key: "local-key"
  # api_mode: "anthropic_messages"  # Optional. Wire protocol override for base_url ("chat_completions", "codex_responses", or "anthropic_messages"). Empty = auto-detect from URL (e.g. /anthropic suffix). Set explicitly for endpoints the heuristic can't classify (Azure AI Foundry, MiniMax, Zhipu GLM, LiteLLM proxies, …).
```

Quando `base_url` aponta para um endpoint compatível com Anthropic — por exemplo um caminho terminando em `/anthropic`, uma rota Claude do Azure Foundry ou um proxy MiniMax `/anthropic` — `api_mode` é auto-detectado como `anthropic_messages` para o subagente usar o wire format certo sem você configurar nada. Defina `api_mode` explicitamente quando o palpite de auto-detecção estiver errado (raro).

:::tip
O agente lida com delegação automaticamente com base na complexidade da tarefa. Você não precisa pedir explicitamente para delegar — ele fará quando fizer sentido.
:::
