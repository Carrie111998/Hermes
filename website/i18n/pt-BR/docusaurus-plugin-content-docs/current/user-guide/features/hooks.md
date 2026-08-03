---
sidebar_position: 6
title: "Hooks de Eventos"
description: "Execute código personalizado em pontos-chave do ciclo de vida — registre atividades, envie alertas, publique em webhooks"
---

# Hooks de Eventos

O Hermes tem quatro sistemas de hooks que executam código personalizado em pontos-chave do ciclo de vida:

| Sistema | Registrado via | Executa em | Caso de uso |
|--------|---------------|---------|----------|
| **[Hooks de gateway](#gateway-event-hooks)** | `HOOK.yaml` + `handler.py` em `~/.hermes/hooks/` | Somente gateway | Registro de logs, alertas, webhooks |
| **[Hooks de plugin](#plugin-hooks)** | `ctx.register_hook()` em um [plugin](/user-guide/features/plugins) | CLI + Gateway | Interceptação de tools, métricas, guardrails |
| **[Hooks de shell](#shell-hooks)** | bloco `hooks:` em `~/.hermes/config.yaml` apontando para scripts shell | CLI + Gateway | Scripts prontos para uso — bloqueio, formatação automática, injeção de contexto |
| **[Webhooks de saída](#outbound-webhooks)** | lista `hooks.outbound:` em `~/.hermes/config.yaml` | CLI + Gateway | Enviar eventos de ciclo de vida assinados para endpoints HTTP externos — CI, dashboards, outros agentes |

Os quatro sistemas são não bloqueantes — erros em qualquer hook são capturados e registrados em log, nunca derrubando o agente.

## Hooks de Eventos do Gateway {#gateway-event-hooks}

Hooks de gateway disparam automaticamente durante a operação do gateway (Telegram, Discord, Slack, WhatsApp, Teams) sem bloquear o pipeline principal do agente.

### Criando um Hook {#creating-a-hook}

Cada hook é um diretório sob `~/.hermes/hooks/` contendo dois arquivos:

```text
~/.hermes/hooks/
└── my-hook/
    ├── HOOK.yaml      # Declares which events to listen for
    └── handler.py     # Python handler function
```

#### HOOK.yaml {#hookyaml}

```yaml
name: my-hook
description: Log all agent activity to a file
events:
  - agent:start
  - agent:end
  - agent:step
```

A lista `events` determina quais eventos disparam seu handler. Você pode se inscrever em qualquer combinação de eventos, incluindo wildcards como `command:*`.

#### handler.py {#handlerpy}

```python
import json
from datetime import datetime
from pathlib import Path

LOG_FILE = Path.home() / ".hermes" / "hooks" / "my-hook" / "activity.log"

async def handle(event_type: str, context: dict):
    """Called for each subscribed event. Must be named 'handle'."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": event_type,
        **context,
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

**Regras do handler:**
- Deve se chamar `handle`
- Recebe `event_type` (string) e `context` (dict)
- Pode ser `async def` ou `def` normal — ambos funcionam
- Erros são capturados e registrados em log, nunca derrubando o agente

### Eventos Disponíveis {#available-events}

| Evento | Quando dispara | Chaves de contexto |
|-------|---------------|--------------|
| `gateway:startup` | Processo do gateway inicia | `platforms` (lista de nomes das plataformas ativas) |
| `session:start` | Nova sessão de mensageria criada | `platform`, `user_id`, `session_id`, `session_key` |
| `session:end` | Sessão encerrada (antes do reset) | `platform`, `user_id`, `session_key` |
| `session:reset` | Usuário executou `/new` ou `/reset` | `platform`, `user_id`, `session_key` |
| `session:compress` | Compressão de contexto concluída para uma sessão | `platform`, `session_id`, `old_session_id` (vazio quando compactado no local), `in_place` (bool — `true` = transcrição compactada no mesmo id, `false` = rotacionado a partir de `old_session_id`), `compression_count` |
| `agent:start` | Agente começa a processar uma mensagem | `platform`, `user_id`, `chat_id`, `thread_id` (id do tópico do fórum / raiz da thread; vazio quando não está em uma thread), `chat_type` (`"dm"` \| `"group"` \| `"forum"`; vazio se desconhecido), `session_id`, `message` (truncado em 500 caracteres) |
| `agent:step` | Cada iteração do loop de chamadas de tool | `platform`, `user_id`, `session_id`, `iteration`, `tool_names` |
| `agent:end` | Agente termina de processar | mesmas chaves de `agent:start`, mais `response` (truncado em 500 caracteres) |
| `reaction:added` | Uma reação de emoji foi adicionada a uma mensagem que o bot pode ver (atualmente o adaptador do Slack). Requer o escopo `reactions:read` + a inscrição no evento de bot `reaction_added`; o bot precisa ser membro do canal. | `platform`, `reaction`, `user_id`, `item_user_id`, `item_type`, `channel_id`, `message_ts`, `team_id`, `event_ts`, `raw_event` |
| `reaction:removed` | Uma reação de emoji foi removida de uma mensagem que o bot pode ver. Requer a inscrição no evento de bot `reaction_removed`. | mesmo formato de `reaction:added` |
| `command:*` | Qualquer comando de barra executado | `platform`, `user_id`, `command`, `args` |

#### Correspondência de Wildcard {#wildcard-matching}

Handlers registrados para `command:*` disparam para qualquer evento `command:` (`command:model`, `command:reset`, etc.). Monitore todos os comandos de barra com uma única inscrição.

:::tip Respostas em thread
Um handler que publica uma mensagem de acompanhamento no mesmo tópico de fórum do Telegram deve incluir `message_thread_id=int(thread_id)` quando `chat_type == "forum"` e `thread_id` não estiver vazio.
:::

### Exemplos {#examples}

#### Alerta no Telegram para Tarefas Longas {#telegram-alert-on-long-tasks}

Envie uma mensagem para si mesmo quando o agente executar mais de 10 passos:

```yaml
# ~/.hermes/hooks/long-task-alert/HOOK.yaml
name: long-task-alert
description: Alert when agent is taking many steps
events:
  - agent:step
```

```python
# ~/.hermes/hooks/long-task-alert/handler.py
import os
import httpx

THRESHOLD = 10
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_HOME_CHANNEL")

async def handle(event_type: str, context: dict):
    iteration = context.get("iteration", 0)
    if iteration == THRESHOLD and BOT_TOKEN and CHAT_ID:
        tools = ", ".join(context.get("tool_names", []))
        text = f"⚠️ Agent has been running for {iteration} steps. Last tools: {tools}"
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text},
            )
```

#### Registrador de Uso de Comandos {#command-usage-logger}

Rastreie quais comandos de barra são usados:

```yaml
# ~/.hermes/hooks/command-logger/HOOK.yaml
name: command-logger
description: Log slash command usage
events:
  - command:*
```

```python
# ~/.hermes/hooks/command-logger/handler.py
import json
from datetime import datetime
from pathlib import Path

LOG = Path.home() / ".hermes" / "logs" / "command_usage.jsonl"

def handle(event_type: str, context: dict):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(),
        "command": context.get("command"),
        "args": context.get("args"),
        "platform": context.get("platform"),
        "user": context.get("user_id"),
    }
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

#### Webhook de Início de Sessão {#session-start-webhook}

Faça um POST para um serviço externo em novas sessões:

```yaml
# ~/.hermes/hooks/session-webhook/HOOK.yaml
name: session-webhook
description: Notify external service on new sessions
events:
  - session:start
  - session:reset
```

```python
# ~/.hermes/hooks/session-webhook/handler.py
import httpx

WEBHOOK_URL = "https://your-service.example.com/hermes-events"

async def handle(event_type: str, context: dict):
    async with httpx.AsyncClient() as client:
        await client.post(WEBHOOK_URL, json={
            "event": event_type,
            **context,
        }, timeout=5)
```

### Tutorial: BOOT.md — Execute uma Checklist de Inicialização a Cada Boot do Gateway {#tutorial-bootmd--run-a-startup-checklist-on-every-gateway-boot}

Um padrão popular da comunidade: coloque uma checklist em Markdown em `~/.hermes/BOOT.md` e faça o agente executá-la uma vez toda vez que o gateway iniciar. Útil para "a cada boot, verifique falhas de cron durante a noite e me avise no Discord se algo falhou", ou "resuma as últimas 24h de deploy.log e publique no Slack #ops".

Este tutorial mostra como construir isso você mesmo como um hook definido pelo usuário. O Hermes não vem com um hook BOOT.md embutido — você monta exatamente o comportamento que quiser.

#### O que vamos construir {#what-were-building}

1. Um arquivo em `~/.hermes/BOOT.md` com instruções de inicialização em linguagem natural.
2. Um hook de gateway que dispara em `gateway:startup`, cria um agente único (one-shot) com o modelo/credenciais resolvidos do seu gateway, e executa as instruções do BOOT.md.
3. Uma convenção `[SILENT]` para que o agente possa optar por não enviar uma mensagem quando não houver nada a relatar.

#### Passo 1: Escreva sua checklist {#step-1-write-your-checklist}

Crie `~/.hermes/BOOT.md`. Escreva como se estivesse dando instruções para um assistente humano:

```markdown
# Startup Checklist

1. Run `hermes cron list` and check if any scheduled jobs failed overnight.
2. If any failed, summarize them for Discord #ops (the hook delivers your final response to its configured target).
3. Check if `/opt/app/deploy.log` has any ERROR lines from the last 24 hours. If yes, summarize them and include in the same report.
4. If nothing went wrong, reply with only `[SILENT]` so no message is sent.
```

O agente vê isso como parte do seu prompt, então qualquer coisa que você conseguir descrever em linguagem simples funciona — chamadas de tool, comandos de shell, envio de mensagens, resumo de arquivos.

#### Passo 2: Crie o hook {#step-2-create-the-hook}

```text
~/.hermes/hooks/boot-md/
├── HOOK.yaml
└── handler.py
```

**`~/.hermes/hooks/boot-md/HOOK.yaml`**

```yaml
name: boot-md
description: Run ~/.hermes/BOOT.md on gateway startup
events:
  - gateway:startup
```

**`~/.hermes/hooks/boot-md/handler.py`**

```python
"""Run ~/.hermes/BOOT.md on every gateway startup."""

import logging
import threading
from pathlib import Path

logger = logging.getLogger("hooks.boot-md")

BOOT_FILE = Path.home() / ".hermes" / "BOOT.md"


def _build_prompt(content: str) -> str:
    return (
        "You are running a startup boot checklist. Follow the instructions "
        "below exactly.\n\n"
        "---\n"
        f"{content}\n"
        "---\n\n"
        "Execute each instruction. Put any user-facing summary in your "
        "final response — the hook delivers it to the configured channel "
        "(e.g. Discord or Slack); you do not send messages yourself.\n"
        "If nothing needs attention and there is nothing to report, reply "
        "with ONLY: [SILENT]"
    )


def _run_boot_agent(content: str) -> None:
    """Spawn a one-shot agent and execute the checklist.

    Uses the gateway's resolved model and runtime credentials so this works
    against custom endpoints, aggregators, and OAuth-based providers alike.
    """
    try:
        from gateway.run import _resolve_gateway_model, _resolve_runtime_agent_kwargs
        from run_agent import AIAgent

        agent = AIAgent(
            model=_resolve_gateway_model(),
            **_resolve_runtime_agent_kwargs(),
            platform="gateway",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=20,
        )
        result = agent.run_conversation(_build_prompt(content))
        response = (result.get("final_response", "") or "").strip()
        if response.upper() not in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}:
            logger.info("boot-md completed: %s", response[:200])
        else:
            logger.info("boot-md completed (nothing to report)")
    except Exception as e:
        logger.error("boot-md agent failed: %s", e)


async def handle(event_type: str, context: dict) -> None:
    if not BOOT_FILE.exists():
        return
    content = BOOT_FILE.read_text(encoding="utf-8").strip()
    if not content:
        return

    logger.info("Running BOOT.md (%d chars)", len(content))

    # Background thread so gateway startup isn't blocked on a full agent turn.
    thread = threading.Thread(
        target=_run_boot_agent,
        args=(content,),
        name="boot-md",
        daemon=True,
    )
    thread.start()
```

As duas linhas-chave:

- `_resolve_gateway_model()` lê o modelo atualmente configurado do gateway.
- `_resolve_runtime_agent_kwargs()` resolve as credenciais do provedor da mesma forma que um turno normal do gateway faz — incluindo chaves de API, URLs base, tokens OAuth e pools de credenciais.

Sem elas, um `AIAgent()` simples cai nos padrões embutidos e vai retornar 401 contra qualquer endpoint que não seja o padrão.

#### Passo 3: Teste {#step-3-test-it}

Reinicie o gateway:

```bash
hermes gateway restart
```

Observe os logs:

```bash
hermes logs --follow --level INFO | grep boot-md
```

Você deve ver `Running BOOT.md (N chars)` seguido de `boot-md completed: ...` (resumo do que o agente fez) ou `boot-md completed (nothing to report)` quando o agente responder com um token de silêncio exato como `[SILENT]`.

Exclua `~/.hermes/BOOT.md` para desativar a checklist — o hook continua carregado, mas é silenciosamente ignorado quando o arquivo não está presente.

#### Estendendo o padrão {#extending-the-pattern}

- **Checklists sensíveis ao horário:** baseie-se em `datetime.now().weekday()` dentro das instruções do BOOT.md ("se for segunda-feira, verifique também o log de deploy semanal"). As instruções são texto livre, então qualquer coisa sobre a qual o agente consiga raciocinar é válida.
- **Múltiplas checklists:** aponte o hook para um arquivo diferente (`STARTUP.md`, `MORNING.md`, etc.) e registre diretórios de hook separados para cada um.
- **Variante sem agente:** se você não precisa de um loop completo de agente, pule o `AIAgent` por completo e faça o handler publicar uma notificação fixa diretamente via `httpx`. Mais barato, mais rápido e sem dependência de provedor.

#### Por que isso não é um recurso embutido {#why-this-isnt-a-built-in}

Uma versão anterior do Hermes trazia isso como um hook embutido e criava silenciosamente um agente com padrões básicos a cada boot do gateway. Isso surpreendia usuários com endpoints customizados e tornava o recurso invisível para quem não sabia que ele estava rodando. Mantê-lo como um padrão documentado — construído por você, no seu diretório de hooks — significa que você vê exatamente o que ele faz e opta por usá-lo ao escrever os arquivos.

### Como Funciona {#how-it-works}

1. Na inicialização do gateway, `HookRegistry.discover_and_load()` varre `~/.hermes/hooks/`
2. Cada subdiretório com `HOOK.yaml` + `handler.py` é carregado dinamicamente
3. Os handlers são registrados para seus eventos declarados
4. Em cada ponto do ciclo de vida, `hooks.emit()` dispara todos os handlers correspondentes
5. Erros em qualquer handler são capturados e registrados em log — um hook quebrado nunca derruba o agente

:::info
Hooks de gateway só disparam no **gateway** (Telegram, Discord, Slack, WhatsApp, Teams). A CLI não carrega hooks de gateway. Para hooks que funcionam em todo lugar, use [hooks de plugin](#plugin-hooks).
:::

## Hooks de Plugin {#plugin-hooks}

[Plugins](/user-guide/features/plugins) podem registrar hooks que disparam em sessões de **CLI e gateway**. Eles são registrados de forma programática via `ctx.register_hook()` na função `register()` do seu plugin.

Para detalhes de empacotamento e registro de plugins, veja
o [guia de Plugins](/docs/user-guide/features/plugins).

```python
def register(ctx):
    ctx.register_hook("pre_tool_call", my_tool_observer)
    ctx.register_hook("post_tool_call", my_tool_logger)
    ctx.register_hook("pre_llm_call", my_memory_callback)
    ctx.register_hook("post_llm_call", my_sync_callback)
    ctx.register_hook("on_session_start", my_init_callback)
    ctx.register_hook("on_session_end", my_cleanup_callback)
    # Kanban board lifecycle (fire after the board DB change commits):
    ctx.register_hook("kanban_task_claimed", my_claim_callback)     # dispatcher process
    ctx.register_hook("kanban_task_completed", my_done_callback)    # worker process
    ctx.register_hook("kanban_task_blocked", my_blocked_callback)   # worker process
```

**Regras gerais para todos os hooks:**

- Callbacks recebem **argumentos nomeados (keyword arguments)**. Sempre aceite `**kwargs` para compatibilidade futura — novos parâmetros podem ser adicionados em versões futuras sem quebrar seu plugin.
- Se um callback **falhar**, ele é registrado em log e ignorado. Outros hooks e o agente continuam normalmente. Um plugin com comportamento inadequado nunca consegue quebrar o agente.
- Os valores de retorno de dois hooks afetam o comportamento: [`pre_tool_call`](#pre_tool_call) pode **bloquear** a tool, e [`pre_llm_call`](#pre_llm_call) pode **injetar contexto** na chamada ao LLM. Todos os outros hooks são observadores do tipo fire-and-forget.
- Callbacks observadores recebem `telemetry_schema_version` automaticamente. Quando presente, `turn_id`, `api_request_id`, `task_id`, `session_id` e `api_call_count` são campos de correlação separados. Trate `api_request_id` como um identificador opaco; não faça parsing do seu formato de string.

### Referência rápida {#quick-reference}

| Hook | Dispara quando | Retorna |
|------|-----------|---------|
| [`pre_tool_call`](#pre_tool_call) | Antes de qualquer tool executar | `{"action": "block", "message": str}` para vetar a chamada |
| [`post_tool_call`](#post_tool_call) | Depois que qualquer tool retorna | ignorado |
| [`pre_llm_call`](#pre_llm_call) | Uma vez por turno, antes do loop de chamadas de tool | `{"context": str}` para inserir contexto antes da mensagem do usuário |
| [`post_llm_call`](#post_llm_call) | Uma vez por turno, depois do loop de chamadas de tool | ignorado |
| [`pre_verify`](#pre_verify) | Uma vez por turno quando o agente edita código, antes de verificar/finalizar | `{"action": "continue", "message": str}` para continuar |
| [`on_session_start`](#on_session_start) | Nova sessão criada (apenas no primeiro turno) | ignorado |
| [`on_session_end`](#on_session_end) | Sessão termina | ignorado |
| [`on_session_finalize`](#on_session_finalize) | CLI/gateway desmonta uma sessão ativa (flush, salvar, estatísticas) | ignorado |
| [`on_session_reset`](#on_session_reset) | Gateway troca para uma nova chave de sessão (ex.: `/new`, `/reset`) | ignorado |
| [`subagent_start`](#subagent_start) | Um filho de `delegate_task` foi construído e está prestes a rodar | ignorado |
| [`subagent_stop`](#subagent_stop) | Um filho de `delegate_task` terminou | ignorado |
| [`pre_gateway_dispatch`](#pre_gateway_dispatch) | Gateway recebeu uma mensagem do usuário, antes de auth + dispatch | `{"action": "skip" \| "rewrite" \| "allow", ...}` para influenciar o fluxo |
| [`pre_approval_request`](#pre_approval_request) | Uma decisão de aprovação é solicitada, incluindo decisões automáticas do modo smart | ignorado |
| [`post_approval_response`](#post_approval_response) | Uma decisão de aprovação é tomada (ou um prompt expira) | ignorado |
| [`transform_tool_result`](#transform_tool_result) | Depois que qualquer tool retorna, antes do resultado ser devolvido ao modelo | `str` para substituir o resultado, `None` para deixar inalterado |
| [`transform_terminal_output`](#transform_terminal_output) | Dentro da tool `terminal`, antes da truncagem/remoção de ANSI/redação | `str` para substituir a saída bruta, `None` para deixar inalterado |
| [`transform_llm_output`](#transform_llm_output) | Depois que o loop de chamadas de tool termina, antes da resposta final ser entregue | `str` para substituir o texto da resposta, `None`/vazio para deixar inalterado |

---

### `pre_tool_call` {#pre_tool_call}

Dispara **imediatamente antes** de cada execução de tool — tools embutidas e tools de plugin igualmente.

**Assinatura do callback:**

```python
def my_callback(tool_name: str, args: dict, task_id: str, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `tool_name` | `str` | Nome da tool que está prestes a executar (ex.: `"terminal"`, `"web_search"`, `"read_file"`) |
| `args` | `dict` | Os argumentos que o modelo passou para a tool |
| `task_id` | `str` | Identificador de sessão/tarefa. String vazia se não definido. |

**Dispara:** Em `model_tools.py`, dentro de `handle_function_call()`, antes do handler da tool rodar. Dispara uma vez por chamada de tool — se o modelo chamar 3 tools em paralelo, isso dispara 3 vezes.

**Valor de retorno — vetar a chamada:**

```python
return {"action": "block", "message": "Reason the tool call was blocked"}
```

O agente interrompe a tool com `message` como o erro retornado ao modelo. A primeira diretiva de bloqueio correspondente vence (plugins Python registrados primeiro, depois hooks de shell). Qualquer outro valor de retorno é ignorado, então callbacks apenas observadores existentes continuam funcionando sem alteração.

**Casos de uso:** Registro de logs, trilhas de auditoria, contadores de chamadas de tool, bloqueio de operações perigosas, limitação de taxa (rate limiting), aplicação de políticas por usuário.

**Exemplo — log de auditoria de chamadas de tool:**

```python
import json, logging
from datetime import datetime

logger = logging.getLogger(__name__)

def audit_tool_call(tool_name, args, task_id, **kwargs):
    logger.info("TOOL_CALL session=%s tool=%s args=%s",
                task_id, tool_name, json.dumps(args)[:200])

def register(ctx):
    ctx.register_hook("pre_tool_call", audit_tool_call)
```

**Exemplo — avisar sobre tools perigosas:**

```python
DANGEROUS = {"terminal", "write_file", "patch"}

def warn_dangerous(tool_name, **kwargs):
    if tool_name in DANGEROUS:
        print(f"⚠ Executing potentially dangerous tool: {tool_name}")

def register(ctx):
    ctx.register_hook("pre_tool_call", warn_dangerous)
```

---

### `post_tool_call` {#post_tool_call}

Dispara **imediatamente depois** que cada execução de tool retorna.

**Assinatura do callback:**

```python
def my_callback(tool_name: str, args: dict, result: str, task_id: str,
                duration_ms: int, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `tool_name` | `str` | Nome da tool que acabou de executar |
| `args` | `dict` | Os argumentos que o modelo passou para a tool |
| `result` | `str` | O valor de retorno da tool (sempre uma string JSON) |
| `task_id` | `str` | Identificador de sessão/tarefa. String vazia se não definido. |
| `duration_ms` | `int` | Quanto tempo o dispatch da tool levou, em milissegundos (medido com `time.monotonic()` em torno de `registry.dispatch()`). |

**Dispara:** Em `model_tools.py`, dentro de `handle_function_call()`, depois que o handler da tool retorna. Dispara uma vez por chamada de tool. **Não** dispara se a tool lançar uma exceção não tratada (o erro é capturado e retornado como uma string JSON de erro, e `post_tool_call` dispara com essa string de erro como `result`).

**Valor de retorno:** Ignorado.

**Casos de uso:** Registro de resultados de tools, coleta de métricas, rastreamento de taxas de sucesso/falha das tools, dashboards de latência, alertas de orçamento por tool, envio de notificações quando tools específicas terminam.

**Exemplo — rastrear métricas de uso de tools:**

```python
from collections import Counter, defaultdict
import json

_tool_counts = Counter()
_error_counts = Counter()
_latency_ms = defaultdict(list)

def track_metrics(tool_name, result, duration_ms=0, **kwargs):
    _tool_counts[tool_name] += 1
    _latency_ms[tool_name].append(duration_ms)
    try:
        parsed = json.loads(result)
        if "error" in parsed:
            _error_counts[tool_name] += 1
    except (json.JSONDecodeError, TypeError):
        pass

def register(ctx):
    ctx.register_hook("post_tool_call", track_metrics)
```

---

### `pre_llm_call` {#pre_llm_call}

Dispara **uma vez por turno**, antes do loop de chamadas de tool começar. Este é o **único hook cujo valor de retorno é usado** — ele pode injetar contexto na mensagem do usuário do turno atual.

**Assinatura do callback:**

```python
def my_callback(session_id: str, user_message: str, conversation_history: list,
                is_first_turn: bool, model: str, platform: str, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `session_id` | `str` | Identificador único para a sessão atual |
| `user_message` | `str` | A mensagem original do usuário para este turno (antes de qualquer injeção de skill) |
| `conversation_history` | `list` | Cópia da lista completa de mensagens (formato OpenAI: `[{"role": "user", "content": "..."}]`) |
| `is_first_turn` | `bool` | `True` se este for o primeiro turno de uma nova sessão, `False` nos turnos seguintes |
| `model` | `str` | O identificador do modelo (ex.: `"anthropic/claude-sonnet-4.6"`) |
| `platform` | `str` | Onde a sessão está rodando: `"cli"`, `"telegram"`, `"discord"`, etc. |

**Dispara:** Em `run_agent.py`, dentro de `run_conversation()`, depois da compressão de contexto mas antes do loop `while` principal. Dispara uma vez por chamada de `run_conversation()` (ou seja, uma vez por turno do usuário), não uma vez por chamada de API dentro do loop de tools.

**Valor de retorno:** Se o callback retornar um dict com uma chave `"context"`, ou uma string simples não vazia, o texto é anexado à mensagem do usuário do turno atual. Retorne `None` para não injetar nada.

```python
# Inject context
return {"context": "Recalled memories:\n- User likes Python\n- Working on hermes-agent"}

# Plain string (equivalent)
return "Recalled memories:\n- User likes Python"

# No injection
return None
```

**Onde o contexto é injetado:** Sempre na **mensagem do usuário**, nunca no system prompt. Isso preserva o cache de prompt — o system prompt permanece idêntico entre turnos, então os tokens em cache são reutilizados. O system prompt é território do Hermes (orientação do modelo, aplicação de tools, personalidade, skills). Plugins contribuem com contexto ao lado da entrada do usuário.

Todo contexto injetado é **efêmero** — adicionado apenas no momento da chamada de API. A mensagem original do usuário no histórico de conversa nunca é alterada, e nada é persistido no banco de dados da sessão.

Quando **múltiplos plugins** retornam contexto, suas saídas são unidas com quebras de linha duplas na ordem de descoberta dos plugins (alfabética por nome de diretório).

**Casos de uso:** Recuperação de memória, injeção de contexto RAG, guardrails, analytics por turno.

**Exemplo — recuperação de memória:**

```python
import httpx

MEMORY_API = "https://your-memory-api.example.com"

def recall(session_id, user_message, is_first_turn, **kwargs):
    try:
        resp = httpx.post(f"{MEMORY_API}/recall", json={
            "session_id": session_id,
            "query": user_message,
        }, timeout=3)
        memories = resp.json().get("results", [])
        if not memories:
            return None
        text = "Recalled context:\n" + "\n".join(f"- {m['text']}" for m in memories)
        return {"context": text}
    except Exception:
        return None

def register(ctx):
    ctx.register_hook("pre_llm_call", recall)
```

**Exemplo — guardrails:**

```python
POLICY = "Never execute commands that delete files without explicit user confirmation."

def guardrails(**kwargs):
    return {"context": POLICY}

def register(ctx):
    ctx.register_hook("pre_llm_call", guardrails)
```

---

### `post_llm_call` {#post_llm_call}

Dispara **uma vez por turno**, depois que o loop de chamadas de tool termina e o agente produziu uma resposta final. Só dispara em turnos **bem-sucedidos** — não dispara se o turno foi interrompido.

**Assinatura do callback:**

```python
def my_callback(session_id: str, user_message: str, assistant_response: str,
                conversation_history: list, model: str, platform: str, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `session_id` | `str` | Identificador único para a sessão atual |
| `user_message` | `str` | A mensagem original do usuário para este turno |
| `assistant_response` | `str` | A resposta final em texto do agente para este turno |
| `conversation_history` | `list` | Cópia da lista completa de mensagens depois que o turno terminou |
| `model` | `str` | O identificador do modelo |
| `platform` | `str` | Onde a sessão está rodando |

**Dispara:** Em `run_agent.py`, dentro de `run_conversation()`, depois que o loop de tools termina com uma resposta final. Protegido por `if final_response and not interrupted` — então **não** dispara quando o usuário interrompe no meio do turno ou o agente atinge o limite de iterações sem produzir uma resposta.

**Valor de retorno:** Ignorado.

**Casos de uso:** Sincronizar dados de conversa com um sistema de memória externo, calcular métricas de qualidade de resposta, registrar resumos de turnos, disparar ações de acompanhamento.

**Exemplo — sincronizar com memória externa:**

```python
import httpx

MEMORY_API = "https://your-memory-api.example.com"

def sync_memory(session_id, user_message, assistant_response, **kwargs):
    try:
        httpx.post(f"{MEMORY_API}/store", json={
            "session_id": session_id,
            "user": user_message,
            "assistant": assistant_response,
        }, timeout=5)
    except Exception:
        pass  # best-effort

def register(ctx):
    ctx.register_hook("post_llm_call", sync_memory)
```

**Exemplo — rastrear tamanhos de resposta:**

```python
import logging
logger = logging.getLogger(__name__)

def log_response_length(session_id, assistant_response, model, **kwargs):
    logger.info("RESPONSE session=%s model=%s chars=%d",
                session_id, model, len(assistant_response or ""))

def register(ctx):
    ctx.register_hook("post_llm_call", log_response_length)
```

---

### `pre_verify` {#pre_verify}

Dispara **uma vez por turno quando o agente editou código**, pouco antes de terminar (depois da proteção embutida de verificação ao parar). Este é um portão de política do usuário/plugin: um callback pode manter o agente continuando — rodar uma verificação, adiá-la, organizar o diff — em vez de deixá-lo parar.

A orientação de verificação que acompanha o Hermes não é um hook `pre_verify` padrão. Ela é anexada ao empurrão (nudge) de verificação ao parar baseado em evidências quando o código editado carece de evidência de verificação recente, então não cria um segundo caminho padrão de continuação. Defina `agent.verify_guidance: false` para manter esse empurrão de evidência embutido conciso.

**Assinatura do callback:**

```python
def my_callback(session_id: str, platform: str, model: str, coding: bool,
                attempt: int, final_response: str, changed_paths: list, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `session_id` | `str` | Identificador único para a sessão atual |
| `platform` | `str` | Onde a sessão está rodando (`"cli"`, `"telegram"`, …) |
| `model` | `str` | O identificador do modelo |
| `coding` | `bool` | Se o turno está na postura de codificação (em um workspace de código) — escopeie seu hook nisso |
| `attempt` | `int` | Quantas vezes este turno já recebeu um empurrão (0 na primeira) — autolimite-se com base nisso |
| `final_response` | `str` | A resposta que o agente está prestes a entregar |
| `changed_paths` | `list` | Arquivos que o agente editou neste turno (ordenados, sempre não vazio aqui) |

Escopeie um hook ao contexto de codificação verificando `coding` e torne-o de disparo único usando `attempt` (hooks de shell leem ambos de `.extra`), da mesma forma que um hook `pre_tool_call` se escopeia em `tool_name` — assim você pode registrar vários hooks `pre_verify`, cada um disparando apenas onde deveria.

**Dispara:** Em `agent/conversation_loop.py`, no ponto em que o agente aceitaria uma resposta final, imediatamente depois da verificação de verificar-ao-parar — mas apenas quando o agente editou código neste turno e pelo menos um hook `pre_verify` está registrado.

**Valor de retorno — manter o agente continuando:**

```python
return {"action": "continue", "message": "Run the formatter on your changes, then finish."}
```

A `message` é anexada como um turno de usuário sintético e o loop roda novamente. O formato Stop do Claude-Code (`{"decision": "block", "reason": "..."}`, onde bloquear a parada significa *continuar*) também é aceito. Uma diretiva sem mensagem — ou qualquer outro retorno — deixa o turno terminar.

**Limitado:** diretivas consecutivas de continuar em um turno são limitadas por `agent.max_verify_nudges` (padrão 3), então um hook que sempre diz para continuar nunca consegue prender o loop. A resposta tentada é mantida no histórico, mas não é exibida ao usuário enquanto o agente está sendo empurrado.

**Torne-o idempotente:** o hook dispara novamente depois de cada empurrão, então proteja usando `attempt` (`if attempt: return None`) — caso contrário ele só vai empurrando até o limite ser atingido.

**Casos de uso:** adiar testes/lints durante iteração criativa, exigir checagens verdes para certos caminhos, bloquear "concluído" até que exista uma entrada de changelog, executar uma checklist de verificação específica do projeto.

**Exemplo — adiar checagens em trabalho criativo de UI, escopado + disparo único:**

```python
UI = (".tsx", ".jsx", ".css", ".scss")

def defer_ui_checks(coding, attempt, changed_paths, **kwargs):
    if attempt or not coding:
        return None  # one-shot, coding only
    if not all(p.endswith(UI) for p in changed_paths):
        return None  # only pure-UI edits
    return {
        "action": "continue",
        "message": "This is UI work — don't run tests/lints yet; ask the user to "
                   "eyeball it first, and clean the diff before any commit.",
    }

def register(ctx):
    ctx.register_hook("pre_verify", defer_ui_checks)
```

Para orientação permanente que deve moldar o empurrão embutido de evidência ausente, use `agent.verify_guidance`. Para regras mais amplas de postura de codificação que não precisam *bloquear* a verificação, prefira `agent.coding_instructions` em `config.yaml` — ela acompanha o briefing de codificação e não custa um turno extra.

---

### `on_session_start` {#on_session_start}

Dispara **uma vez** quando uma sessão totalmente nova é criada. **Não** dispara na continuação de sessão (quando o usuário envia uma segunda mensagem em uma sessão existente).

**Assinatura do callback:**

```python
def my_callback(session_id: str, model: str, platform: str, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `session_id` | `str` | Identificador único para a nova sessão |
| `model` | `str` | O identificador do modelo |
| `platform` | `str` | Onde a sessão está rodando |

**Dispara:** Em `run_agent.py`, dentro de `run_conversation()`, durante o primeiro turno de uma nova sessão — especificamente depois que o system prompt é construído mas antes do loop de tools começar. A verificação é `if not conversation_history` (sem mensagens anteriores = nova sessão).

**Valor de retorno:** Ignorado.

**Casos de uso:** Inicializar estado com escopo de sessão, aquecer caches, registrar a sessão em um serviço externo, registrar em log o início de sessões.

**Exemplo — inicializar um cache de sessão:**

```python
_session_caches = {}

def init_session(session_id, model, platform, **kwargs):
    _session_caches[session_id] = {
        "model": model,
        "platform": platform,
        "tool_calls": 0,
        "started": __import__("datetime").datetime.now().isoformat(),
    }

def register(ctx):
    ctx.register_hook("on_session_start", init_session)
```

---

### `on_session_end` {#on_session_end}

Dispara no **exato final** de cada chamada de `run_conversation()`, independente do resultado. Também dispara a partir do handler de saída da CLI se o agente estava no meio de um turno quando o usuário saiu.

**Assinatura do callback:**

```python
def my_callback(session_id: str, completed: bool, interrupted: bool,
                model: str, platform: str, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `session_id` | `str` | Identificador único para a sessão |
| `completed` | `bool` | `True` se o agente produziu uma resposta final, `False` caso contrário |
| `interrupted` | `bool` | `True` se o turno foi interrompido (usuário enviou nova mensagem, `/stop`, ou saiu) |
| `model` | `str` | O identificador do modelo |
| `platform` | `str` | Onde a sessão está rodando |

**Dispara:** Em dois lugares:
1. **`run_agent.py`** — no final de cada chamada de `run_conversation()`, depois de toda a limpeza. Sempre dispara, mesmo se o turno teve erro.
2. **`cli.py`** — no handler atexit da CLI, mas **somente** se o agente estava no meio de um turno (`_agent_running=True`) quando a saída ocorreu. Isso captura Ctrl+C e `/exit` durante o processamento. Nesse caso, `completed=False` e `interrupted=True`.

**Valor de retorno:** Ignorado.

**Casos de uso:** Descarregar (flush) buffers, fechar conexões, persistir estado de sessão, registrar em log a duração da sessão, limpeza de recursos inicializados em `on_session_start`.

**Exemplo — flush e limpeza:**

```python
_session_caches = {}

def cleanup_session(session_id, completed, interrupted, **kwargs):
    cache = _session_caches.pop(session_id, None)
    if cache:
        # Flush accumulated data to disk or external service
        status = "completed" if completed else ("interrupted" if interrupted else "failed")
        print(f"Session {session_id} ended: {status}, {cache['tool_calls']} tool calls")

def register(ctx):
    ctx.register_hook("on_session_end", cleanup_session)
```

**Exemplo — rastreamento de duração de sessão:**

```python
import time, logging
logger = logging.getLogger(__name__)

_start_times = {}

def on_start(session_id, **kwargs):
    _start_times[session_id] = time.time()

def on_end(session_id, completed, interrupted, **kwargs):
    start = _start_times.pop(session_id, None)
    if start:
        duration = time.time() - start
        logger.info("SESSION_DURATION session=%s seconds=%.1f completed=%s interrupted=%s",
                     session_id, duration, completed, interrupted)

def register(ctx):
    ctx.register_hook("on_session_start", on_start)
    ctx.register_hook("on_session_end", on_end)
```

---

### `on_session_finalize` {#on_session_finalize}

Dispara quando a CLI ou o gateway **desmonta** uma sessão ativa — por exemplo, quando o usuário executa `/new`, o gateway faz o GC de uma sessão ociosa, ou a CLI é encerrada com um agente ativo. Esta é a última chance de fazer flush do estado ligado à sessão que está saindo antes que sua identidade se perca.

**Assinatura do callback:**

```python
def my_callback(session_id: str | None, platform: str, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `session_id` | `str` ou `None` | O ID da sessão que está saindo. Pode ser `None` se não havia sessão ativa. |
| `platform` | `str` | `"cli"` ou o nome da plataforma de mensageria (`"telegram"`, `"discord"`, etc.). |

**Dispara:** Em `cli.py` (em `/new` / saída da CLI) e `gateway/run.py` (quando uma sessão é resetada ou passa por GC). Sempre emparelhado com `on_session_reset` do lado do gateway.

**Valor de retorno:** Ignorado.

**Casos de uso:** Persistir métricas finais da sessão antes que o ID da sessão seja descartado, fechar recursos por sessão, emitir um evento final de telemetria, drenar escritas enfileiradas.

---

### `on_session_reset` {#on_session_reset}

Dispara quando o gateway **troca para uma nova chave de sessão** em um chat ativo — o usuário invocou `/new`, `/reset`, `/clear`, ou o adaptador escolheu uma sessão nova depois de uma janela de inatividade. Isso permite que plugins reajam ao fato de que o estado da conversa foi apagado, sem esperar pelo próximo `on_session_start`.

**Assinatura do callback:**

```python
def my_callback(session_id: str, platform: str, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `session_id` | `str` | O ID da nova sessão (já rotacionado para o novo valor). |
| `platform` | `str` | O nome da plataforma de mensageria. |

**Dispara:** Em `gateway/run.py`, imediatamente depois que a nova chave de sessão é alocada, mas antes que a próxima mensagem de entrada seja processada. No gateway, a ordem é: `on_session_finalize(old_id)` → troca → `on_session_reset(new_id)` → `on_session_start(new_id)` no primeiro turno de entrada.

**Valor de retorno:** Ignorado.

**Casos de uso:** Resetar caches por sessão indexados por `session_id`, emitir analytics de "sessão rotacionada", preparar um novo bucket de estado.

---

Veja o **[guia de Construção de um Plugin](/developer-guide/plugins)** para o passo a passo completo incluindo schemas de tool, handlers e padrões avançados de hooks.

---

### `subagent_start` {#subagent_start}

Dispara **uma vez por agente filho** depois que `delegate_task` construiu o `AIAgent` filho e antes que esse filho seja executado. Seja você delegando uma única tarefa ou um lote de três, este hook dispara uma vez para cada filho.

Este hook é específico do ciclo de vida de delegação/subagente. Não é um portão universal de "antes de qualquer invocação de agente" para execuções de agente originadas de gateway, CLI, cron, batch, MoA ou outros executores.

**Assinatura do callback:**

```python
def my_callback(parent_session_id: str | None,
                parent_turn_id: str,
                parent_subagent_id: str | None,
                child_session_id: str | None,
                child_subagent_id: str,
                child_role: str,
                child_goal: str,
                **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `parent_session_id` | `str \| None` | ID de sessão do agente pai que está delegando. |
| `parent_turn_id` | `str` | ID do turno do agente pai que solicitou a delegação, se disponível. |
| `parent_subagent_id` | `str \| None` | ID do subagente pai quando este filho foi criado por outro subagente; `None` para agentes pais de nível superior. |
| `child_session_id` | `str \| None` | ID de sessão alocado para o agente filho. |
| `child_subagent_id` | `str` | ID de subagente estável usado por observabilidade e controles de delegação. |
| `child_role` | `str` | Papel efetivo do filho depois que a política de delegação é aplicada, por exemplo `"leaf"` ou `"orchestrator"`. |
| `child_goal` | `str` | Objetivo/prompt delegado que o agente filho vai executar. |

**Dispara:** Em `tools/delegate_tool.py`, dentro de `_build_child_agent()`, depois que o `AIAgent` filho foi construído e anotado com metadados de identidade de subagente, e antes que `_run_single_child()` execute o filho.

**Valor de retorno:** Ignorado. Este é apenas um hook observador; retornar um valor não bloqueia nem altera a execução do agente filho.

**Casos de uso:** Registrar em log a criação de subagentes, mapear relações de sessão pai/filho, rastrear árvores de delegação aninhadas, emitir registros de auditoria pré-execução, pré-alocar recursos de observabilidade por filho.

**Exemplo — registrar em log a criação de subagente:**

```python
import logging

logger = logging.getLogger(__name__)

def log_subagent_start(
    parent_session_id,
    parent_turn_id,
    child_session_id,
    child_subagent_id,
    child_role,
    child_goal,
    **kwargs,
):
    logger.info(
        "SUBAGENT_START parent=%s turn=%s child_session=%s child=%s role=%s goal=%r",
        parent_session_id,
        parent_turn_id,
        child_session_id,
        child_subagent_id,
        child_role,
        child_goal[:200],
    )

def register(ctx):
    ctx.register_hook("subagent_start", log_subagent_start)
```

:::info
`subagent_start` é útil para observabilidade de delegação, mas não é um hook de política bloqueante. Para bloquear a delegação antes que um filho seja construído, use [`pre_tool_call`](#pre_tool_call) para bloquear a chamada da tool `delegate_task`.
:::

---

### `subagent_stop` {#subagent_stop}

Dispara **uma vez por agente filho** depois que `delegate_task` termina. Seja você tendo delegado uma única tarefa ou um lote de três, este hook dispara uma vez para cada filho, serializado na thread do pai.

**Assinatura do callback:**

```python
def my_callback(parent_session_id: str, child_role: str | None,
                child_summary: str | None, child_status: str,
                tool_call_history: list[dict], duration_ms: int, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `parent_session_id` | `str` | ID de sessão do agente pai que está delegando |
| `child_role` | `str \| None` | Tag de papel de orquestrador definida no filho (`None` se o recurso não estiver habilitado) |
| `child_summary` | `str \| None` | A resposta final que o filho retornou ao pai |
| `child_status` | `str` | `"completed"`, `"failed"`, `"interrupted"`, ou `"error"` |
| `tool_call_history` | `list[dict]` | Chamadas de tool ordenadas, apenas com metadados: `tool_name`, `tool_input` limitado, `input_bytes`, `output_bytes` e `status`; entradas e saídas brutas são excluídas |
| `duration_ms` | `int` | Tempo real gasto executando o filho, em milissegundos |

**Dispara:** Em `tools/delegate_tool.py`, depois que `ThreadPoolExecutor.as_completed()` drena todos os futures dos filhos. O disparo é encaminhado para a thread do pai, para que autores de hooks não precisem raciocinar sobre execução concorrente de callbacks.

**Valor de retorno:** Ignorado.

**Casos de uso:** Registrar em log a atividade de orquestração, acumular durações dos filhos para faturamento, escrever registros de auditoria pós-delegação.

**Exemplo — registrar em log a atividade do orquestrador:**

```python
import logging
logger = logging.getLogger(__name__)

def log_subagent(parent_session_id, child_role, child_status, duration_ms, **kwargs):
    logger.info(
        "SUBAGENT parent=%s role=%s status=%s duration_ms=%d",
        parent_session_id, child_role, child_status, duration_ms,
    )

def register(ctx):
    ctx.register_hook("subagent_stop", log_subagent)
```

:::info
Com delegação pesada (ex.: papéis de orquestrador × 5 folhas × profundidade aninhada), `subagent_stop` dispara muitas vezes por turno. Mantenha seu callback rápido; empurre trabalho caro para uma fila em segundo plano.
:::

---

### `pre_gateway_dispatch` {#pre_gateway_dispatch}

Dispara **uma vez por `MessageEvent` recebido** no gateway, depois da proteção de eventos internos mas **antes** de auth/pairing e do dispatch do agente. Este é o ponto de interceptação para políticas de fluxo de mensagens em nível de gateway (janelas de apenas escuta, transferência para humano, roteamento por chat, etc.) que não se encaixam perfeitamente em nenhum adaptador de plataforma específico.

**Assinatura do callback:**

```python
def my_callback(event, gateway, session_store, **kwargs):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `event` | `MessageEvent` | A mensagem de entrada normalizada (tem `.text`, `.source`, `.message_id`, `.internal`, etc.). |
| `gateway` | `GatewayRunner` | O executor de gateway ativo, para que plugins possam chamar `gateway.adapters[platform].send(...)` para respostas por canal lateral (notificações ao dono, etc.). |
| `session_store` | `SessionStore` | Para ingestão silenciosa de transcrição via `session_store.append_to_transcript(...)`. |

**Dispara:** Em `gateway/run.py`, dentro de `GatewayRunner._handle_message()`, imediatamente depois que `is_internal` é calculado. **Eventos internos pulam o hook por completo** (eles são gerados pelo sistema — conclusões de processos em segundo plano, etc. — e não devem ser controlados por política voltada ao usuário).

**Valor de retorno:** `None` ou um dict. O primeiro dict de ação reconhecido vence; os demais resultados de plugin são ignorados. Exceções em callbacks de plugin são capturadas e registradas em log; o gateway sempre cai no dispatch normal em caso de erro.

| Retorno | Efeito |
|--------|--------|
| `{"action": "skip", "reason": "..."}` | Descarta a mensagem — sem resposta do agente, sem fluxo de pairing, sem auth. Assume-se que o plugin já a tratou (ex.: ingerida silenciosamente na transcrição). |
| `{"action": "rewrite", "text": "new text"}` | Substitui `event.text`, então continua o dispatch normal com o evento modificado. Útil para colapsar mensagens ambiente armazenadas em buffer em um único prompt. |
| `{"action": "allow"}` / `None` | Dispatch normal — executa a cadeia completa de auth / pairing / loop do agente. |

**Casos de uso:** Grupos de chat apenas de escuta (só responder quando marcado; armazenar mensagens ambiente em buffer como contexto); transferência para humano (ingerir silenciosamente mensagens do cliente enquanto o dono lida com o chat manualmente); limitação de taxa por perfil; roteamento orientado por política.

**Exemplo — descartar DMs não autorizadas silenciosamente sem disparar o código de pairing:**

```python
def deny_unauthorized_dms(event, **kwargs):
    src = event.source
    if src.chat_type == "dm" and not _is_approved_user(src.user_id):
        return {"action": "skip", "reason": "unauthorized-dm"}
    return None

def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", deny_unauthorized_dms)
```

**Exemplo — reescrever um buffer de mensagens ambiente em um único prompt ao ser mencionado:**

```python
_buffers = {}

def buffer_or_rewrite(event, **kwargs):
    key = (event.source.platform, event.source.chat_id)
    buf = _buffers.setdefault(key, [])
    if _bot_mentioned(event.text):
        combined = "\n".join(buf + [event.text])
        buf.clear()
        return {"action": "rewrite", "text": combined}
    buf.append(event.text)
    return {"action": "skip", "reason": "ambient-buffered"}

def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", buffer_or_rewrite)
```

---

### `pre_approval_request` {#pre_approval_request}

Dispara antes que uma decisão de aprovação seja solicitada. Cobre superfícies com prompt — CLI interativa, TUI Ink, plataformas de gateway e clientes ACP — e decisões `approvals.mode=smart` tomadas sem um prompt humano (`surface="smart"`). No modo smart, o hook roda antes do LLM auxiliar ser chamado.

Este é o lugar certo para conectar um notificador customizado — por exemplo, um app de barra de menu do macOS que exibe uma notificação de permitir/negar, ou um log de auditoria que registra cada solicitação de aprovação com contexto.

**Assinatura do callback:**

```python
def my_callback(
    command: str,
    description: str,
    pattern_key: str,
    pattern_keys: list[str],
    session_key: str,
    surface: str,
    **kwargs,
):
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `command` | `str` | Comando de terminal ou script `execute_code` sendo avaliado. Payloads smart e de gateway são redigidos (redacted) antes do dispatch ao observador. A redação do observador smart é obrigatória mesmo quando `security.redact_secrets` está desabilitado; se a redação falhar, os hooks smart são pulados. |
| `description` | `str` | Motivo(s) legível(is) por humanos pelos quais o comando foi sinalizado (combinados quando múltiplos padrões correspondem) |
| `pattern_key` | `str` | Chave de padrão principal que disparou a aprovação (ex.: `"rm_rf"`, `"sudo"`) |
| `pattern_keys` | `list[str]` | Todas as chaves de padrão que corresponderam |
| `session_key` | `str` | Identificador de sessão, útil para escopar notificações por chat |
| `surface` | `str` | `"cli"` para prompts interativos de CLI/TUI, `"gateway"` para aprovações assíncronas de plataforma, ou `"smart"` para decisões automáticas de aprovar/negar do LLM auxiliar |

**Valor de retorno:** ignorado. Hooks aqui são apenas observadores; não podem vetar ou pré-responder a aprovação. Use [`pre_tool_call`](#pre_tool_call) para bloquear uma tool antes que ela chegue ao sistema de aprovação.

**Casos de uso:** Notificações no desktop, alertas push, registro de auditoria, webhooks do Slack, roteamento de escalonamento, métricas.

**Exemplo — notificação no desktop no macOS:**

```python
import subprocess

def notify_approval(command, description, session_key, **kwargs):
    title = "Hermes needs approval"
    body = f"{description}: {command[:80]}"
    subprocess.Popen([
        "osascript", "-e",
        f'display notification "{body}" with title "{title}"',
    ])

def register(ctx):
    ctx.register_hook("pre_approval_request", notify_approval)
```

---

### `post_approval_response` {#post_approval_response}

Dispara depois de uma decisão de aprovação com prompt ou smart (ou depois que um prompt expira).

**Assinatura do callback:**

```python
def my_callback(
    command: str,
    description: str,
    pattern_key: str,
    pattern_keys: list[str],
    session_key: str,
    surface: str,
    choice: str,
    **kwargs,
):
```

Mesmos kwargs de `pre_approval_request`, mais:

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `choice` | `str` | Superfícies com prompt usam `"once"`, `"session"`, `"always"`, `"deny"`, ou `"timeout"`; decisões smart usam `"smart_approve"` ou `"smart_deny"` |
| `decided_by` | `str` | `"aux_llm"` para decisões smart; ausente em superfícies com prompt |

**Valor de retorno:** ignorado.

**Casos de uso:** Fechar a notificação de desktop correspondente, registrar a decisão final em um log de auditoria, atualizar métricas, avançar um limitador de taxa.

```python
def log_decision(command, choice, session_key, **kwargs):
    logger.info("approval %s: %s for session %s", choice, command[:60], session_key)

def register(ctx):
    ctx.register_hook("post_approval_response", log_decision)
```

---

### `transform_tool_result` {#transform_tool_result}

Dispara **depois** que uma tool retorna e **antes** que o resultado seja anexado à conversa. Permite que um plugin reescreva a string de resultado de QUALQUER tool — não apenas a saída do terminal — antes que o modelo a veja.

**Assinatura do callback:**

```python
def my_callback(
    tool_name: str,
    arguments: dict,
    result: str,
    task_id: str | None,
    **kwargs,
) -> str | None:
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `tool_name` | `str` | Tool que produziu o resultado (`read_file`, `web_extract`, `delegate_task`, …). |
| `arguments` | `dict` | Argumentos com os quais o modelo chamou a tool. |
| `result` | `str` | A string de resultado bruta da tool, após truncagem e após remoção de ANSI. |
| `task_id` | `str \| None` | ID de tarefa/sessão quando rodando dentro de ambientes de RL/benchmark. |

**Valor de retorno:** `str` para substituir o resultado (a string retornada é o que o modelo vê), `None` para deixá-lo inalterado.

**Casos de uso:** Redigir PII específico da organização da saída de `web_extract`, envolver respostas longas de tool em JSON com um cabeçalho de resumo, injetar dicas de retrieval-augmented nos resultados de `read_file`, reescrever relatórios de subagente de `delegate_task` em um schema específico do projeto.

```python
import re
SECRET = re.compile(r"sk-[A-Za-z0-9]{32,}")

def redact_secrets(tool_name, result, **kwargs):
    if SECRET.search(result):
        return SECRET.sub("[REDACTED]", result)
    return None

def register(ctx):
    ctx.register_hook("transform_tool_result", redact_secrets)
```

Aplica-se a toda tool. Para reescrita apenas do terminal, veja `transform_terminal_output` abaixo — é mais restrito e roda mais cedo no pipeline (antes da truncagem, antes da redação).

---

### `transform_terminal_output` {#transform_terminal_output}

Dispara dentro do pipeline de saída em primeiro plano da tool `terminal`, **antes** da truncagem padrão de 50 KB, da remoção de ANSI e da redação de segredos. Permite que plugins reescrevam o stdout/stderr bruto de um comando de shell antes que qualquer processamento posterior o toque.

**Assinatura do callback:**

```python
def my_callback(
    command: str,
    output: str,
    exit_code: int,
    cwd: str,
    task_id: str | None,
    **kwargs,
) -> str | None:
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `command` | `str` | O comando de shell que produziu a saída. |
| `output` | `str` | stdout/stderr bruto combinado (pode ser muito grande — a truncagem acontece depois do hook). |
| `exit_code` | `int` | Código de saída do processo. |
| `cwd` | `str` | Diretório de trabalho em que o comando rodou. |

**Valor de retorno:** `str` para substituir a saída, `None` para deixá-la inalterada.

**Casos de uso:** Injetar resumos para comandos que produzem saída massiva (`du -ah`, `find`, `tree`), marcar a saída com um indicador específico do projeto para que hooks posteriores saibam como tratá-la, remover ruído de tempo que varia entre execuções e prejudica o cache de prompt.

```python
def summarize_find(command, output, **kwargs):
    if command.startswith("find ") and len(output) > 50_000:
        lines = output.count("\n")
        head = "\n".join(output.splitlines()[:40])
        return f"{head}\n\n[summary: {lines} paths total, showing first 40]"
    return None

def register(ctx):
    ctx.register_hook("transform_terminal_output", summarize_find)
```

Combina bem com `transform_tool_result` (que cobre todas as outras tools).

---

### `transform_llm_output` {#transform_llm_output}

Dispara **uma vez por turno** depois que o loop de chamadas de tool termina e o modelo produziu uma resposta final, **antes** que essa resposta seja entregue ao usuário (CLI, gateway, ou chamador programático). Permite que um plugin reescreva o texto final do assistente usando métodos de programação clássica — sem gastar tokens extras de inferência em texto de sabor SOUL ou uma transformação orientada por skill.

**Assinatura do callback:**

```python
def my_callback(
    response_text: str,
    session_id: str,
    model: str,
    platform: str,
    **kwargs,
) -> str | None:
```

| Parâmetro | Tipo | Descrição |
|-----------|------|-------------|
| `response_text` | `str` | O texto de resposta final do assistente para este turno. |
| `session_id` | `str` | ID de sessão para esta conversa (pode estar vazio em execuções one-shot). |
| `model` | `str` | Nome do modelo que produziu a resposta (ex.: `anthropic/claude-sonnet-4.6`). |
| `platform` | `str` | Plataforma de entrega (`cli`, `telegram`, `discord`, …; vazio quando não definido). |

**Valor de retorno:** `str` não vazio para substituir o texto da resposta, `None` ou string vazia para deixá-lo inalterado. **A primeira string não vazia vence** quando múltiplos plugins se registram — espelhando `transform_tool_result`.

**Casos de uso:** Aplicar uma transformação de personalidade/vocabulário (fala de pirata, Bob Esponja), redigir identificadores específicos do usuário do texto final, anexar um rodapé de assinatura específico do projeto, aplicar um guia de estilo interno sem gastar tokens em instruções SOUL.

```python
import os, re

def spongebob(response_text, **kwargs):
    if os.environ.get("SPONGEBOB_MODE") != "on":
        return None  # pass through unchanged
    return re.sub(r"!", "!! Tartar sauce!", response_text)

def register(ctx):
    ctx.register_hook("transform_llm_output", spongebob)
```

O hook é protegido para uma resposta não vazia e não interrompida — não vai disparar em interrupções pelo botão de parar ou em turnos vazios. Exceções são registradas em log como avisos e não quebram a execução do agente.

---

## Hooks de Shell {#shell-hooks}

Declare hooks de script shell no seu `~/.hermes/config.yaml` e o Hermes vai executá-los como subprocessos sempre que o evento de hook de plugin correspondente disparar — tanto em sessões de CLI quanto de gateway. Não é necessário escrever plugins Python.

Use hooks de shell quando quiser um script de arquivo único, pronto para uso (Bash, Python, qualquer coisa com shebang) para:

- **Bloquear uma chamada de tool** — rejeitar comandos `terminal` perigosos, aplicar políticas por diretório, exigir aprovação para operações destrutivas de `write_file` / `patch`.
- **Rodar depois de uma chamada de tool** — formatar automaticamente arquivos Python ou TypeScript que o agente acabou de escrever, registrar em log chamadas de API, disparar um workflow de CI.
- **Injetar contexto no próximo turno do LLM** — inserir a saída de `git status`, o dia da semana atual, ou documentos recuperados antes da mensagem do usuário (veja [`pre_llm_call`](#pre_llm_call)).
- **Observar eventos de ciclo de vida** — escrever uma linha de log quando um subagente termina (`subagent_stop`) ou uma sessão inicia (`on_session_start`).

Hooks de shell são registrados chamando `agent.shell_hooks.register_from_config(cfg)` tanto na inicialização da CLI (`hermes_cli/main.py`) quanto na inicialização do gateway (`gateway/run.py`). Eles se compõem naturalmente com hooks de plugin Python — ambos fluem pelo mesmo dispatcher.

### Comparação rápida {#comparison-at-a-glance}

| Dimensão | Hooks de shell | [Hooks de plugin](#plugin-hooks) | [Hooks de gateway](#gateway-event-hooks) |
|-----------|-------------|-------------------------------|---------------------------------------|
| Declarado em | bloco `hooks:` em `~/.hermes/config.yaml` | `register()` em um plugin `plugin.yaml` | diretório `HOOK.yaml` + `handler.py` |
| Fica em | `~/.hermes/agent-hooks/` (por convenção) | `~/.hermes/plugins/<name>/` | `~/.hermes/hooks/<name>/` |
| Linguagem | Qualquer (Bash, Python, binário Go, …) | Somente Python | Somente Python |
| Executa em | CLI + Gateway | CLI + Gateway | Somente gateway |
| Eventos | `VALID_HOOKS` (incl. `subagent_stop`) | `VALID_HOOKS` | Ciclo de vida do gateway (`gateway:startup`, `agent:*`, `command:*`) |
| Pode bloquear uma chamada de tool | Sim (`pre_tool_call`) | Sim (`pre_tool_call`) | Não |
| Pode injetar contexto no LLM | Sim (`pre_llm_call`) | Sim (`pre_llm_call`) | Não |
| Consentimento | Prompt no primeiro uso por par `(event, command)` | Implícito (confiança no plugin Python) | Implícito (confiança no diretório) |
| Isolamento entre processos | Sim (subprocesso) | Não (no mesmo processo) | Não (no mesmo processo) |

### Schema de configuração {#configuration-schema}

```yaml
hooks:
  <event_name>:                  # Must be in VALID_HOOKS
    - matcher: "<regex>"         # Optional; used for pre/post_tool_call only
      command: "<shell command>" # Required; runs via shlex.split, shell=False
      timeout: <seconds>         # Optional; default 60, capped at 300

hooks_auto_accept: false         # See "Consent model" below
```

Os nomes de evento devem ser um dos [eventos de hook de plugin](#plugin-hooks); erros de digitação geram um aviso "Did you mean X?" e são ignorados. Chaves desconhecidas dentro de uma entrada única são ignoradas; a ausência de `command` é ignorada com aviso. `timeout > 300` é limitado com um aviso.

### Protocolo de comunicação JSON {#json-wire-protocol}

Cada vez que o evento dispara, o Hermes cria um subprocesso para cada hook correspondente (respeitando o `matcher`), envia um payload JSON via **stdin**, e lê o **stdout** de volta como JSON.

**stdin — payload que o script recebe:**

```json
{
  "hook_event_name": "pre_tool_call",
  "tool_name":       "terminal",
  "tool_input":      {"command": "rm -rf /"},
  "session_id":      "sess_abc123",
  "cwd":             "/home/user/project",
  "extra":           {"task_id": "...", "tool_call_id": "..."}
}
```

`tool_name` e `tool_input` são `null` para eventos que não são de tool (`pre_llm_call`, `subagent_stop`, ciclo de vida de sessão). O dict `extra` carrega todos os kwargs específicos do evento (`user_message`, `conversation_history`, `child_role`, `duration_ms`, …). Valores não serializáveis são convertidos para string em vez de omitidos.

**stdout — resposta opcional:**

```jsonc
// Block a pre_tool_call (both shapes accepted; normalised internally):
{"decision": "block", "reason":  "Forbidden: rm -rf"}   // Claude-Code style
{"action":   "block", "message": "Forbidden: rm -rf"}   // Hermes-canonical

// Inject context for pre_llm_call:
{"context": "Today is Friday, 2026-04-17"}

// Keep the agent going at the verify gate (pre_verify); both shapes accepted:
{"action": "continue", "message": "Run the formatter, then finish."}
{"decision": "block",  "reason":  "Run the formatter, then finish."}

// Silent no-op — any empty / non-matching output is fine:
```

JSON malformado, códigos de saída diferentes de zero e timeouts registram um aviso em log, mas nunca abortam o loop do agente.

### Exemplos práticos {#worked-examples}

#### 1. Formatar automaticamente arquivos Python depois de cada escrita {#1-auto-format-python-files-after-every-write}

```yaml
# ~/.hermes/config.yaml
hooks:
  post_tool_call:
    - matcher: "write_file|patch"
      command: "~/.hermes/agent-hooks/auto-format.sh"
```

```bash
#!/usr/bin/env bash
# ~/.hermes/agent-hooks/auto-format.sh
payload="$(cat -)"
path=$(echo "$payload" | jq -r '.tool_input.path // empty')
[[ "$path" == *.py ]] && command -v black >/dev/null && black "$path" 2>/dev/null
printf '{}\n'
```

A visão do arquivo no contexto do agente **não** é relida automaticamente — a reformatação só afeta o arquivo em disco. Chamadas subsequentes de `read_file` capturam a versão formatada.

#### 2. Bloquear comandos `terminal` destrutivos {#2-block-destructive-terminal-commands}

```yaml
hooks:
  pre_tool_call:
    - matcher: "terminal"
      command: "~/.hermes/agent-hooks/block-rm-rf.sh"
      timeout: 5
```

```bash
#!/usr/bin/env bash
# ~/.hermes/agent-hooks/block-rm-rf.sh
payload="$(cat -)"
cmd=$(echo "$payload" | jq -r '.tool_input.command // empty')
if echo "$cmd" | grep -qE 'rm[[:space:]]+-rf?[[:space:]]+/'; then
  printf '{"decision": "block", "reason": "blocked: rm -rf / is not permitted"}\n'
else
  printf '{}\n'
fi
```

#### 3. Injetar `git status` em cada turno (equivalente ao `UserPromptSubmit` do Claude-Code) {#3-inject-git-status-into-every-turn-claude-code-userpromptsubmit-equivalent}

```yaml
hooks:
  pre_llm_call:
    - command: "~/.hermes/agent-hooks/inject-cwd-context.sh"
```

```bash
#!/usr/bin/env bash
# ~/.hermes/agent-hooks/inject-cwd-context.sh
cat - >/dev/null   # discard stdin payload
if status=$(git status --porcelain 2>/dev/null) && [[ -n "$status" ]]; then
  jq --null-input --arg s "$status" \
     '{context: ("Uncommitted changes in cwd:\n" + $s)}'
else
  printf '{}\n'
fi
```

O evento `UserPromptSubmit` do Claude Code intencionalmente não é um evento separado no Hermes — `pre_llm_call` dispara no mesmo lugar e já suporta injeção de contexto. Use-o aqui.

#### 4. Registrar em log cada conclusão de subagente {#4-log-every-subagent-completion}

```yaml
hooks:
  subagent_stop:
    - command: "~/.hermes/agent-hooks/log-orchestration.sh"
```

```bash
#!/usr/bin/env bash
# ~/.hermes/agent-hooks/log-orchestration.sh
log=~/.hermes/logs/orchestration.log
jq -c '{ts: now, parent: .session_id, extra: .extra}' < /dev/stdin >> "$log"
printf '{}\n'
```

### Modelo de consentimento {#consent-model}

Cada par único `(event, command)` solicita aprovação do usuário na primeira vez que o Hermes o vê, e então persiste a decisão em `~/.hermes/shell-hooks-allowlist.json`. Execuções subsequentes (CLI ou gateway) pulam o prompt.

Três válvulas de escape contornam o prompt interativo — qualquer uma delas é suficiente:

1. flag `--accept-hooks` na CLI (ex.: `hermes --accept-hooks chat`)
2. variável de ambiente `HERMES_ACCEPT_HOOKS=1`
3. `hooks_auto_accept: true` em `~/.hermes/config.yaml`

Execuções sem TTY (gateway, cron, CI) precisam de uma dessas três opções — caso contrário, qualquer hook recém-adicionado permanece silenciosamente não registrado e registra um aviso em log.

**Edições no script são confiadas silenciosamente.** A allowlist usa como chave a string exata do comando, não o hash do script, então editar o script em disco não invalida o consentimento. `hermes hooks doctor` sinaliza divergência de mtime para que você possa identificar edições e decidir se deve reaprovar.

#### Allowlist manual {#manual-allowlisting}

A allowlist manual é útil para deployments sem TTY ou de conta de serviço, onde um operador não pode responder o prompt de primeiro uso interativamente. O arquivo de allowlist é `~/.hermes/shell-hooks-allowlist.json`, e o formato esperado é um array `approvals`. Cada aprovação registra o `event` do hook e a string exata do `command`:

```json
{
  "approvals": [
    {
      "event": "post_llm_call",
      "command": "/home/hermes/.hermes/hooks/my-hook.py"
    }
  ]
}
```

A string do comando deve corresponder exatamente ao comando de hook configurado. Um objeto indexado por caminho com um campo `sha256` não é o formato esperado e não vai aprovar o hook. Verifique entradas manuais com `hermes hooks list`.

### A CLI `hermes hooks` {#the-hermes-hooks-cli}

| Comando | O que faz |
|---------|--------------|
| `hermes hooks list` | Exibe os hooks configurados com matcher, timeout e status de consentimento |
| `hermes hooks test <event> [--for-tool X] [--payload-file F]` | Dispara cada hook correspondente contra um payload sintético e imprime a resposta interpretada |
| `hermes hooks revoke <command>` | Remove toda entrada da allowlist que corresponda a `<command>` (tem efeito no próximo reinício) |
| `hermes hooks doctor` | Para cada hook configurado: verifica o bit de execução, status na allowlist, divergência de mtime, validade da saída JSON e tempo aproximado de execução |

### Segurança {#security}

Hooks de shell rodam com **suas credenciais completas de usuário** — o mesmo limite de confiança de uma entrada de cron ou um alias de shell. Trate o bloco `hooks:` em `config.yaml` como configuração privilegiada:

- Só referencie scripts que você escreveu ou revisou completamente.
- Mantenha os scripts dentro de `~/.hermes/agent-hooks/` para que o caminho seja fácil de auditar.
- Rode `hermes hooks doctor` novamente depois de puxar uma config compartilhada para identificar hooks recém-adicionados antes que sejam registrados.
- Se o seu config.yaml é versionado entre uma equipe, revise PRs que alteram a seção `hooks:` da mesma forma que revisaria uma configuração de CI.

### Ordem e precedência {#ordering-and-precedence}

Tanto hooks de plugin Python quanto hooks de shell fluem pelo mesmo dispatcher `invoke_hook()`. Plugins Python são registrados primeiro (`discover_and_load()`), hooks de shell em segundo (`register_from_config()`), então decisões de bloqueio `pre_tool_call` em Python têm precedência em casos de empate. O primeiro bloqueio válido vence — o agregador retorna assim que qualquer callback produz `{"action": "block", "message": str}` com uma mensagem não vazia.

## Webhooks de Saída {#outbound-webhooks}

Webhooks de saída são o espelho do lado push da [plataforma de webhooks de entrada](/user-guide/messaging/webhooks): webhooks de entrada acordam o Hermes quando o mundo muda; webhooks de saída avisam o mundo quando o Hermes faz algo. Configure uma lista de endpoints HTTP e os eventos de ciclo de vida com os quais eles se importam, e o Hermes faz um POST de um payload JSON assinado para cada endpoint sempre que um evento correspondente dispara — sem necessidade de polling do lado receptor.

Usos típicos:

- Notificar um sistema de CI ou dashboard quando um turno do agente termina (`on_session_end`)
- Rastrear conclusões de subagente em uma frota (`subagent_stop`)
- Alimentar monitoramento externo com atividade de tool (`post_tool_call` com um `matcher`)
- Acordar *outra* instância do Hermes: apontar a URL para o webhook de entrada dessa instância

### Configuração {#configuration}

Adicione uma lista `hooks.outbound:` ao `~/.hermes/config.yaml`:

```yaml
hooks:
  outbound:
    - name: ci-notify                       # optional label for logs
      url: https://ci.example.com/hermes-events
      events: [on_session_end, subagent_stop]
      secret_env: HERMES_OUTBOUND_WEBHOOK_SECRET   # env var holding the HMAC secret
      timeout: 10                           # per-attempt seconds (1–60)

    - name: tool-monitor
      url: https://metrics.example.com/hooks/hermes
      events: [post_tool_call]
      matcher: "terminal|delegate_task"     # regex, tool-scoped events only
```

Qualquer evento do conjunto de hooks de plugin é válido (`pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_start`, `on_session_end`, `subagent_start`, `subagent_stop`, ...). Entradas malformadas geram aviso e são puladas — um webhook quebrado nunca derruba o agente. Mudanças têm efeito na próxima sessão de CLI / reinício do gateway.

Segredos: prefira `secret_env` (o nome de uma variável de ambiente, tipicamente definida em `~/.hermes/.env`) em vez de um literal `secret:` embutido, para que o arquivo de config fique livre de credenciais. Entradas sem segredo são entregues sem assinatura (sinalizadas como `UNSIGNED` por `hermes hooks list`).

### Formato de comunicação {#wire-format}

Cada disparo faz um POST de um corpo JSON com o mesmo formato de nível superior que o stdin dos hooks de shell, mais metadados de entrega:

```json
{
  "hook_event_name": "on_session_end",
  "tool_name": null,
  "tool_input": null,
  "session_id": "sess_abc123",
  "cwd": "/home/user/project",
  "extra": {"completed": true, "interrupted": false, "model": "...", "platform": "cli"},
  "delivery_id": "3f2c9a...",
  "timestamp": "2026-07-22T14:00:00Z"
}
```

Cabeçalhos:

| Cabeçalho | Valor |
|--------|-------|
| `Content-Type` | `application/json` |
| `X-Hermes-Event` | O nome do evento de hook |
| `X-Hermes-Delivery` | ID único por entrega — mesmo valor de `delivery_id` no corpo |
| `X-Hermes-Signature-256` | `sha256=<hex>` — HMAC-SHA256 do corpo bruto, no estilo GitHub; presente apenas quando um segredo está configurado |

Verifique a assinatura exatamente como faria com um webhook do GitHub:

```python
import hashlib, hmac

def verify(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

Como `delivery_id` e `timestamp` ficam **dentro do corpo assinado**, um receptor verificado também ganha proteção contra replay de graça:

- **Dedupe** em `delivery_id` (ou o header `X-Hermes-Delivery` correspondente) — lembre-se dos ids vistos recentemente e pule duplicatas. O Hermes tenta novamente entregas com falha uma vez, então o mesmo id pode legitimamente chegar duas vezes.
- **Rejeite eventos obsoletos** verificando `timestamp` contra seu relógio com uma janela de tolerância (5 minutos é o padrão comum). Um atacante repetindo uma requisição capturada não consegue forjar um timestamp recente sem o segredo.

### Semântica de entrega {#delivery-semantics}

- **Fire-and-forget, fora do caminho crítico.** Eventos são serializados e enfileirados instantaneamente; uma única thread em segundo plano realiza os POSTs HTTP. Um endpoint lento ou morto nunca consegue travar uma chamada de tool ou um turno do agente.
- **Apenas notificação.** Ao contrário dos hooks de shell, webhooks de saída não conseguem bloquear chamadas de tool nem injetar contexto — o corpo da resposta é ignorado. Eles observam, nunca direcionam.
- **Tentativas limitadas.** Erros de conexão e respostas 5xx são tentados novamente uma vez com backoff; respostas 4xx não são tentadas novamente (o receptor disse que a própria requisição está errada). Falhas são registradas em log e descartadas — a entrega é best-effort, não garantida.
- **Redirecionamentos nunca são seguidos.** Uma resposta 3xx é tratada como uma má configuração e registrada em log — seguir um POST redirecionado descartaria silenciosamente o payload assinado. Aponte a `url` para o endpoint final.
- **Fila limitada.** Se a fila acumular (endpoint morto, tempestade de eventos), novos eventos são descartados com um aviso em vez de consumir memória ilimitada.
- **Sem prompt de consentimento.** Alvos de saída não executam código na sua máquina — eles recebem dados em uma URL que você configurou. `HERMES_SAFE_MODE=1` ainda pula o registro, assim como plugins e hooks de shell. Note que os payloads incluem entradas de tool e metadados de evento, então só aponte os alvos para endpoints em que você confia, e prefira `https://`.

`hermes hooks list` mostra os alvos de saída configurados junto com os hooks de shell, incluindo se cada alvo está assinado.
