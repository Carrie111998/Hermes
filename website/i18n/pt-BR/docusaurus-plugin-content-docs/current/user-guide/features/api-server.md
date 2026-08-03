---
sidebar_position: 14
title: "API Server"
description: "Exponha o hermes-agent como uma API compatível com OpenAI para qualquer frontend"
---

# Servidor de API

O servidor de API expõe o hermes-agent como um endpoint HTTP compatível com OpenAI. Qualquer frontend que fale o formato OpenAI — Open WebUI, LobeChat, LibreChat, NextChat, ChatBox, e centenas de outros — pode se conectar ao hermes-agent e usá-lo como backend.

Seu agente processa requisições com seu conjunto completo de ferramentas (terminal, operações de arquivo, busca na web, memória, skills) e retorna a resposta final. Ao transmitir, indicadores de progresso de ferramentas aparecem inline para que os frontends possam mostrar o que o agente está fazendo.

:::tip Um backend cobre modelos + ferramentas
O próprio Hermes precisa de um provedor configurado e de backends de ferramentas para que o servidor de API seja útil. Uma assinatura do [Nous Portal](/user-guide/features/tool-gateway) resolve ambos — mais de 300 modelos além de web/imagem/TTS/navegador através do Tool Gateway. Execute `hermes setup --portal` uma vez antes de iniciar o servidor de API e frontends como Open WebUI ou LobeChat terão um backend totalmente equipado com ferramentas.
:::

## Início Rápido {#quick-start}

### 1. Habilite o servidor de API {#1-enable-the-api-server}

Adicione ao `~/.hermes/.env`:

```bash
API_SERVER_ENABLED=true
API_SERVER_KEY=change-me-local-dev
# Optional: only if a browser must call Hermes directly
# API_SERVER_CORS_ORIGINS=http://localhost:3000
```

### 2. Inicie o gateway {#2-start-the-gateway}

```bash
hermes gateway
```

Você verá:

```
[API Server] API server listening on http://127.0.0.1:8642
```

### 3. Conecte um frontend {#3-connect-a-frontend}

Aponte qualquer cliente compatível com OpenAI para `http://localhost:8642/v1`:

```bash
# Test with curl
curl http://localhost:8642/v1/chat/completions \
  -H "Authorization: Bearer change-me-local-dev" \
  -H "Content-Type: application/json" \
  -d '{"model": "hermes-agent", "messages": [{"role": "user", "content": "Hello!"}]}'
```

Ou conecte o Open WebUI, o LobeChat, ou qualquer outro frontend — veja o [guia de integração do Open WebUI](/user-guide/messaging/open-webui) para instruções passo a passo.

## Endpoints {#endpoints}

### POST /v1/chat/completions {#post-v1chatcompletions}

Formato padrão do OpenAI Chat Completions. Sem estado (stateless) — a conversa completa é incluída em cada requisição através do array `messages`.

**Requisição:**
```json
{
  "model": "hermes-agent",
  "messages": [
    {"role": "system", "content": "You are a Python expert."},
    {"role": "user", "content": "Write a fibonacci function"}
  ],
  "stream": false
}
```

**Resposta:**
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1710000000,
  "model": "hermes-agent",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "Here's a fibonacci function..."},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 50, "completion_tokens": 200, "total_tokens": 250}
}
```

**Entrada de imagem inline:** mensagens do usuário podem enviar `content` como um array de partes `text` e `image_url`. Tanto URLs remotas `http(s)` quanto URLs `data:image/...` são suportadas:

```json
{
  "model": "hermes-agent",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "What is in this image?"},
        {"type": "image_url", "image_url": {"url": "https://example.com/cat.png", "detail": "high"}}
      ]
    }
  ]
}
```

Arquivos enviados (`file` / `input_file` / `file_id`) e URLs `data:` que não sejam de imagem retornam `400 unsupported_content_type`.

**Streaming** (`"stream": true`): Retorna Server-Sent Events (SSE) com blocos de resposta token a token. Para **Chat Completions**, o stream usa os eventos padrão `chat.completion.chunk` mais o evento personalizado do Hermes `hermes.tool.progress` para a experiência de início de ferramenta. Para **Responses**, o stream usa tipos de evento da OpenAI Responses como `response.created`, `response.output_text.delta`, `response.output_item.added`, `response.output_item.done`, e `response.completed`.

**Progresso de ferramentas em streams**:
- **Chat Completions**: o Hermes emite `event: hermes.tool.progress` para visibilidade de início de ferramenta sem poluir o texto persistido do assistente.
- **Responses**: o Hermes emite itens de saída nativos da spec `function_call` e `function_call_output` durante o stream SSE, para que os clientes possam renderizar UI estruturada de ferramentas em tempo real.

### POST /v1/responses {#post-v1responses}

Formato da OpenAI Responses API. Suporta estado de conversa do lado do servidor via `previous_response_id` — o servidor armazena o histórico completo da conversa (incluindo chamadas de ferramenta e resultados) para que o contexto de múltiplos turnos seja preservado sem que o cliente precise gerenciá-lo.

**Requisição:**
```json
{
  "model": "hermes-agent",
  "input": "What files are in my project?",
  "instructions": "You are a helpful coding assistant.",
  "store": true
}
```

**Resposta:**
```json
{
  "id": "resp_abc123",
  "object": "response",
  "status": "completed",
  "model": "hermes-agent",
  "output": [
    {"type": "function_call", "name": "terminal", "arguments": "{\"command\": \"ls\"}", "call_id": "call_1"},
    {"type": "function_call_output", "call_id": "call_1", "output": "README.md src/ tests/"},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Your project has..."}]}
  ],
  "usage": {"input_tokens": 50, "output_tokens": 200, "total_tokens": 250}
}
```

**Entrada de imagem inline:** `input[].content` pode conter partes `input_text` e `input_image`. Tanto URLs remotas quanto URLs `data:image/...` são suportadas:

```json
{
  "model": "hermes-agent",
  "input": [
    {
      "role": "user",
      "content": [
        {"type": "input_text", "text": "Describe this screenshot."},
        {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0K..."}
      ]
    }
  ]
}
```

Arquivos enviados (`input_file` / `file_id`) e URLs `data:` que não sejam de imagem retornam `400 unsupported_content_type`.

#### Múltiplos turnos com previous_response_id {#multi-turn-with-previous_response_id}

Encadeie respostas para manter o contexto completo (incluindo chamadas de ferramenta) entre turnos:

```json
{
  "input": "Now show me the README",
  "previous_response_id": "resp_abc123"
}
```

O servidor reconstrói a conversa completa a partir da cadeia de respostas armazenada — todas as chamadas de ferramenta e resultados anteriores são preservados. Requisições encadeadas também compartilham a mesma sessão, então conversas de múltiplos turnos aparecem como uma única entrada no dashboard e no histórico de sessões.

#### Conversas nomeadas {#named-conversations}

Use o parâmetro `conversation` em vez de rastrear IDs de resposta:

```json
{"input": "Hello", "conversation": "my-project"}
{"input": "What's in src/?", "conversation": "my-project"}
{"input": "Run the tests", "conversation": "my-project"}
```

O servidor encadeia automaticamente com a resposta mais recente daquela conversa. Semelhante ao comando `/title` para sessões de gateway.

### GET /v1/responses/\{id\} {#get-v1responsesid}

Recupera uma resposta previamente armazenada pelo ID.

### DELETE /v1/responses/\{id\} {#delete-v1responsesid}

Exclui uma resposta armazenada.

### GET /v1/models {#get-v1models}

Lista o agente como um modelo disponível. O nome do modelo anunciado usa por padrão o nome do [profile](/user-guide/profiles) (ou `hermes-agent` para o profile padrão). Necessário para a maioria dos frontends para descoberta de modelo.

`/v1/models` é intencionalmente a superfície econômica compatível com OpenAI. Ela **não**
enumera cada combinação autenticada de provedor/modelo para a qual o Hermes pode rotear,
e não faz enriquecimento de preços ou capacidades.

### GET /api/model/options {#get-apimodeloptions}

Clientes com reconhecimento do Hermes podem solicitar o mesmo inventário curado de provedor/modelo usado
pelo dashboard e pela TUI. Essa rota usa a autenticação bearer normal do servidor de API e
retorna linhas de provedor, dicas de capacidade de modelo, e metadados de precificação que não pertencem
à resposta compatível com OpenAI de `/v1/models`:

```bash
curl \
  -H "Authorization: Bearer $API_SERVER_KEY" \
  "http://127.0.0.1:8642/api/model/options"
```

Essa carga é o mesmo substrato que a página Models do dashboard e a RPC `model.options`
da TUI usam. Ela retorna provedores autenticados, listas de modelos curadas, precificação
por modelo, e dicas de capacidade de modelo.

Aberturas normais são intencionalmente conservadoras para provedores personalizados: o Hermes sonda
apenas o endpoint personalizado **atualmente selecionado**, para que um endpoint salvo desatualizado ou offline
não bloqueie o seletor. Uma atualização explícita muda para sondagem completa e
invalida o cache de modelo do provedor:

```bash
curl \
  -H "Authorization: Bearer $API_SERVER_KEY" \
  "http://127.0.0.1:8642/api/model/options?refresh=1"
```

Use `/v1/models` quando um cliente compatível com OpenAI só precisa de um nome de modelo para
enviar de volta em requisições de chat/responses. Use `/api/model/options` quando uma
UI autenticada precisa dos metadados mais ricos do seletor específico do Hermes.

### GET /v1/capabilities {#get-v1capabilities}

Retorna uma descrição legível por máquina da superfície estável do servidor de API para UIs externas, orquestradores, e pontes de plugins.

```json
{
  "object": "hermes.api_server.capabilities",
  "platform": "hermes-agent",
  "model": "hermes-agent",
  "auth": {"type": "bearer", "required": true},
  "features": {
    "chat_completions": true,
    "responses_api": true,
    "run_submission": true,
    "run_status": true,
    "run_events_sse": true,
    "run_stop": true
  }
}
```

Use esse endpoint ao integrar dashboards, UIs de navegador, ou planos de controle para que eles possam descobrir se a versão do Hermes em execução suporta runs, streaming, cancelamento, e continuidade de sessão sem depender de internos privados do Python.

## Seleção de modelo por requisição {#per-request-model-selection}

Clientes autenticados podem sobrepor a seleção de modelo padrão do Hermes por requisição
enviando:

- `model` — o id do modelo alvo para este turno
- `provider` — o slug do provedor Hermes para resolver credenciais/tempo de execução para este turno
- `model_options` — controles de raciocínio / camada de serviço com escopo na requisição

Os mesmos campos de requisição são aceitos em:

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/runs`
- `POST /api/sessions/{session_id}/chat`
- `POST /api/sessions/{session_id}/chat/stream`

A precedência é determinística:

1. Sobreposição de `/model` da sessão, se essa sessão já tiver uma
2. Um mapeamento estático `gateway.platforms.api_server.model_routes` selecionado quando
   o `model` da requisição é um alias de rota configurado
3. `model` / `provider` diretos da requisição quando nenhum alias de rota corresponde
4. Padrões globais de configuração do gateway / ambiente

`model_options` permanece com escopo na requisição independentemente de qual modelo/provedor prevalece.
Se uma requisição enviar um `provider` que conflite com um alias `model_routes` configurado,
o Hermes rejeita a requisição com `400` em vez de remisturar silenciosamente as credenciais da rota
com outro provedor.

**Valores `model` simples nos endpoints compatíveis com OpenAI são opt-in.** Clientes OpenAI
genéricos costumam fixar nomes de modelo (`gpt-4o`, ...), e implantações existentes
dependem que esses valores recorram ao padrão do gateway. Em
`POST /v1/chat/completions` e `POST /v1/responses`, um valor `model` enviado
SEM um `provider` é, portanto, ignorado, a menos que você habilite:

```yaml
gateway:
  platforms:
    api_server:
      direct_model_requests: true
```

Requisições que incluem um `provider` explícito — e os endpoints nativos do Hermes
`/v1/runs` e session-chat — sempre respeitam o modelo solicitado, independentemente
dessa flag.

Exemplo:

```json
{
  "model": "MiniMax-M3",
  "provider": "minimax",
  "model_options": {
    "reasoning_effort": "high",
    "service_tier": "priority"
  },
  "messages": [
    {"role": "user", "content": "Summarize the repo status."}
  ]
}
```

### GET /health {#get-health}

Verificação de integridade. Retorna `{"status": "ok"}`. Também disponível em **GET /v1/health** para clientes compatíveis com OpenAI que esperam o prefixo `/v1/`.

### GET /health/detailed {#get-healthdetailed}

Verificação de prontidão autenticada para monitoramento e planos de controle. Ela reporta
status limitado para a configuração do profile ativo, banco de dados de estado, modelo
configurado, espaço em disco, estado de gateway/plataforma, runs de API ativos, conclusões de processo
pendentes, e delegações ativas. A resposta expõe status e contagens,
não valores de configuração, credenciais, caminhos, comandos, payloads de fila, ou erros brutos.

A rota pública `/health` continua sendo uma sonda de liveness econômica e não executa verificações de
prontidão. Um resultado de prontidão degradado ainda usa HTTP 200; inspecione os
campos de nível superior `status` e `readiness.checks`.

## API de Runs (alternativa amigável a streaming) {#runs-api-streaming-friendly-alternative}

Além de `/v1/chat/completions` e `/v1/responses`, o servidor expõe uma API de **runs** para sessões de formato longo em que o cliente quer se inscrever em eventos de progresso em vez de gerenciar o streaming por conta própria.

### POST /v1/runs {#post-v1runs}

Cria um novo run de agente. Retorna um `run_id` que pode ser usado para se inscrever em eventos de progresso.

```json
{
  "run_id": "run_abc123",
  "status": "started"
}
```

Runs aceitam uma string `input` simples e, opcionalmente, `session_id`, `instructions`, `conversation_history`, ou `previous_response_id`. Quando `session_id` é fornecido, o Hermes o expõe no status do run para que UIs externas possam correlacionar runs com seus próprios IDs de conversa.

### GET /v1/runs/\{run_id\} {#get-v1runsrun_id}

Consulta o estado atual do run. Isso é útil para dashboards que precisam de status sem manter uma conexão SSE aberta, ou para UIs que se reconectam após navegação.

```json
{
  "object": "hermes.run",
  "run_id": "run_abc123",
  "status": "completed",
  "session_id": "space-session",
  "model": "hermes-agent",
  "output": "Done.",
  "usage": {"input_tokens": 50, "output_tokens": 200, "total_tokens": 250}
}
```

Os status são retidos brevemente após estados terminais (`completed`, `failed`, ou `cancelled`) para consultas e reconciliação de UI.

### GET /v1/runs/\{run_id\}/events {#get-v1runsrun_idevents}

Stream de Server-Sent Events do progresso das chamadas de ferramenta, deltas de tokens, e eventos de ciclo de vida do run. Projetado para dashboards e clientes robustos que querem se conectar/desconectar sem perder o estado.

Quando o agente delega trabalho a subagentes em segundo plano, o stream também carrega
eventos de ciclo de vida `subagent.start` e `subagent.complete`, para que os clientes possam
observar os resultados de delegação — incluindo timeouts e falhas — em vez de o
run ficar silencioso enquanto um filho trabalha. O payload de `subagent.complete` carrega
o status, o resumo, a duração, as figuras de token/custo do filho, e um
`child_session_id` para correlação; campos de texto livre passam por redação forçada de segredos
antes de sair do processo. Eventos por ferramenta do filho
(`subagent.tool`, marcações de progresso) são intencionalmente **não** encaminhados — eles
são ruído de UI de alto volume; use os arquivos de transcrição ao vivo por filho para
acompanhamento detalhado.

Buffers de eventos não consumidos expiram após cinco minutos para que um cliente desconectado não
cresça a memória indefinidamente. Isso expira apenas o estado de transporte: um run que ainda está
em execução continua visível para consulta de status, aprovação, controle de parada, e
contabilização de concorrência até que seu trabalho de executor realmente termine. Um assinante SSE
conectado continua drenando normalmente.

### POST /v1/runs/\{run_id\}/stop {#post-v1runsrun_idstop}

Interrompe um turno de agente em execução. O endpoint retorna imediatamente com `{"status": "stopping"}` enquanto o Hermes pede ao agente ativo para parar no próximo ponto seguro de interrupção.
O run permanece rastreado como `stopping` até que o trabalho apoiado pelo executor termine, então
se estabiliza como `cancelled`; solicitar a parada nunca esconde um worker que ainda está
em execução.

### POST /v1/runs/\{run_id\}/approval {#post-v1runsrun_idapproval}

Resolve uma aprovação pendente para um run que está aguardando uma decisão humana (por exemplo, uma chamada de ferramenta protegida por uma política de aprovação). O corpo carrega a decisão de aprovação; o run é retomado assim que a decisão é registrada. Este endpoint é anunciado em `/v1/capabilities` como a feature `run_approval`, para que UIs externas possam detectar suporte antes de exibir um prompt de aprovação.

## API de Jobs (trabalho agendado em segundo plano) {#jobs-api-background-scheduled-work}

O servidor expõe uma superfície CRUD leve de jobs para gerenciar runs de agente agendados / em segundo plano a partir de um cliente remoto. Todos os endpoints são protegidos pela mesma autenticação bearer.

### GET /api/jobs {#get-apijobs}

Lista todos os jobs agendados.

### POST /api/jobs {#post-apijobs}

Cria um novo job agendado. O corpo aceita o mesmo formato que `hermes cron` — prompt, agendamento, skills, sobreposição de provedor, alvo de entrega.

### GET /api/jobs/\{job_id\} {#get-apijobsjob_id}

Busca a definição de um único job e o estado da última execução.

### PATCH /api/jobs/\{job_id\} {#patch-apijobsjob_id}

Atualiza campos em um job existente (prompt, agendamento, etc.). Atualizações parciais são mescladas.

### DELETE /api/jobs/\{job_id\} {#delete-apijobsjob_id}

Remove um job. Também cancela qualquer execução em andamento.

### POST /api/jobs/\{job_id\}/pause {#post-apijobsjob_idpause}

Pausa um job sem excluí-lo. Os carimbos de tempo da próxima execução agendada são suspensos até serem retomados.

### POST /api/jobs/\{job_id\}/resume {#post-apijobsjob_idresume}

Retoma um job previamente pausado.

### POST /api/jobs/\{job_id\}/run {#post-apijobsjob_idrun}

Aciona a execução imediata do job, fora do agendamento.

## API de Sessões (controle de sessão via REST) {#sessions-api-session-control-over-rest}

UIs externas podem gerenciar sessões do Hermes via REST sem precisar montar o dashboard. Todos os endpoints são protegidos por `API_SERVER_KEY` e vivem sob `/api/sessions/*`.

| Método | Caminho | Descrição |
|--------|------|-------------|
| `GET` | `/api/sessions` | Lista sessões (paginado — `limit`, `offset`, `source`, `include_children`) |
| `POST` | `/api/sessions` | Cria uma sessão vazia |
| `GET` | `/api/sessions/{id}` | Lê os metadados da sessão |
| `PATCH` | `/api/sessions/{id}` | Atualiza o título ou `end_reason` |
| `DELETE` | `/api/sessions/{id}` | Exclui uma sessão |
| `GET` | `/api/sessions/{id}/messages` | Histórico de mensagens de uma sessão |
| `POST` | `/api/sessions/{id}/fork` | Ramifica a sessão via linhagem do `SessionDB` (corresponde à semântica do `/branch` da CLI) |
| `POST` | `/api/sessions/{id}/chat` | Executa um turno de agente síncrono |
| `POST` | `/api/sessions/{id}/chat/stream` | Wrapper SSE sobre um único turno — emite eventos `assistant.delta`, `tool.started`, `tool.completed`, `run.completed` |

`/v1/capabilities` anuncia a superfície completa via flags de feature `session_*` e entradas `endpoints.session_*`, para que UIs externas possam detectar suporte e recuar com segurança. Imagens inline são suportadas nos payloads de `chat` e `chat/stream` (caminho com reconhecimento multimodal).

```bash
# fork a session and run one turn
curl -X POST http://localhost:8642/api/sessions/$ID/fork \
  -H "Authorization: Bearer $API_SERVER_KEY" \
  -d '{"title": "explore alt path"}'

# stream a turn over SSE
curl -N -X POST http://localhost:8642/api/sessions/$ID/chat/stream \
  -H "Authorization: Bearer $API_SERVER_KEY" \
  -d '{"input": "what files changed in the last hour?"}'
```

## Descoberta de skills e toolsets {#skills-and-toolsets-discovery}

`GET /v1/skills` e `GET /v1/toolsets` permitem que clientes externos enumerem as capacidades do agente de forma determinística via REST, em vez de perguntar ao modelo. Ambos são somente leitura e protegidos por `API_SERVER_KEY`.

```bash
curl http://localhost:8642/v1/skills \
  -H "Authorization: Bearer $API_SERVER_KEY"
# → [{"name": "github-pr-workflow", "description": "...", "category": "..."}, ...]

curl http://localhost:8642/v1/toolsets \
  -H "Authorization: Bearer $API_SERVER_KEY"
# → [{"name": "core", "label": "...", "description": "...", "enabled": true,
#     "configured": true, "tools": ["read_file", "write_file", ...]}, ...]
```

`/v1/skills` retorna os mesmos metadados que o skills hub usa internamente. `/v1/toolsets` retorna os toolsets resolvidos para a plataforma `api_server` com a lista concreta de `tools` para a qual cada um se expande. Ambos são anunciados sob `endpoints.*` em `/v1/capabilities`.

## Escopo de memória de longo prazo (`X-Hermes-Session-Key`) {#long-term-memory-scoping-x-hermes-session-key}

Frontends multiusuário como o Open WebUI precisam de um identificador estável por canal para memória de longo prazo (Honcho, etc.) que seja **independente** do `X-Hermes-Session-Id` com escopo na transcrição (que gira a cada `/new`). Envie `X-Hermes-Session-Key` em `/v1/chat/completions`, `/v1/responses`, ou `/v1/runs` e o Hermes o encaminha até `AIAgent(gateway_session_key=...)`, onde o provedor de memória Honcho o usa para derivar um escopo estável.

```http
POST /v1/chat/completions HTTP/1.1
Authorization: Bearer ***
X-Hermes-Session-Id: transcript-alpha
X-Hermes-Session-Key: agent:main:webui:dm:user-42
```

Regras: máximo de 256 caracteres, caracteres de controle (`\r`, `\n`, `\x00`) são rejeitados, e o valor é ecoado de volta nas respostas (JSON + SSE). `/v1/capabilities` anuncia o suporte via `"session_key_header": "X-Hermes-Session-Key"`. Sem a chave, a estratégia `per-session` do Honcho produz um escopo diferente por `session_id` — exatamente o comportamento que o Hermes tinha antes.

## Tratamento do Prompt de Sistema {#system-prompt-handling}

Quando um frontend envia uma mensagem `system` (Chat Completions) ou o campo `instructions` (Responses API), o hermes-agent **a sobrepõe** ao seu prompt de sistema principal. Seu agente mantém todas as suas ferramentas, memória, e skills — o prompt de sistema do frontend adiciona instruções extras.

Isso significa que você pode personalizar o comportamento por frontend sem perder capacidades:
- Prompt de sistema do Open WebUI: "You are a Python expert. Always include type hints."
- O agente ainda tem terminal, ferramentas de arquivo, busca na web, memória, etc.

## Autenticação {#authentication}

Autenticação por bearer token através do cabeçalho `Authorization`:

```
Authorization: Bearer ***
```

Configure a chave via a variável de ambiente `API_SERVER_KEY`. Se você precisar que um navegador chame o Hermes diretamente, defina também `API_SERVER_CORS_ORIGINS` para uma lista de permissões explícita.

### Roteamento multi-profile (`/p/<profile>/…`) {#multi-profile-routing-pprofile}

Quando o [roteamento de gateway multi-profile](/user-guide/multi-profile-gateways) está
habilitado (`gateway.multiplex_profiles`), o listener compartilhado atende cada
profile através de um prefixo de URL `/p/<profile>/` — e **a autenticação é vinculada
ao profile roteado**:

- Requisições para `/p/<profile>/v1/...` precisam apresentar a própria
  `API_SERVER_KEY` daquele profile (de `~/.hermes/profiles/<profile>/.env`). A chave do
  listener padrão é rejeitada em prefixos de profile nomeados.
- Rotas sem prefixo e `/p/default/...` continuam usando a chave do profile padrão.
- Um profile nomeado sem sua própria `API_SERVER_KEY` falha de forma fechada — seu
  prefixo fica inacessível até que você defina uma.

:::warning Mudança que quebra compatibilidade (julho de 2026)
Antes desta correção, uma chave válida do profile padrão era aceita em qualquer
prefixo `/p/<profile>/`. Se você dependia de uma chave compartilhada entre prefixos de
profile, defina uma `API_SERVER_KEY` distinta no `.env` de cada profile — chaves
padrão reutilizadas em prefixos nomeados agora retornam `401`.
:::

:::warning Segurança
O servidor de API dá acesso total ao conjunto de ferramentas do hermes-agent, **incluindo comandos de terminal**. `API_SERVER_KEY` é **obrigatória para toda implantação**, incluindo o bind de loopback padrão em `127.0.0.1`. Mantenha `API_SERVER_CORS_ORIGINS` restrito para controlar o acesso via navegador quando você permitir explicitamente chamadores de navegador.
:::

## Configuração {#configuration}

### Variáveis de Ambiente {#environment-variables}

| Variável | Padrão | Descrição |
|----------|---------|-------------|
| `API_SERVER_ENABLED` | `false` | Habilita o servidor de API |
| `API_SERVER_PORT` | `8642` | Porta do servidor HTTP |
| `API_SERVER_HOST` | `127.0.0.1` | Endereço de bind (apenas localhost por padrão) |
| `API_SERVER_KEY` | _(obrigatório)_ | Bearer token para autenticação |
| `API_SERVER_CORS_ORIGINS` | _(nenhum)_ | Origens de navegador permitidas, separadas por vírgula |
| `API_SERVER_MODEL_NAME` | _(nome do profile)_ | Nome do modelo em `/v1/models`. Usa por padrão o nome do profile, ou `hermes-agent` para o profile padrão. |

### config.yaml {#configyaml}

As mesmas configurações podem ficar em `~/.hermes/config.yaml` sob uma seção aninhada `gateway.api_server:`:

```yaml
gateway:
  api_server:
    enabled: true
    port: 8642
    host: 127.0.0.1
    key: your-secret-key
    cors_origins: http://localhost:3000
    model_name: my-hermes
    max_concurrent_runs: 10   # concurrent-run cap; 0 disables the limit
```

`port`, `key`, `host`, `cors_origins`, e `model_name` são automaticamente conectados às configurações `extra` da plataforma, então se comportam exatamente como suas contrapartes de variável de ambiente `API_SERVER_*`. Variáveis de ambiente têm precedência sobre os valores de `config.yaml`. O bloco também é aceito sob `gateway.platforms.api_server:` ou uma seção de nível superior `platforms.api_server:`.

### Limite de runs concorrentes {#concurrent-run-cap}

O servidor de API limita quantos runs de agente podem ser executados ao mesmo tempo entre os endpoints compatíveis com OpenAI e os de Runs. O limite é lido de `gateway.api_server.max_concurrent_runs` (padrão **10**; `0` desabilita o limite, valores negativos são fixados em 0). Quando o limite é atingido, novas requisições que iniciariam um run são rejeitadas com **HTTP 429** `Too many concurrent runs (max N)` — os clientes devem recuar e tentar novamente.

## Cabeçalhos de Segurança {#security-headers}

Todas as respostas incluem cabeçalhos de segurança:
- `X-Content-Type-Options: nosniff` — evita a detecção de tipo MIME
- `Referrer-Policy: no-referrer` — evita o vazamento de referrer

## CORS {#cors}

O servidor de API **não** habilita CORS de navegador por padrão.

Para acesso direto via navegador, defina uma lista de permissões explícita:

```bash
API_SERVER_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```

Quando o CORS está habilitado:
- **Respostas de preflight** incluem `Access-Control-Max-Age: 600` (cache de 10 minutos)
- **Respostas de streaming SSE** incluem cabeçalhos CORS para que clientes EventSource de navegador funcionem corretamente
- **`Idempotency-Key`** é um cabeçalho de requisição permitido — os clientes podem enviá-lo para deduplicação (respostas são armazenadas em cache por chave por 5 minutos)

A maioria dos frontends documentados, como o Open WebUI, se conecta de servidor para servidor e não precisa de CORS.

## Frontends Compatíveis {#compatible-frontends}

Qualquer frontend que suporte o formato da API OpenAI funciona. Integrações testadas/documentadas:

| Frontend | Estrelas | Conexão |
|----------|-------|------------|
| [Open WebUI](/user-guide/messaging/open-webui) | 126k | Guia completo disponível |
| LobeChat | 73k | Endpoint de provedor personalizado |
| LibreChat | 34k | Endpoint personalizado em librechat.yaml |
| AnythingLLM | 56k | Provedor OpenAI genérico |
| NextChat | 87k | Variável de ambiente BASE_URL |
| ChatBox | 39k | Configuração de API Host |
| Jan | 26k | Configuração de modelo remoto |
| HF Chat-UI | 8k | OPENAI_BASE_URL |
| big-AGI | 7k | Endpoint personalizado |
| OpenAI Python SDK | — | `OpenAI(base_url="http://localhost:8642/v1")` |
| curl | — | Requisições HTTP diretas |

## Configuração Multiusuário com Profiles {#multi-user-setup-with-profiles}

Para dar a múltiplos usuários sua própria instância isolada do Hermes (configuração, memória, skills separadas), use [profiles](/user-guide/profiles):

```bash
# Create a profile per user
hermes profile create alice
hermes profile create bob

# Configure each profile's API server on a different port. API_SERVER_* are env
# vars (not config.yaml keys), so write them to each profile's .env:
cat >> ~/.hermes/profiles/alice/.env <<EOF
API_SERVER_ENABLED=true
API_SERVER_PORT=8643
API_SERVER_KEY=alice-secret
EOF

cat >> ~/.hermes/profiles/bob/.env <<EOF
API_SERVER_ENABLED=true
API_SERVER_PORT=8644
API_SERVER_KEY=bob-secret
EOF

# Start each profile's gateway
hermes -p alice gateway &
hermes -p bob gateway &
```

O servidor de API de cada profile anuncia automaticamente o nome do profile como o ID do modelo:

- `http://localhost:8643/v1/models` → modelo `alice`
- `http://localhost:8644/v1/models` → modelo `bob`

No Open WebUI, adicione cada um como uma conexão separada. O menu suspenso de modelo mostra `alice` e `bob` como modelos distintos, cada um apoiado por uma instância totalmente isolada do Hermes. Veja o [guia do Open WebUI](/user-guide/messaging/open-webui#multi-user-setup-with-profiles) para detalhes.

## Limitações {#limitations}

- **Armazenamento de respostas** — respostas armazenadas (para `previous_response_id`) são persistidas em SQLite e sobrevivem a reinicializações do gateway. Máximo de 100 respostas armazenadas (remoção por LRU).
- **Sem upload de arquivos** — imagens inline são suportadas tanto em `/v1/chat/completions` quanto em `/v1/responses`, mas arquivos enviados (`file`, `input_file`, `file_id`) e entradas de documento que não sejam imagem não são suportados através da API.
- **Clientes OpenAI simples ainda veem um alias** — `/v1/models` anuncia o
  alias estável do Hermes (`hermes-agent` ou o nome do profile ativo). Clientes
  mais avançados podem enviar sobreposições explícitas de `provider` / `model_options` nas requisições.

## Modo Proxy {#proxy-mode}

O servidor de API também serve como o backend para o **modo proxy de gateway**. Quando outra instância de gateway do Hermes é configurada com `GATEWAY_PROXY_URL` apontando para este servidor de API, ela encaminha todas as mensagens para cá em vez de executar seu próprio agente. Isso permite implantações divididas — por exemplo, um contêiner Docker lidando com E2EE do Matrix que retransmite para um agente do lado do host.

Veja o [Modo Proxy do Matrix](/user-guide/messaging/matrix#proxy-mode-e2ee-on-macos) para o guia de configuração completo.
