---
sidebar_position: 2
title: "Configuração"
description: "Configure o Hermes Agent — config.yaml, provedores, modelos, chaves de API e mais"
---

# Configuração

Todas as configurações são armazenadas no diretório `~/.hermes/` para fácil acesso.

:::tip Caminho mais fácil para um `config.yaml` funcional
Execute `hermes setup --portal` — um único OAuth te dá um provedor de modelo e todas as quatro ferramentas do Tool Gateway sem editar YAML manualmente. Assinantes do Portal também recebem 10% de desconto em provedores cobrados por token. Veja [Nous Portal](/integrations/nous-portal).
:::

## Estrutura de Diretórios {#directory-structure}

```text
~/.hermes/
├── config.yaml     # Settings (model, terminal, TTS, compression, etc.)
├── .env            # API keys and secrets
├── auth.json       # OAuth provider credentials (Nous Portal, etc.)
├── SOUL.md         # Primary agent identity (slot #1 in system prompt)
├── memories/       # Persistent memory (MEMORY.md, USER.md)
├── skills/         # Agent-created skills (managed via skill_manage tool)
├── cron/           # Scheduled jobs
├── sessions/       # Gateway sessions
└── logs/           # Logs (errors.log, gateway.log — secrets auto-redacted)
```

## Gerenciando a Configuração {#managing-configuration}

```bash
hermes config              # View current configuration
hermes config edit         # Open config.yaml in your editor
hermes config get KEY      # Print a resolved value
hermes config set KEY VAL  # Set a specific value
hermes config unset KEY    # Remove a user-set value
hermes config check        # Check for missing options (after updates)
hermes config migrate      # Interactively add missing options

# Examples:
hermes config get model
hermes config set model anthropic/claude-opus-4
hermes config set terminal.backend docker
hermes config unset terminal.backend
hermes config set OPENROUTER_API_KEY sk-or-...  # Saves to .env
```

:::tip
O comando `hermes config set` roteia automaticamente os valores para o arquivo correto — chaves de API são salvas em `.env`, tudo o mais em `config.yaml`.
:::

## Precedência de Configuração {#configuration-precedence}

As configurações são resolvidas nesta ordem (prioridade mais alta primeiro):

1. **Argumentos de linha de comando** — ex.: `hermes chat --model anthropic/claude-sonnet-4` (sobrescrita por invocação)
2. **`~/.hermes/config.yaml`** — o arquivo de configuração principal para todas as configurações não secretas
3. **`~/.hermes/.env`** — alternativa para variáveis de ambiente; **obrigatório** para segredos (chaves de API, tokens, senhas)
4. **Padrões embutidos** — padrões seguros pré-configurados quando nada mais está definido

:::info Regra Geral
Segredos (chaves de API, tokens de bot, senhas) vão em `.env`. Tudo o mais (modelo, backend do terminal, configurações de compressão, limites de memória, conjuntos de ferramentas) vai em `config.yaml`. Quando ambos estão definidos, `config.yaml` prevalece para configurações não secretas.
:::

:::tip Implantações organizacionais
Um administrador pode fixar valores específicos de configuração e segredos que um
usuário padrão não pode sobrescrever, através de um diretório gerenciado em nível de sistema. Veja
[Managed Scope](/user-guide/managed-scope).
:::

## Substituição de Variáveis de Ambiente {#environment-variable-substitution}

Você pode referenciar variáveis de ambiente no `config.yaml` usando a sintaxe `${VAR_NAME}`:

```yaml
auxiliary:
  vision:
    api_key: ${GOOGLE_API_KEY}
    base_url: ${CUSTOM_VISION_URL}

delegation:
  api_key: ${DELEGATION_KEY}
```

Múltiplas referências em um único valor funcionam: `url: "${HOST}:${PORT}"`. Se uma variável referenciada não estiver definida, o placeholder é mantido literalmente (`${UNDEFINED_VAR}` permanece como está) e um aviso é registrado no log. `$VAR` sem chaves não é expandido.

A sintaxe SecretRef no estilo Cursor também é aceita: `${env:VAR_NAME}` resolve exatamente como `${VAR_NAME}` (o prefixo `env:` é removido), então trechos de MCP ou de provedores copiados de configurações do Cursor/Claude funcionam sem alterações tanto em `config.yaml` quanto no bloco `mcp_servers`. Outras fontes SecretRef (`${file:...}`, `${vault:...}`, `${bitwarden:...}`) **não** são resolvidas inline — backends de segredos externos injetam seus valores no ambiente na inicialização via o bloco `secrets:`, então referencie-os como `${env:NAME}` em vez disso; prefixos desconhecidos avisam uma vez e permanecem literais.

Para configuração de provedores de IA (OpenRouter, Anthropic, Copilot, endpoints personalizados, LLMs auto-hospedados, modelos de fallback, etc.), veja [AI Providers](/integrations/providers).

### Timeouts de Provedor {#provider-timeouts}

Você pode definir `providers.<id>.request_timeout_seconds` para um timeout de requisição em nível de provedor, além de `providers.<id>.models.<model>.timeout_seconds` para uma sobrescrita específica de modelo. Aplica-se ao cliente principal do turno em todo transporte (OpenAI-wire, Anthropic nativo, compatível com Anthropic), à cadeia de fallback, a reconstruções após rotação de credencial e (para OpenAI-wire) ao kwarg de timeout por requisição — então o valor configurado prevalece sobre a variável de ambiente legada `HERMES_API_TIMEOUT`.

Você também pode definir `providers.<id>.stale_timeout_seconds` para o detector de chamada obsoleta não-streaming, além de `providers.<id>.models.<model>.stale_timeout_seconds` para uma sobrescrita específica de modelo. Isso prevalece sobre a variável de ambiente legada `HERMES_API_CALL_STALE_TIMEOUT`.

Deixar isso sem definir mantém os padrões legados (`HERMES_API_TIMEOUT=1800`s, `HERMES_API_CALL_STALE_TIMEOUT=90`s, Anthropic nativo 900s). O detector de chamada obsoleta não-streaming é desativado automaticamente para endpoints locais quando deixado implícito e pode escalar para cima em contextos muito grandes. Ainda não está conectado para AWS Bedrock (tanto o caminho `bedrock_converse` quanto o SDK AnthropicBedrock usam boto3 com sua própria configuração de timeout). Veja o exemplo comentado em [`cli-config.yaml.example`](https://github.com/NousResearch/hermes-agent/blob/main/cli-config.yaml.example).

## Comportamento de Atualização {#update-behavior}

As configurações do `hermes update` ficam sob `updates` em `config.yaml`:

```yaml
updates:
  pre_update_backup: quick       # quick (state snapshot, default) | full (snapshot + HERMES_HOME zip) | off
  backup_keep: 5                 # Keep this many full pre-update backup zips
  non_interactive_local_changes: stash  # stash | discard
```

`pre_update_backup` é o único ajuste de segurança de pré-atualização: `quick` (padrão) tira um snapshot dos arquivos de estado críticos (dados de pareamento, jobs de cron, config, auth; arquivos acima de 1 GiB são ignorados) em `state-snapshots/`; `full` adicionalmente compacta todo o `HERMES_HOME` em `backups/` e pode adicionar minutos em instalações grandes; `off` desativa ambos. Booleanos legados são respeitados (`true` → `full`, `false` → `off`).

Para instalações via git, o Hermes automaticamente guarda em stash arquivos rastreados e não rastreados com alterações antes de fazer checkout do branch de atualização ou dar pull. Atualizações interativas no terminal perguntam antes de restaurar esse stash. Atualizações não interativas (app de desktop/chat, gateway, ou `--yes`) usam `updates.non_interactive_local_changes`: `stash` restaura edições locais de código-fonte após um pull bem-sucedido, enquanto `discard` descarta o stash criado pela atualização após um pull bem-sucedido. Use `discard` apenas em instalações gerenciadas onde edições locais de código-fonte nunca devem persistir.

Antes dessa etapa de stash, o Hermes também restaura diferenças rastreadas de `package-lock.json` deixadas por churn de instalação/build do npm. Faça commit ou guarde manualmente em stash edições intencionais do lockfile antes de atualizar.

## Configuração do Backend de Terminal {#terminal-backend-configuration}

O Hermes suporta sete backends de terminal. Cada um determina onde os comandos de shell do agente realmente são executados — sua máquina local, um contêiner Docker, um servidor remoto via SSH, um sandbox de nuvem Modal (direto ou via o gateway gerenciado pela Nous), um workspace Daytona, um Vercel Sandbox, ou um contêiner Singularity/Apptainer.

```yaml
terminal:
  backend: local    # local | docker | ssh | modal | daytona | vercel_sandbox | singularity
  cwd: "."          # Gateway/cron working directory (CLI always uses launch dir)
  font_family: ""   # Desktop terminal font; e.g. "MesloLGS NF"
  timeout: 180      # Per-command timeout in seconds
  home_mode: auto   # auto | real | profile — subprocess HOME policy
  env_passthrough: []  # Env var names to forward to sandboxed execution (terminal + execute_code)
  singularity_image: "docker://nikolaik/python-nodejs:python3.11-nodejs20"  # Container image for Singularity backend
  modal_image: "nikolaik/python-nodejs:python3.11-nodejs20"                 # Container image for Modal backend
  daytona_image: "nikolaik/python-nodejs:python3.11-nodejs20"               # Container image for Daytona backend
```

`terminal.font_family` controla o terminal embutido no Hermes Desktop. Aceita tanto o nome de uma família instalada localmente (por exemplo, `MesloLGS NF`) quanto uma pilha de fontes CSS. O Hermes anexa sua pilha JetBrains Mono empacotada como fallback, e um valor vazio mantém o padrão. Você pode editar a mesma configuração com escopo de perfil em **Settings → Appearance → Terminal Font**; nenhum download do Google Fonts ou permissão de fonte do sistema é necessário.

Para sandboxes de nuvem como Modal, Daytona e Vercel Sandbox, `container_persistent: true` significa que o Hermes tentará preservar o estado do sistema de arquivos entre recriações do sandbox. Isso não garante que o mesmo sandbox ativo, espaço de PID ou processos em segundo plano ainda estarão em execução mais tarde.

### Visão Geral dos Backends {#backend-overview}

| Backend | Onde os comandos rodam | Isolamento | Melhor para |
|---------|-------------------|-----------|----------|
| **local** | Sua máquina diretamente | Nenhum | Desenvolvimento, uso pessoal |
| **docker** | Contêiner Docker único e persistente (compartilhado entre sessão, `/new`, subagentes) | Total (namespaces, cap-drop) | Sandboxing seguro, CI/CD |
| **ssh** | Servidor remoto via SSH | Fronteira de rede | Desenvolvimento remoto, hardware potente |
| **modal** | Sandbox de nuvem Modal | Total (VM na nuvem) | Computação em nuvem efêmera, avaliações |
| **daytona** | Workspace Daytona | Total (contêiner na nuvem) | Ambientes de desenvolvimento em nuvem gerenciados |
| **vercel_sandbox** | Vercel Sandbox | Total (microVM na nuvem) | Execução em nuvem com persistência de sistema de arquivos via snapshot |
| **singularity** | Contêiner Singularity/Apptainer | Namespaces (--containall) | Clusters HPC, máquinas compartilhadas |

### Backend Local {#local-backend}

O padrão. Comandos rodam diretamente na sua máquina sem isolamento. Nenhuma configuração especial necessária.

```yaml
terminal:
  backend: local
```

Por padrão, os subprocessos de ferramentas locais mantêm o `HOME` real do seu usuário do
SO. Isso permite que CLIs externas como `git`, `ssh`, `gh`, `az`, `npm`, Claude Code e Codex
encontrem as credenciais e configurações que já usam no seu shell normal. O estado do Hermes
continua com escopo de perfil através de `HERMES_HOME`; `HOME` não é como os perfis
selecionam config, memória, sessões ou skills.

O Hermes **não** altera seu `HOME` global do sistema, seus arquivos de inicialização de shell, ou
a conta home do sistema operacional. Essa configuração controla apenas o ambiente
passado para subprocessos que o Hermes inicia através de ferramentas como `terminal`,
processos de terminal em segundo plano, `execute_code` e processos auxiliares do ACP.

#### `terminal.home_mode` {#terminalhome_mode}

| Modo | Instalações no host | Contêineres | Trade-off |
|---|---|---|---|
| `auto` | Mantém o `HOME` real do usuário do SO | Usa `{HERMES_HOME}/home` | Padrão recomendado. CLIs do host continuam funcionando; o estado do contêiner persiste. |
| `real` | Força o `HOME` real do usuário do SO | Força o `HOME` real do usuário do SO se visível | Útil se um processo pai acidentalmente iniciou com `HOME` apontando para um home de perfil. |
| `profile` | Usa `{HERMES_HOME}/home` quando existe | Usa `{HERMES_HOME}/home` quando existe | Isolamento estrito de configuração de CLI por perfil, mas `~/.ssh`, `~/.gitconfig`, `~/.azure`, `~/.config/gh`, autenticação Claude/Codex, estado do npm etc. normais não estarão visíveis a menos que você os inicialize ou vincule dentro do home do perfil. |

A desvantagem do padrão é que os perfis do host compartilham as mesmas
credenciais/configurações normais de CLI em nível de usuário sob `~`. Se você precisar de um perfil com
identidade git separada, chaves SSH, login na GitHub CLI, configuração npm ou login em CLI de
nuvem, use `home_mode: profile` e inicialize essas ferramentas dentro daquele home
de perfil deliberadamente.

Se você deliberadamente quiser isolamento estrito de configuração de ferramentas por perfil, defina:

```yaml
terminal:
  home_mode: profile
```

Nesse modo, os subprocessos de ferramentas usam `{HERMES_HOME}/home` como `HOME`. O Hermes também
define `HERMES_REAL_HOME` para que scripts ainda possam localizar o home real do usuário quando
precisarem. Backends de contêiner continuam usando `{HERMES_HOME}/home` no modo `auto`
porque esse diretório vive no volume de dados persistente do Hermes.

Scripts que precisam distinguir o estado do perfil do home real do usuário devem
preferir `HERMES_HOME` para dados do Hermes e `HERMES_REAL_HOME` para o home da conta:

```python
from pathlib import Path
import os

hermes_home = Path(os.environ["HERMES_HOME"])
real_home = Path(os.environ.get("HERMES_REAL_HOME", os.environ["HOME"]))
```

:::warning
O agente tem o mesmo acesso ao sistema de arquivos que sua conta de usuário. Use `hermes tools` para desativar ferramentas que você não quer, ou mude para Docker para sandboxing.
:::

### Backend Docker {#docker-backend}

Executa comandos dentro de um contêiner Docker com reforço de segurança (todas as capabilities removidas, sem escalonamento de privilégios, limites de PID).

**Contêiner único e persistente, compartilhado entre processos do Hermes.** O Hermes inicia UM contêiner de longa duração na primeira utilização e roteia cada chamada de terminal, arquivo e `execute_code` através de `docker exec` para esse mesmo contêiner — entre sessões, `/new`, `/reset` e subagentes de `delegate_task`. Mudanças de diretório de trabalho, pacotes instalados, arquivos em `/workspace` e **processos em segundo plano** persistem de uma chamada de ferramenta para a próxima, e de um processo Hermes para o próximo. Quando você fecha uma sessão TUI, executa `/quit`, ou inicia uma nova invocação do `hermes`, o contêiner continua em execução e o próximo processo Hermes o reutiliza via uma busca rotulada. Veja **Ciclo de vida do contêiner** abaixo para as regras exatas de desligamento.

```yaml
terminal:
  backend: docker
  docker_image: "nikolaik/python-nodejs:python3.11-nodejs20"
  docker_mount_cwd_to_workspace: false  # Mount launch dir into /workspace
  docker_run_as_host_user: false   # See "Running container as host user" below
  docker_forward_env:              # Host env vars to forward into container
    - "GITHUB_TOKEN"
  docker_env:                      # Literal env vars to inject (KEY=value)
    DEBUG: "1"
    PYTHONUNBUFFERED: "1"
  docker_volumes:                  # Host directory mounts
    - "/home/user/projects:/workspace/projects"
    - "/home/user/data:/data:ro"   # :ro for read-only
  docker_extra_args:               # Extra flags appended verbatim to `docker run`
    - "--gpus=all"
    - "--network=host"
  docker_network: true             # false = air-gap the container (--network=none)

  # Resource limits
  container_cpu: 1                 # CPU cores (0 = unlimited)
  container_memory: 5120           # MB (0 = unlimited)
  container_disk: 51200            # MB (requires overlay2 on XFS+pquota)
  container_persistent: true       # Persist /workspace and /root bind-mount dirs

  # Cross-process container reuse (defaults match the "one long-lived
  # container shared across sessions" contract — see Container lifecycle).
  docker_persist_across_processes: true   # Reuse container across Hermes restarts
  docker_orphan_reaper: true              # Sweep abandoned Exited containers at startup

  # Cross-backend lifecycle settings (apply to docker as well)
  timeout: 180                     # Per-command timeout in seconds
  lifetime_seconds: 300            # Idle-reaper window; also feeds 2× orphan-reaper threshold
```

**`docker_env`** vs **`docker_forward_env`**: o primeiro injeta pares `KEY=value` literais que você especifica na configuração (os valores ficam no seu `config.yaml` ou são passados como um dict JSON via `TERMINAL_DOCKER_ENV='{"DEBUG":"1"}'`). O segundo encaminha valores do seu shell ou `~/.hermes/.env`, de modo que o segredo real nunca aparece no arquivo de configuração. Use `docker_forward_env` para tokens e `docker_env` para ajustes estáticos que o contêiner precisa.

**`terminal.docker_extra_args`** (também sobrescrevível via `TERMINAL_DOCKER_EXTRA_ARGS='["--gpus=all"]'`) permite passar flags arbitrárias do `docker run` que o Hermes não expõe como chaves de primeira classe — `--gpus`, `--network`, `--add-host`, sobrescritas alternativas de `--security-opt`, etc. Cada entrada deve ser uma string; a lista é anexada por último na invocação montada do `docker run`, então pode sobrescrever os padrões do Hermes se necessário. Use com moderação — flags que conflitam com o reforço de segurança do sandbox (remoção de capabilities, `--user`, o bind mount do workspace) enfraquecerão silenciosamente o isolamento.

**`terminal.docker_network`** (padrão `true`; env: `TERMINAL_DOCKER_NETWORK`) — defina como `false` para rodar o contêiner sandbox com `--network=none`, cortando toda saída de rede dos comandos do agente. Isso se aplica ao contêiner de execução usado por `terminal`, `execute_code` e as ferramentas de arquivo. Como os contêineres persistem entre processos do Hermes, mudar isso para `false` enquanto um contêiner mais antigo com rede existe removerá esse contêiner e iniciará um novo isolado (air-gapped) (um aviso é registrado); processos em segundo plano rodando nele são perdidos. Prefira essa chave a passar `--network=none` via `docker_extra_args`.

**Requisitos:** Docker Desktop ou Docker Engine instalado e em execução. O Hermes verifica o `$PATH` mais locais comuns de instalação no macOS (`/usr/local/bin/docker`, `/opt/homebrew/bin/docker`, o pacote de app Docker Desktop). Podman é suportado nativamente: defina `HERMES_DOCKER_BINARY=podman` (ou o caminho completo) para forçá-lo quando ambos estiverem instalados.

#### Ciclo de vida do contêiner {#container-lifecycle}

Cada contêiner gerenciado pelo Hermes é marcado com três labels para que processos subsequentes (e o coletor de órfãos) possam identificá-lo:

- `hermes-agent=1` — marca como gerenciado pelo Hermes
- `hermes-task-id=<sanitized task_id>` — chave para a sondagem de reutilização por tarefa
- `hermes-profile=<sanitized profile name>` — restringe reutilização e coleta ao perfil Hermes ativo

Na inicialização, o Hermes executa `docker ps --filter label=hermes-task-id=<id> --filter label=hermes-profile=<profile>` e **conecta-se ao contêiner existente** quando encontra um. Se o contêiner estiver `exited` (por exemplo, após um reinício do daemon Docker), ele é reiniciado com `docker start` e reutilizado — o estado do sistema de arquivos e quaisquer pacotes instalados sobrevivem, mas os processos em segundo plano dentro do contêiner não.

Quando um processo Hermes é encerrado — `/quit`, fechamento de uma sessão TUI, desligamento do gateway, até mesmo SIGKILL — o caminho de limpeza é um **no-op para o contêiner no modo padrão**. O contêiner continua rodando. O próximo processo Hermes se conecta a ele em milissegundos via a sondagem de label. Esse é o comportamento exigido pelo contrato de "um contêiner único de longa duração compartilhado entre sessões": é a única forma de processos em segundo plano (watchers do npm, servidores de desenvolvimento, pytest de longa duração) sobreviverem entre sessões.

**O contêiner só é desmontado (parado e removido com `docker rm -f`) nestes casos:**

| Gatilho | Quando dispara |
|---|---|
| `docker_persist_across_processes: false` | Isolamento explícito por processo. Todo `cleanup()` faz `stop` + `rm -f`. Corresponde ao comportamento pré-issue-#20561. |
| Coletor de ociosos (`lifetime_seconds`, padrão 300s) | Só quando o ambiente é `persist_across_processes=false`. Ambientes em modo persistente não fazem nada; o contêiner sobrevive à varredura de ociosidade. |
| Coletor de órfãos na próxima inicialização | Varre contêineres rotulados pelo Hermes em estado **Exited** mais antigos que `2 × lifetime_seconds` (padrão 600s = 10 min), restrito ao perfil atual. **Contêineres em execução nunca são tocados** — segurança entre processos irmãos. Defina `docker_orphan_reaper: false` para desativar. |
| Ação direta do usuário | `docker rm -f`, `docker system prune`, reinício do Docker Desktop. Não definimos `--restart=always`, então um reboot do host deixa o contêiner `Exited` (sua camada CoW sobrevive e é reutilizada na próxima inicialização, mas os processos em segundo plano se foram). |

Casos extremos que vale conhecer:

- **OOM kill do PID 1 dentro do contêiner** transiciona o contêiner para `Exited`. A próxima reutilização fará `docker start`; o estado do sistema de arquivos sobrevive, os processos em segundo plano não.
- **Trocar de perfil** isola contêineres uns dos outros — um contêiner rotulado `hermes-profile=work` fica invisível para um processo Hermes rodando sob `hermes-profile=research`. O coletor de órfãos também é restrito por perfil, então contêineres entre perfis não são coletados acidentalmente, mas também não serão limpos automaticamente até você iniciar o Hermes novamente sob o perfil original deles.

Subagentes paralelos gerados via `delegate_task(tasks=[...])` compartilham esse mesmo contêiner — `cd` concorrente, mutações de ambiente e escritas no mesmo caminho vão colidir. Se um subagente precisar de um sandbox isolado, ele deve registrar uma sobrescrita de imagem por tarefa via `register_task_env_overrides()`, o que os ambientes de RL e benchmark (TerminalBench2, HermesSweEnv, etc.) fazem automaticamente para suas imagens Docker por tarefa.

**Reforço de segurança:**
- `--cap-drop ALL` com apenas `DAC_OVERRIDE`, `CHOWN`, `FOWNER` adicionados de volta
- `--security-opt no-new-privileges`
- `--pids-limit 256`
- tmpfs com tamanho limitado para `/tmp` (512MB), `/var/tmp` (256MB), `/run` (64MB)

**Encaminhamento de credenciais:** Variáveis de ambiente listadas em `docker_forward_env` são resolvidas primeiro a partir do ambiente do seu shell, depois `~/.hermes/.env`. Skills também podem declarar `required_environment_variables`, que são mescladas automaticamente.

#### Sobrescritas por variável de ambiente {#environment-variable-overrides}

Cada chave sob `terminal:` tem uma sobrescrita por variável de ambiente no formato `TERMINAL_<KEY_UPPERCASE>`. As mais úteis para o backend Docker:

| Variável de ambiente | Corresponde a | Observações |
|---|---|---|
| `TERMINAL_DOCKER_IMAGE` | `docker_image` | Imagem base |
| `TERMINAL_DOCKER_FORWARD_ENV` | `docker_forward_env` | Array JSON: `'["GITHUB_TOKEN","OPENAI_API_KEY"]'` |
| `TERMINAL_DOCKER_ENV` | `docker_env` | Dict JSON: `'{"DEBUG":"1"}'` |
| `TERMINAL_DOCKER_VOLUMES` | `docker_volumes` | Array JSON de strings `"host:container[:ro]"` |
| `TERMINAL_DOCKER_EXTRA_ARGS` | `docker_extra_args` | Array JSON |
| `TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE` | `docker_mount_cwd_to_workspace` | `true` / `false` |
| `TERMINAL_DOCKER_RUN_AS_HOST_USER` | `docker_run_as_host_user` | `true` / `false` |
| `TERMINAL_DOCKER_NETWORK` | `docker_network` | `true` / `false` — padrão `true`; `false` = `--network=none` |
| `TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES` | `docker_persist_across_processes` | `true` / `false` — padrão `true` |
| `TERMINAL_DOCKER_ORPHAN_REAPER` | `docker_orphan_reaper` | `true` / `false` — padrão `true` |
| `TERMINAL_CONTAINER_CPU` | `container_cpu` | Núcleos de CPU |
| `TERMINAL_CONTAINER_MEMORY` | `container_memory` | MB |
| `TERMINAL_CONTAINER_DISK` | `container_disk` | MB |
| `TERMINAL_CONTAINER_PERSISTENT` | `container_persistent` | `true` / `false` — controla os diretórios de workspace com bind-mount, distinto de `docker_persist_across_processes` |
| `TERMINAL_LIFETIME_SECONDS` | `lifetime_seconds` | Janela do coletor de ociosos |
| `TERMINAL_TIMEOUT` | `timeout` | Timeout por comando |
| `HERMES_DOCKER_BINARY` | _nenhuma_ | Força um binário docker/podman específico |

### Backend SSH {#ssh-backend}

Executa comandos em um servidor remoto via SSH. Usa ControlMaster para reutilização de conexão (keepalive ocioso de 5 minutos). O shell persistente está habilitado por padrão — o estado (cwd, variáveis de ambiente) sobrevive entre comandos.

```yaml
terminal:
  backend: ssh
  persistent_shell: true           # Keep a long-lived bash session (default: true)
```

**Variáveis de ambiente obrigatórias:**

```bash
TERMINAL_SSH_HOST=my-server.example.com
TERMINAL_SSH_USER=ubuntu
```

**Opcional:**

| Variável | Padrão | Descrição |
|----------|---------|-------------|
| `TERMINAL_SSH_PORT` | `22` | Porta SSH |
| `TERMINAL_SSH_KEY` | (padrão do sistema) | Caminho para a chave privada SSH |
| `TERMINAL_SSH_PERSISTENT` | `true` | Habilita shell persistente |

**Como funciona:** Conecta-se no momento da inicialização com `BatchMode=yes` e `StrictHostKeyChecking=accept-new`. O shell persistente mantém um único processo `bash -l` ativo no host remoto, comunicando-se via arquivos temporários. Comandos que precisam de `stdin_data` ou `sudo` automaticamente recorrem ao modo one-shot.

### Backend Modal {#modal-backend}

Executa comandos em um sandbox de nuvem [Modal](https://modal.com). Cada tarefa recebe uma VM isolada com CPU, memória e disco configuráveis. O sistema de arquivos pode ter snapshot/restauração entre sessões.

```yaml
terminal:
  backend: modal
  container_cpu: 1                 # CPU cores
  container_memory: 5120           # MB (5GB)
  container_disk: 51200            # MB (50GB)
  container_persistent: true       # Snapshot/restore filesystem
```

**Obrigatório:** As variáveis de ambiente `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET`, ou um arquivo de configuração `~/.modal.toml`.

**Persistência:** Quando habilitado, o sistema de arquivos do sandbox recebe um snapshot na limpeza e é restaurado na próxima sessão. Os snapshots são rastreados em `~/.hermes/modal_snapshots.json`. Isso preserva o estado do sistema de arquivos, não processos ativos, espaço de PID ou jobs em segundo plano.

**Arquivos de credenciais:** Montados automaticamente a partir de `~/.hermes/` (tokens OAuth, etc.) e sincronizados antes de cada comando.

### Backend Daytona {#daytona-backend}

Executa comandos em um workspace gerenciado [Daytona](https://daytona.io). Suporta stop/resume para persistência.

```yaml
terminal:
  backend: daytona
  container_cpu: 1                 # CPU cores
  container_memory: 5120           # MB → converted to GiB
  container_disk: 10240            # MB → converted to GiB (max 10 GiB)
  container_persistent: true       # Stop/resume instead of delete
```

**Obrigatório:** variável de ambiente `DAYTONA_API_KEY`.

**Persistência:** Quando habilitado, os sandboxes são parados (não excluídos) na limpeza e retomados na próxima sessão. Os nomes dos sandboxes seguem o padrão `hermes-{task_id}`.

**Limite de disco:** o Daytona impõe um máximo de 10 GiB. Solicitações acima disso são limitadas com um aviso.

### Backend Vercel Sandbox {#vercel-sandbox-backend}

Executa comandos em uma microVM de nuvem [Vercel Sandbox](https://vercel.com/docs/vercel-sandbox). O Hermes usa as superfícies normais de ferramentas de terminal e arquivo; não há ferramentas voltadas ao modelo específicas da Vercel.

```yaml
terminal:
  backend: vercel_sandbox
  vercel_runtime: node24          # node24 | node22 | python3.13
  cwd: /vercel/sandbox            # default workspace root
  container_persistent: true      # Snapshot/restore filesystem
  container_disk: 51200           # Shared default only; custom disk is unsupported
```

**Instalação obrigatória:** Instale o extra opcional do SDK:

```bash
pip install 'hermes-agent[vercel]'
```

**Autenticação obrigatória:** Configure a autenticação por token de acesso com todos os três: `VERCEL_TOKEN`, `VERCEL_PROJECT_ID` e `VERCEL_TEAM_ID`. Essa é a configuração suportada para implantações e processos Hermes normais de longa duração em Render, Railway, Docker e hosts similares.

Para desenvolvimento local pontual, o Hermes também aceita tokens Vercel OIDC de curta duração:

```bash
VERCEL_OIDC_TOKEN="$(vc project token <project-name>)" hermes chat
```

A partir de um diretório de projeto Vercel vinculado, você pode omitir o nome do projeto:

```bash
VERCEL_OIDC_TOKEN="$(vc project token)" hermes chat
```

Tokens OIDC são de curta duração e não devem ser usados como o caminho de implantação documentado.

**Runtime:** `terminal.vercel_runtime` suporta `node24`, `node22` e `python3.13`. Se não definido, o Hermes usa `node24` por padrão.

**Persistência:** Quando `container_persistent: true`, o Hermes tira um snapshot do sistema de arquivos do sandbox durante a limpeza e restaura um sandbox posterior para a mesma tarefa a partir desse snapshot. O conteúdo do snapshot pode incluir credenciais sincronizadas pelo Hermes, skills e arquivos de cache que foram copiados para o sandbox. Isso preserva apenas o estado do sistema de arquivos; não preserva a identidade viva do sandbox, o espaço de PID, o estado do shell ou processos em segundo plano em execução.

**Comandos em segundo plano:** `terminal(background=true)` usa o fluxo genérico não local de processos em segundo plano do Hermes. Você pode gerar, sondar, esperar, ver logs e matar processos através da ferramenta de processo normal enquanto o sandbox está ativo. O Hermes não fornece recuperação nativa de processos desanexados da Vercel após limpeza ou reinício.

**Dimensionamento de disco:** o Vercel Sandbox atualmente não suporta o ajuste de recursos `container_disk` do Hermes. Deixe `container_disk` não definido ou no padrão compartilhado `51200`; valores diferentes do padrão falham nos diagnósticos e na criação do backend em vez de serem silenciosamente ignorados.

### Backend Singularity/Apptainer {#singularityapptainer-backend}

Executa comandos em um contêiner [Singularity/Apptainer](https://apptainer.org). Projetado para clusters HPC e máquinas compartilhadas onde o Docker não está disponível.

```yaml
terminal:
  backend: singularity
  singularity_image: "docker://nikolaik/python-nodejs:python3.11-nodejs20"
  container_cpu: 1                 # CPU cores
  container_memory: 5120           # MB
  container_persistent: true       # Writable overlay persists across sessions
```

**Requisitos:** binário `apptainer` ou `singularity` no `$PATH`.

**Tratamento de imagem:** URLs Docker (`docker://...`) são convertidas automaticamente em arquivos SIF e armazenadas em cache. Arquivos `.sif` existentes são usados diretamente.

**Diretório de scratch:** resolvido nesta ordem: `TERMINAL_SCRATCH_DIR` → `TERMINAL_SANDBOX_DIR/singularity` → `/scratch/$USER/hermes-agent` (convenção HPC) → `~/.hermes/sandboxes/singularity`.

**Isolamento:** Usa `--containall --no-home` para isolamento total de namespace sem montar o diretório home do host.

### Problemas Comuns do Backend de Terminal {#common-terminal-backend-issues}

Se os comandos de terminal falharem imediatamente ou a ferramenta de terminal for reportada como desativada:

- **Local** — Sem requisitos especiais. O padrão mais seguro para começar.
- **Docker** — Execute `docker version` para verificar se o Docker está funcionando. Se falhar, corrija o Docker ou execute `hermes config set terminal.backend local`.
- **SSH** — Tanto `TERMINAL_SSH_HOST` quanto `TERMINAL_SSH_USER` devem estar definidos. O Hermes registra um erro claro se algum estiver faltando.
- **Modal** — Precisa da variável de ambiente `MODAL_TOKEN_ID` ou `~/.modal.toml`. Execute `hermes doctor` para verificar.
- **Daytona** — Precisa de `DAYTONA_API_KEY`. O SDK do Daytona cuida da configuração da URL do servidor.
- **Singularity** — Precisa de `apptainer` ou `singularity` no `$PATH`. Comum em clusters HPC.

Em caso de dúvida, defina `terminal.backend` de volta para `local` e verifique se os comandos funcionam lá primeiro.

### Sincronização de Estado Remoto-para-Host no Desligamento {#remote-to-host-state-sync-on-teardown}

Para os backends **SSH**, **Modal** e **Daytona**, o Hermes envia seu estado de `~/.hermes/` (arquivos de credenciais, skills, cache) para o sandbox remoto durante a sessão e, no desligamento, **sincroniza de volta os arquivos de estado alterados** para seus locais originais no host. Arquivos que diferem do que foi originalmente enviado (comparados por hash de conteúdo) são aplicados de volta no lugar; novos arquivos remotos sob um diretório sincronizado (por exemplo, uma skill que o agente criou remotamente) são mapeados de volta para o caminho correspondente no host. Arquivos de credenciais apenas de upload nunca são sobrescritos no host.

- A sincronização de volta tenta novamente até 3 vezes com backoff e se recusa a extrair arquivos remotos maiores que 2 GiB.
- Docker e Singularity usam bind mounts (visão do sistema de arquivos do host ao vivo) e não precisam disso.
- Isso cobre o estado do Hermes (`~/.hermes/`), **não** arquivos arbitrários da árvore de trabalho dentro do sandbox — faça o agente copiar artefatos importantes explicitamente (por exemplo, `scp`, `modal volume put`) antes que o sandbox seja destruído.

### Montagens de Volume Docker {#docker-volume-mounts}

Ao usar o backend Docker, `docker_volumes` permite compartilhar diretórios do host com o contêiner. Cada entrada usa a sintaxe padrão `-v` do Docker: `host_path:container_path[:options]`.

```yaml
terminal:
  backend: docker
  docker_volumes:
    - "/home/user/projects:/workspace/projects"   # Read-write (default)
    - "/home/user/datasets:/data:ro"              # Read-only
    - "/home/user/.hermes/cache/documents:/output" # Gateway-visible exports
```

Isso é útil para:
- **Fornecer arquivos** ao agente (datasets, configurações, código de referência)
- **Receber arquivos** do agente (código gerado, relatórios, exportações)
- **Workspaces compartilhados** onde tanto você quanto o agente acessam os mesmos arquivos

Se você usa um gateway de mensagens e quer que o agente envie arquivos gerados via
`MEDIA:/...`, prefira uma montagem de exportação dedicada visível pelo host, como
`/home/user/.hermes/cache/documents:/output`.

- Escreva arquivos dentro do Docker em `/output/...`
- Emita o **caminho do host** em `MEDIA:`, por exemplo:
  `MEDIA:/home/user/.hermes/cache/documents/report.txt`
- Não emita `/workspace/...` ou `/output/...` a menos que esse caminho exato também
  exista para o processo do gateway no host

:::warning
Chaves YAML duplicadas silenciosamente sobrescrevem as anteriores. Se você já tem um
bloco `docker_volumes:`, mescle novas montagens na mesma lista em vez de adicionar
outra chave `docker_volumes:` mais adiante no arquivo.
:::

Também pode ser definido via variável de ambiente: `TERMINAL_DOCKER_VOLUMES='["/host:/container"]'` (array JSON).

### Encaminhamento de Credenciais Docker {#docker-credential-forwarding}

Por padrão, sessões de terminal Docker não herdam credenciais arbitrárias do host. Se você precisar de um token específico dentro do contêiner, adicione-o a `terminal.docker_forward_env`.

```yaml
terminal:
  backend: docker
  docker_forward_env:
    - "GITHUB_TOKEN"
    - "NPM_TOKEN"
```

O Hermes resolve cada variável listada primeiro a partir do seu shell atual, depois recorre a `~/.hermes/.env` se ela foi salva com `hermes config set`.

:::warning
Qualquer coisa listada em `docker_forward_env` fica visível para comandos executados dentro do contêiner. Encaminhe apenas credenciais que você está confortável em expor à sessão de terminal.
:::

### Executando o Contêiner como Seu Usuário do Host {#running-the-container-as-your-host-user}

Por padrão, os contêineres Docker rodam como `root` (UID 0). Arquivos criados dentro de `/workspace` ou outros bind-mounts acabam pertencendo ao root no host, então depois de uma sessão você precisa fazer `sudo chown` neles antes de poder editá-los a partir do editor do seu host. A flag `terminal.docker_run_as_host_user` corrige isso:

```yaml
terminal:
  backend: docker
  docker_run_as_host_user: true   # default: false
```

Quando habilitado, o Hermes anexa `--user $(id -u):$(id -g)` ao comando `docker run`, de modo que os arquivos escritos em diretórios bind-mounted (`/workspace`, `/root`, qualquer coisa em `docker_volumes`) pertençam ao seu usuário do host, não ao root. A contrapartida: o contêiner não pode mais fazer `apt install` ou escrever em caminhos pertencentes ao root, como `/root/.npm` — use uma imagem base cujo `HOME` pertença a um usuário não root (ou adicione as ferramentas necessárias no momento da construção da imagem) se precisar de ambos.

Deixe isso como `false` (o padrão) para um comportamento retrocompatível. Ative quando seu fluxo de trabalho for principalmente "editar arquivos do host montados" e você estiver cansado de `sudo chown -R`.

### Opcional: Montar o Diretório de Lançamento em `/workspace` {#optional-mount-the-launch-directory-into-workspace}

Sandboxes Docker permanecem isolados por padrão. O Hermes **não** passa seu diretório de trabalho atual do host para dentro do contêiner a menos que você opte explicitamente por isso.

Habilite em `config.yaml`:

```yaml
terminal:
  backend: docker
  docker_mount_cwd_to_workspace: true
```

Quando habilitado:
- se você iniciar o Hermes a partir de `~/projects/my-app`, esse diretório do host é montado com bind mount em `/workspace`
- o backend Docker inicia em `/workspace`
- as ferramentas de arquivo e comandos de terminal veem o mesmo projeto montado

Quando desabilitado, `/workspace` permanece de propriedade do sandbox, a menos que você monte algo explicitamente via `docker_volumes`.

Trade-off de segurança:
- `false` preserva a fronteira do sandbox
- `true` dá ao sandbox acesso direto ao diretório de onde você iniciou o Hermes

Use a opção opt-in apenas quando você intencionalmente quiser que o contêiner trabalhe em arquivos ativos do host.

### Shell Persistente {#persistent-shell}

Por padrão, cada comando de terminal roda em seu próprio subprocesso — diretório de trabalho, variáveis de ambiente e variáveis de shell são reiniciados entre comandos. Quando o **shell persistente** está habilitado, um único processo bash de longa duração é mantido ativo entre chamadas de `execute()`, de modo que o estado sobrevive entre comandos.

Isso é mais útil para o **backend SSH**, onde também elimina a sobrecarga de conexão por comando. O shell persistente é **habilitado por padrão para SSH** e desabilitado para o backend local.

```yaml
terminal:
  persistent_shell: true   # default — enables persistent shell for SSH
```

Para desativar:

```bash
hermes config set terminal.persistent_shell false
```

**O que persiste entre comandos:**
- Diretório de trabalho (`cd /tmp` permanece para o próximo comando)
- Variáveis de ambiente exportadas (`export FOO=bar`)
- Variáveis de shell (`MY_VAR=hello`)

**Precedência:**

| Nível | Variável | Padrão |
|-------|----------|---------|
| Config | `terminal.persistent_shell` | `true` |
| Sobrescrita SSH | `TERMINAL_SSH_PERSISTENT` | segue a config |
| Sobrescrita local | `TERMINAL_LOCAL_PERSISTENT` | `false` |

Variáveis de ambiente por backend têm a maior precedência. Se você também quiser shell persistente no backend local:

```bash
export TERMINAL_LOCAL_PERSISTENT=true
```

:::note
Comandos que exigem `stdin_data` ou sudo automaticamente recorrem ao modo one-shot, já que o stdin do shell persistente já está ocupado pelo protocolo IPC.
:::

Veja [Code Execution](features/code-execution.md) e a [seção de Terminal do README](features/tools.md) para detalhes sobre cada backend.

## Configurações de Skill {#skill-settings}

Skills podem declarar suas próprias configurações através do frontmatter do SKILL.md. Esses são valores não secretos (caminhos, preferências, configurações de domínio) armazenados sob o namespace `skills.config` em `config.yaml`.

```yaml
skills:
  config:
    myplugin:
      path: ~/myplugin-data   # Example — each skill defines its own keys
```

**Como funcionam as configurações de skill:**

- `hermes config migrate` varre todas as skills habilitadas, encontra configurações não definidas e oferece perguntar a você
- `hermes config show` exibe todas as configurações de skill sob "Skill Settings" junto com a skill a que pertencem
- Quando uma skill é carregada, seus valores de configuração resolvidos são injetados automaticamente no contexto da skill

**Definindo valores manualmente:**

```bash
hermes config set skills.config.myplugin.path ~/myplugin-data
```

Para detalhes sobre como declarar configurações nas suas próprias skills, veja [Creating Skills — Config Settings](/developer-guide/creating-skills#config-settings-configyaml).

### Proteção em escritas de skill criadas pelo agente {#guard-on-agent-created-skill-writes}

Quando o agente usa `skill_manage` para criar, editar, aplicar patch ou excluir uma skill, o Hermes pode opcionalmente varrer o conteúdo novo/atualizado em busca de padrões de palavras-chave perigosos (coleta de credenciais, injeção de prompt óbvia, instruções de exfiltração). O scanner está **desativado por padrão** — fluxos de trabalho reais do agente que legitimamente tocam `~/.ssh/` ou mencionam `$OPENAI_API_KEY` estavam disparando a heurística com muita frequência. Reative-o se quiser que o scanner peça sua confirmação antes que as escritas de skill do agente sejam efetivadas:

```yaml
skills:
  guard_agent_created: true   # default: false
```

Quando ativado, qualquer escrita de `skill_manage` sinalizada aparece como um prompt de aprovação com a justificativa do scanner. Escritas aceitas são efetivadas; escritas negadas retornam um erro explicativo ao agente.

### Aprovação de escrita para escritas de skill {#write-approval-for-skill-writes}

Independentemente do scanner de conteúdo acima, `skills.write_approval` restringe **toda** escrita de skill do agente (criar/editar/aplicar patch/excluir/arquivos de suporte) atrás da sua aprovação explícita — o mesmo mecanismo de aprovar/negar usado para comandos perigosos:

```yaml
skills:
  write_approval: false   # false = write freely (default) | true = stage every write for review
```

Quando ativado, as escritas de skill são preparadas em `~/.hermes/pending/skills/` e revisadas com `/skills pending`, `/skills diff <id>`, `/skills approve <id>`, `/skills reject <id>` — a partir da CLI ou de qualquer plataforma de mensagens. Alterne em tempo de execução com `/skills approval on|off`. A memória tem a mesma restrição (`memory.write_approval`, abaixo). Passo a passo completo: [Gating agent skill writes](/user-guide/features/skills#gating-agent-skill-writes-skillswrite_approval).

## Configuração de Memória {#memory-configuration}

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200   # ~800 tokens
  user_char_limit: 1375     # ~500 tokens
  write_approval: false     # true = require approval before any memory write
```

Com `memory.write_approval: true`, as escritas de memória precisam da sua aprovação antes de serem efetivadas: turnos interativos da CLI perguntam inline; sessões de mensagens e a revisão de autoaperfeiçoamento em segundo plano preparam a escrita para revisão via `/memory pending` → `/memory approve <id>` / `/memory reject <id>`. Alterne em tempo de execução com `/memory approval on|off`. Veja [Controlling memory writes](/user-guide/features/memory#controlling-memory-writes-write_approval).

## Truncamento de Arquivo de Contexto {#context-file-truncation}

Controla quanto conteúdo o Hermes carrega de cada arquivo de contexto automático antes de aplicar truncamento de início/fim. Isso se aplica a arquivos injetados no prompt de sistema, como `SOUL.md`, `.hermes.md`, `AGENTS.md`, `CLAUDE.md` e `.cursorrules`. Isso **não** afeta a ferramenta `read_file`.

```yaml
context_file_max_chars: null  # default — dynamic cap scaled to the model's context window (floor 20K, ceiling 500K chars)
```

Defina um inteiro positivo para fixar um limite fixo em vez do comportamento dinâmico:

```yaml
context_file_max_chars: 25000
```

## Segurança de Leitura de Arquivo {#file-read-safety}

Controla quanto conteúdo uma única chamada de `read_file` pode retornar. Leituras que excedem o limite são rejeitadas com um erro dizendo ao agente para usar `offset` e `limit` para um intervalo menor. Isso evita que uma única leitura de um bundle JS minificado ou arquivo de dados grande inunde a janela de contexto.

```yaml
file_read_max_chars: 100000  # default — ~25-35K tokens
```

Aumente se você estiver usando um modelo com uma janela de contexto grande e ler arquivos grandes com frequência. Diminua para modelos de contexto pequeno para manter as leituras eficientes:

```yaml
# Large context model (200K+)
file_read_max_chars: 200000

# Small local model (16K context)
file_read_max_chars: 30000
```

O agente também deduplica leituras de arquivo automaticamente — se a mesma região de arquivo for lida duas vezes e o arquivo não tiver mudado, um stub leve é retornado em vez de reenviar o conteúdo. Isso reinicia na compressão de contexto para que o agente possa reler arquivos depois que o conteúdo deles for resumido.

## Limites de Truncamento de Saída de Ferramenta {#tool-output-truncation-limits}

Três limites relacionados controlam quanta saída bruta uma ferramenta pode retornar antes que o Hermes a trunque:

```yaml
tool_output:
  max_bytes: 50000        # terminal output cap (chars)
  max_lines: 2000         # read_file pagination cap
  max_line_length: 2000   # per-line cap in read_file's line-numbered view
```

- **`max_bytes`** — Quando um comando de `terminal` produz mais do que esse número de caracteres de stdout/stderr combinados, o Hermes mantém os primeiros 40% e os últimos 60% e insere um aviso `[OUTPUT TRUNCATED]` entre eles. Padrão `50000` (≈12-15K tokens em tokenizadores típicos).
- **`max_lines`** — Limite superior do parâmetro `limit` de uma única chamada de `read_file`. Solicitações acima disso são limitadas para que uma única leitura não possa inundar a janela de contexto. Padrão `2000`.
- **`max_line_length`** — Limite por linha aplicado quando `read_file` emite a visão numerada por linhas. Linhas mais longas que isso são truncadas para essa quantidade de caracteres seguida por `... [truncated]`. Padrão `2000`.

Aumente os limites em modelos com janelas de contexto grandes que podem se dar ao luxo de mais saída bruta por chamada. Diminua-os para modelos de contexto pequeno para manter os resultados de ferramentas compactos:

```yaml
# Large context model (200K+)
tool_output:
  max_bytes: 150000
  max_lines: 5000

# Small local model (16K context)
tool_output:
  max_bytes: 20000
  max_lines: 500
```

## Desativação Global de Conjunto de Ferramentas {#global-toolset-disable}

Para suprimir conjuntos de ferramentas específicos em toda a CLI e cada plataforma do gateway em um único
lugar, liste seus nomes sob `agent.disabled_toolsets`:

```yaml
agent:
  disabled_toolsets:
    - memory       # hide memory tools + MEMORY_GUIDANCE injection
    - web          # no web_search / web_extract anywhere
```

Isso se aplica **depois** da configuração de ferramentas por plataforma (`platform_toolsets` escrita pelo
`hermes tools`), então um conjunto de ferramentas listado aqui é sempre removido — mesmo que a
configuração salva de uma plataforma ainda o liste. Use isso quando você quiser um único
interruptor para "desligar X em todo lugar" em vez de editar mais de 15 linhas de plataforma
na UI do `hermes tools`.

Deixar a lista vazia, ou omitir a chave, não tem efeito.

## Isolamento de Git Worktree {#git-worktree-isolation}

Habilite worktrees git isolados para executar múltiplos agentes em paralelo no mesmo repositório:

```yaml
worktree: true    # Always create a worktree (same as hermes -w)
# worktree: false # Default — only when -w flag is passed
```

Quando habilitado, cada sessão CLI cria um worktree novo sob `.worktrees/` com seu próprio branch. Agentes podem editar arquivos, fazer commit, push e criar PRs sem interferir uns nos outros. Worktrees limpos são removidos na saída; os sujos são mantidos para recuperação manual.

Por padrão, o novo worktree ramifica a partir da **ponta remota recém-buscada** (o upstream do branch atual, ou o branch padrão do remoto) para que comece atualizado com o projeto em vez de a partir do `HEAD` possivelmente desatualizado do clone local. Isso mantém o diff de um PR restrito à mudança real, em vez de herdar o quanto o clone local estava atrasado. Defina `worktree_sync: false` para ramificar a partir do `HEAD` local em vez disso — útil offline, ou quando você deliberadamente quer o estado atual exato do clone como base. Se o remoto não puder ser alcançado, ele recorre automaticamente ao `HEAD` local.

```yaml
worktree_sync: true    # Default — branch from the fetched remote tip
# worktree_sync: false # Branch from local HEAD (offline / pinned base)
```

Você também pode listar arquivos ignorados pelo git para copiar para os worktrees via `.worktreeinclude` na raiz do seu repositório:

```
# .worktreeinclude
.env
.venv/
node_modules/
```

## Compressão de Contexto {#context-compression}

O Hermes comprime automaticamente conversas longas para permanecer dentro da janela de contexto do seu modelo. O sumarizador de compressão é uma chamada de LLM separada — você pode apontá-lo para qualquer provedor ou endpoint.

Todas as configurações de compressão ficam em `config.yaml` (nenhuma variável de ambiente).

### Referência completa {#full-reference}

```yaml
compression:
  enabled: true                                     # Toggle compression on/off
  progress_notices: false                           # Opt-in: deliver routine compression progress notices to chat platforms — see below
  threshold: 0.50                                   # Compress at this % of context limit
  threshold_tokens: null                            # Absolute token cap (optional) — takes lower of ratio vs absolute
  target_ratio: 0.20                                # Fraction of threshold to preserve as recent tail
  protect_last_n: 20                                # Min recent messages to keep uncompressed
  protect_first_n: 3                                # Non-system head messages pinned across compactions (0 = pin nothing)
  in_place: true                                    # Compact on the same session id (no rotation) — see below
  idle_compact_after_seconds: 0                     # Opt-in idle compaction (0 = disabled) — see below
  hygiene_hard_message_limit: 5000                  # Gateway safety valve — see below
  hygiene_timeout_seconds: 30                       # Max seconds of NO summary-model output before hygiene compression is cut off
  hygiene_total_ceiling_seconds: 600                # Absolute cap on the hygiene wait even while tokens are still streaming
  hygiene_failure_cooldown_seconds: 300             # Skip repeated failed hygiene attempts for this session
  context_timeout_seconds: 120                      # Inactivity budget for in-agent compress_context (loop /compress / preflight) — see below
  context_total_ceiling_seconds: 600                # Absolute cap on the *pre-commit* in-agent compress_context wait even while tokens are still streaming (an already-started SessionDB commit is never abandoned; overruns are logged + surfaced)
  proactive_prune_tokens: 0                         # Opt-in tokens trigger for the no-LLM tool-result prune (0 = off; see below)
  proactive_prune_min_result_chars: 8000            # Prune's summarize pass only touches tool results larger than this (clamped >= 200)
  proactive_prune_min_reclaim_tokens: 4096          # Prune only commits when it reclaims at least this many tokens (0 = commit any)

# The summarization model/provider is configured under auxiliary:
auxiliary:
  compression:
    model: ""                                       # Empty = use main chat model. Override with e.g. "google/gemini-3-flash-preview" for cheaper/faster compression.
    provider: "auto"                                # Provider: "auto", "openrouter", "nous", "codex", "main", etc.
    base_url: null                                  # Custom OpenAI-compatible endpoint (overrides provider)
```

:::info Migração de configuração legada
Configurações mais antigas com `compression.summary_model`, `compression.summary_provider` e `compression.summary_base_url` são migradas automaticamente para `auxiliary.compression.*` no primeiro carregamento (versão de config 17). Nenhuma ação manual necessária.
:::

`progress_notices` (padrão `false`) controla se os status **rotineiros** de progresso de compressão chegam às plataformas de chat (Telegram, Discord, Slack, etc.). Por design, a compressão automática é silenciosa nas superfícies de chat — ela roda em segundo plano apenas com registro no lado do servidor. Defina `progress_notices: true` para optar por ver o ciclo de vida rotineiro nas plataformas de chat: o aviso de início "Compactando contexto…", gatilhos de compressão preflight/pré-API, compactação por ociosidade, progresso de nova tentativa ("Comprimidas 30 → 12 mensagens, tentando novamente…") e o aviso "Compactação de contexto concluída". A restrição se aplica apenas aos status de compressão — ruídos operacionais não relacionados (falhas de modelo auxiliar, ruído de retry/limite de taxa de provedor) permanecem suprimidos de qualquer forma. Avisos de **falha** de compressão e o feedback manual de `/compress` são sempre visíveis independentemente dessa configuração. Editar esse valor em um gateway em execução tem efeito na próxima mensagem.

`hygiene_hard_message_limit` é uma **válvula de segurança de pré-compressão** apenas do gateway. Ela existe para quebrar uma espiral de morte: quando chamadas de API continuam desconectando em uma sessão superdimensionada, o gateway nunca recebe dados de uso de token, então o limite baseado em token não pode disparar, então a transcrição continua crescendo e as desconexões pioram. Esse piso baseado em contagem dispara apenas pela contagem de mensagens (sempre conhecida, independentemente de falhas de API) para forçar a compressão e recuperar a sessão. Padrão `5000` — muito acima de qualquer sessão normal, incluindo modelos de contexto grande (1M+) fazendo milhares de turnos curtos, que comprimem no limite de token muito antes disso. Aumente ainda mais para plataformas incomuns, diminua para forçar compressão mais agressiva. Editar esse valor em um gateway em execução tem efeito na próxima mensagem (veja abaixo).

`hygiene_timeout_seconds` é o **orçamento de inatividade** do gateway para essa passada de compressão pré-agente — não um limite total de tempo de relógio. A chamada de resumo de compressão faz streaming a partir do modelo, e cada token recebido conta como progresso: um modelo de raciocínio lento que ainda está gerando continua estendendo seu próprio prazo, então modelos de resumo lentos mas saudáveis nunca são interrompidos no meio da geração. Somente quando o modelo de resumo não produz **nenhuma saída** por esse número de segundos (backend fora do ar, conexão travada, provedor silencioso) o gateway avisa o usuário, continua a mensagem recebida sem compressão e registra um cooldown temporário de falha por sessão em vez de parecer travado.

`hygiene_total_ceiling_seconds` (padrão `600`) limita a espera total mesmo enquanto os tokens ainda estão em movimento, para que um fluxo degenerado em gotas não possa manter um turno refém indefinidamente. É limitado a pelo menos `hygiene_timeout_seconds`.

`hygiene_failure_cooldown_seconds` controla esse cooldown por sessão após um timeout ou aborto de compressão de higiene. Durante o cooldown, o gateway pula tentativas repetidas de higiene para a mesma sessão superdimensionada, para que toda mensagem recebida não fique bloqueada no mesmo backend auxiliar quebrado. `/compress`, `/reset`, ou um turno saudável posterior ainda pode recuperar a sessão.

`context_timeout_seconds` (padrão `120`) é o mesmo **orçamento de inatividade** para o `compress_context` interno ao agente — o loop de conversa, a compactação preflight e o `/compress` manual — para que um modelo de resumo travado não possa paralisar uma sessão indefinidamente. Tokens de resumo em streaming estendem a espera; apenas um worker silencioso é interrompido. No timeout, o Hermes pula a compactação, mantém as mensagens existentes e avisa o usuário. Defina como `0` para desativar. A higiene de sessão do gateway mantém seu próprio caminho `hygiene_timeout_seconds` e não é envolvida duas vezes.

`context_total_ceiling_seconds` (padrão `600`) limita a espera **pré-commit** interna ao agente (fase de resumo/streaming) mesmo enquanto os tokens ainda estão em movimento. É limitado a pelo menos `context_timeout_seconds`. A garantia exata: **a fase de resumo é limitada por esse teto; a fase de commit é registrada e exposta se ultrapassá-lo.** Uma vez que o worker entra na cerca de commit de compressão e a mutação do SessionDB está em andamento, o commit nunca é abandonado no meio do caminho — isso arriscaria divergência de transcrição — mas a espera não é mais silenciosa: se o commit ultrapassar o teto, o Hermes registra o excesso (WARNING, escalando para ERROR em repetição), envia um aviso único através do canal de aviso visível ao usuário e continua esperando em incrementos limitados até que o commit seja concluído.

`protect_first_n` controla quantas mensagens de início **não-sistema** são fixadas em toda compactação. Padrão `3` — a troca inicial usuário/assistente sobrevive a cada passada do sumarizador, para que o objetivo original permaneça visível. Em sessões de compactação contínua de longa duração onde o turno de abertura não é mais relevante, defina `protect_first_n: 0` para não fixar nada além do prompt de sistema + resumo + cauda. O prompt de sistema em si é sempre preservado independentemente dessa configuração.

`in_place` (padrão `true`) controla o que acontece com a identidade da sessão quando a compactação dispara. Quando `true`, a compactação reescreve a lista de mensagens e reconstrói o prompt de sistema **sem rotacionar o id da sessão** — a conversa mantém um id durável por toda sua vida (sem cadeia de `parent_session_id`, sem renumeração `nome #2` / `#3` em listas de sessão). A compactação é não destrutiva: o contexto ativo é compactado, mas os turnos pré-compactação são arquivados suavemente sob o mesmo id (marcados como inativos/compactados) — ainda pesquisáveis via `session_search` e recuperáveis, não excluídos. Hooks veem o modo através do campo `in_place` no evento `session:compress`. Defina `in_place: false` para restaurar o comportamento legado onde cada compactação rotaciona para um novo id de sessão vinculado ao antigo.

`threshold_tokens` define um **limite absoluto de tokens** opcional para o gatilho de compressão. Quando definido, a compressão dispara no menor entre o `threshold` baseado em razão e essa contagem absoluta — de modo que a compressão nunca dispara mais tarde do que o número de tokens preferido pelo usuário, independentemente de qual modelo esteja ativo. Isso resolve o problema em que trocar entre modelos com janelas de contexto diferentes (por exemplo, 1M → 400K) desloca o ponto de gatilho absoluto. O limite é ajustado ao comprimento de contexto do modelo, então defini-lo mais alto do que o modelo suporta é seguro — o limite baseado em razão é usado em vez disso. Padrão `null` (desativado — apenas limite baseado em razão). O limite sobrevive a trocas de modelo e ativações de fallback.

`idle_compact_after_seconds` é um gatilho **opt-in, baseado em tempo** que complementa o `threshold` baseado em tamanho. Padrão `0` (desativado). Quando definido acima de 0, uma sessão que retoma após pelo menos essa quantidade de segundos de inatividade compacta seu histórico acumulado antecipadamente, antes da primeira resposta — de modo que uma thread de longa duração (por exemplo, uma conversa no Telegram à qual você volta horas depois) não releia todo o seu contexto obsoleto completo em cada turno subsequente. Nunca dispara quando o contexto já está em ou abaixo do alvo pós-compressão (`threshold × target_ratio`), e respeita as mesmas proteções de cooldown de falha, anti-thrash e bloqueio por sessão que qualquer compactação automática. Exemplo: `idle_compact_after_seconds: 1800` compacta após 30 minutos de inatividade.

`proactive_prune_tokens` habilita uma poda determinística, sem LLM, de payloads antigos de resultado de ferramenta que roda independentemente do `threshold`. Em modelos de janela grande, a compactação por `threshold` (≈50% da janela) raramente dispara, então saídas de ferramenta volumosas (dumps de terminal, leituras de arquivo, extrações web) ficam no histórico e são reenviadas em cada turno subsequente. Quando o histórico reenviado excede `proactive_prune_tokens` (padrão `0` = desligado; tente `48000` para habilitar), a poda deduplica resultados idênticos, resume os mais antigos e superdimensionados, e trunca argumentos grandes de chamada de ferramenta — protegendo as `protect_last_n` mensagens mais recentes e nunca chamando o modelo. Saídas completas permanecem recuperáveis no armazenamento de sessão. `proactive_prune_min_result_chars` (padrão `8000`, limitado a ≥ 200) define o tamanho abaixo do qual um resultado de ferramenta é deixado intocado. `proactive_prune_min_reclaim_tokens` (padrão `4096`) impede que uma poda seja efetivada a menos que recupere pelo menos essa quantidade de tokens — uma poda efetivada reescreve o histórico já enviado e invalida o prefixo de cache de prompt do provedor, então essa proteção mantém essas quebras de cache episódicas e amortizadas (uma quebra significativa, como um limite de compressão) em vez de disparar a cada iteração de ferramenta. Isso roda apenas sob o motor `compressor` embutido; outros motores de contexto herdam um no-op.

:::tip Recarga a quente do gateway para compressão e comprimento de contexto
Nas versões mais recentes, editar `model.context_length` ou qualquer chave `compression.*` em `config.yaml` em um gateway em execução tem efeito na próxima mensagem — sem reinício do gateway, sem `/reset`, sem necessidade de rotação de sessão. A assinatura do agente em cache inclui essas chaves, então o gateway reconstrói o agente de forma transparente quando vê uma mudança. Chaves de API e configuração de ferramenta/skill ainda exigem os caminhos usuais de recarga.
:::

### Configurações comuns {#common-setups}

**Padrão (detecção automática) — nenhuma configuração necessária:**
```yaml
compression:
  enabled: true
  threshold: 0.50
```
Usa seu provedor principal e modelo principal. Sobrescreva por tarefa (por exemplo, `auxiliary.compression.provider: openrouter` + `model: google/gemini-2.5-flash`) se você quiser compressão em um modelo mais barato do que seu modelo de chat principal.

**Forçar um provedor específico** (baseado em OAuth ou chave de API):
```yaml
auxiliary:
  compression:
    provider: nous
    model: gemini-3-flash
```
Funciona com qualquer provedor: `nous`, `openrouter`, `codex`, `anthropic`, `main`, etc.

**Endpoint personalizado** (auto-hospedado, Ollama, zai, DeepSeek, etc.):
```yaml
auxiliary:
  compression:
    model: glm-4.7
    base_url: https://api.z.ai/api/coding/paas/v4
```
Aponta para um endpoint personalizado compatível com OpenAI. Usa `OPENAI_API_KEY` para autenticação.

### Como os três ajustes interagem {#how-the-three-knobs-interact}

| `auxiliary.compression.provider` | `auxiliary.compression.base_url` | Resultado |
|---------------------|---------------------|--------|
| `auto` (padrão) | não definido | Detecta automaticamente o melhor provedor disponível |
| `nous` / `openrouter` / etc. | não definido | Força esse provedor, usa sua autenticação |
| qualquer | definido | Usa o endpoint personalizado diretamente (provedor ignorado) |

:::warning Requisito de comprimento de contexto do modelo de resumo
O modelo de resumo **deve** ter uma janela de contexto pelo menos tão grande quanto a do seu modelo de agente principal. O compressor envia a seção do meio completa da conversa para o modelo de resumo — se a janela de contexto desse modelo for menor do que a do modelo principal, a chamada de sumarização falhará com um erro de comprimento de contexto. Quando isso acontece, os turnos do meio são **descartados sem um resumo**, perdendo silenciosamente o contexto da conversa. Se você sobrescrever o modelo, verifique se o comprimento de contexto dele atende ou excede o do seu modelo principal.
:::

## Watchdog de Sessão Travada {#session-stall-watchdog}

O gateway executa um watchdog de travamento apenas para notificação (`agent.session_stall_timeout`, padrão `300` segundos, `0` = desativado). Quando uma sessão ocupada tem um **acompanhamento de entrada pendente** e o relógio de atividade compartilhado do agente esteve ocioso por pelo menos esse tempo, o gateway registra um WARNING e envia ao usuário uma notificação única:

```
⚠️ Agent session appears stalled (last activity N min ago). Try /new to reset.
```

Semântica:

- **Apenas notificação.** O watchdog nunca mata o turno — em contraste, `agent.gateway_timeout` cancela uma execução após inatividade prolongada. O aviso de travamento apenas informa que o agente parece emperrado, para que você possa decidir (`/new`, `/stop`, ou continuar esperando).
- **Uma notificação por episódio de travamento.** A trava é liberada quando o acompanhamento pendente é drenado ou a atividade é retomada, então uma sessão que se recupera e trava novamente notifica novamente.
- O progresso vem apenas do snapshot de atividade compartilhado (chamadas de ferramenta, progresso de stream de API, heartbeats de compressão). O acompanhamento pendente é um gatilho de notificação, não um relógio de progresso.

```yaml
agent:
  session_stall_timeout: 300   # seconds; 0 disables the watchdog
```

## Motor de Contexto {#context-engine}

O motor de contexto controla como as conversas são gerenciadas ao se aproximar do limite de tokens do modelo. O motor embutido `compressor` usa sumarização com perdas (veja [Context Compression](/developer-guide/context-compression-and-caching)). Motores plugin podem substituí-lo por estratégias alternativas.

```yaml
context:
  engine: "compressor"    # default — built-in lossy summarization
```

Para usar um motor plugin (por exemplo, LCM para gerenciamento de contexto sem perdas):

```yaml
context:
  engine: "lcm"          # must match the plugin's name
```

Motores plugin **nunca são ativados automaticamente** — você deve definir explicitamente `context.engine` para o nome do plugin. Motores disponíveis podem ser explorados e selecionados via `hermes plugins` → Provider Plugins → Context Engine.

Veja [Memory Providers](/user-guide/features/memory-providers) para o sistema análogo de seleção única para plugins de memória.

## Orçamento de Iteração {#iteration-budget}

Quando o agente está trabalhando em uma tarefa complexa com muitas chamadas de ferramenta, ele pode consumir todo o seu orçamento de iteração (padrão: 500 turnos). O Hermes **não** injeta avisos de pressão no meio da tarefa — versões anteriores avisavam o modelo em 70%/90% do orçamento, o que fazia os modelos abandonarem tarefas complexas prematuramente, e isso foi removido em abril de 2026.

Em vez disso, quando o orçamento é realmente esgotado (500/500), o Hermes injeta uma mensagem pedindo ao modelo para encerrar e permite uma única **chamada de cortesia** para que ele possa entregar uma resposta final. Se essa chamada de cortesia ainda não produzir texto, o agente é solicitado a resumir o que conseguiu realizar.

```yaml
agent:
  max_turns: 500               # Max iterations per conversation turn (default: 500)
  api_max_retries: 3           # Retries per provider before fallback engages (default: 3)
```

Quando o orçamento de iteração é totalmente esgotado, a CLI mostra uma notificação ao usuário: `⚠ Iteration budget reached (500/500) — response may be incomplete`.

`agent.api_max_retries` controla quantas vezes o Hermes tenta novamente uma chamada de API do provedor em erros transitórios (limites de taxa, quedas de conexão, 5xx) **antes** que a troca para provedor de fallback entre em ação. O padrão é `3` — quatro tentativas no total. Se você tiver [provedores de fallback](/user-guide/features/fallback-providers) configurados e quiser fazer o failover mais rápido, diminua isso para `0`, para que o primeiro erro transitório no seu provedor primário faça o handoff imediato para o fallback em vez de repetir tentativas contra o endpoint instável.

## Verify-on-Stop (verificação de código) {#verify-on-stop-coding-verification}

Quando habilitado, o Hermes se recusa a aceitar uma resposta final em um turno em que o agente editou código em um workspace mas não produziu nenhuma evidência de verificação recente (uma execução de teste, build, lint etc. bem-sucedida) — ele injeta um acompanhamento sintético pedindo ao agente para verificar ou explicar por que não pode. Edições apenas de documentação/markdown/skill nunca disparam isso, e o loop é limitado para que nunca possa prender o agente.

```yaml
agent:
  verify_on_stop: false        # true | false | "auto" (surface-aware: on for CLI/TUI/desktop, off for messaging)
  verify_guidance: true        # Append creative-UI / clean-diff guidance to the missing-evidence nudge
  max_verify_nudges: 3         # Cap on consecutive continue nudges per turn (built-in + pre_verify hooks)
  coding_instructions: ""      # Standing project-wide coding rules appended to the coding brief
```

`verify_on_stop` aceita `true` (ligado em todo lugar), `false` (desligado), ou `"auto"` (ligado para superfícies de codificação interativas — CLI, TUI, desktop — e chamadores programáticos; desligado para superfícies de mensagens como Telegram/Discord, onde a narrativa de verificação soa como ruído de chat). A migração de configuração desliga isso em instalações existentes, então trate desligado como o padrão efetivo e ative explicitamente. A variável de ambiente `HERMES_VERIFY_ON_STOP` sobrescreve o valor de configuração quando definida.

Para uma restrição de política de usuário/plugin no mesmo ponto — manter o agente andando com suas próprias verificações — veja o [hook `pre_verify`](/user-guide/features/hooks#pre_verify).

## Objetivos Permanentes (`/goal`) {#standing-goals-goal}

Quando um objetivo permanente está ativo, o Hermes avalia se cada resposta do assistente o satisfaz. Se não, ele alimenta um prompt de continuação de volta na mesma sessão e continua trabalhando até que o objetivo seja concluído, o orçamento de turnos se esgote, ou o usuário pause/limpe-o. O orçamento de turnos é o verdadeiro mecanismo de segurança — falhas do juiz falham de forma **aberta** (continuar), então um juiz instável nunca emperra o progresso.

```yaml
goals:
  max_turns: 20   # Max continuation turns before Hermes auto-pauses the goal (default: 20)
```

`max_turns` limita quantos turnos de continuação um objetivo pode dirigir antes que o Hermes o pause automaticamente e peça ao usuário para fazer `/goal resume`. Isso protege contra falsos negativos do juiz (objetivo realmente concluído, mas o juiz diz para continuar) e gasto ilimitado do modelo em objetivos vagos ou inalcançáveis. Veja [Goals](/user-guide/features/goals) para a funcionalidade completa.

### Timeouts de API {#api-timeouts}

O Hermes tem camadas de timeout separadas para streaming, além de um detector de obsolescência para chamadas não-streaming. Os detectores de obsolescência se ajustam automaticamente apenas para provedores locais quando você os deixa em seus padrões implícitos.

| Timeout | Padrão | Provedores locais | Config / env |
|---------|---------|----------------|--------------|
| Timeout de leitura de socket | 120s | Elevado automaticamente para 1800s | `HERMES_STREAM_READ_TIMEOUT` |
| Detecção de stream obsoleto | 180s | Elevado para um teto de 900s (`agent.local_stream_stale_timeout`) | `HERMES_STREAM_STALE_TIMEOUT` |
| Detecção de não-stream obsoleto | 90s | Desativado automaticamente quando deixado implícito | `providers.<id>.stale_timeout_seconds` ou `HERMES_API_CALL_STALE_TIMEOUT` |
| Chamada de API (não-streaming) | 1800s | Inalterado | `providers.<id>.request_timeout_seconds` / `timeout_seconds` ou `HERMES_API_TIMEOUT` |

O **timeout de leitura de socket** controla quanto tempo o httpx espera pelo próximo pedaço de dados do provedor. LLMs locais podem levar minutos para o prefill em contextos grandes antes de produzir o primeiro token, então o Hermes aumenta isso para 30 minutos quando detecta um endpoint local. Se você definir explicitamente `HERMES_STREAM_READ_TIMEOUT`, esse valor é sempre usado, independentemente da detecção de endpoint.

A **detecção de stream obsoleto** encerra conexões que recebem pings de keep-alive SSE, mas nenhum conteúdo real. Para provedores locais (que não enviam pings de keep-alive durante o prefill), o padrão é elevado para um teto finito de 900 segundos em vez da base de 180s — configurável via `agent.local_stream_stale_timeout` ou a variável de ambiente `HERMES_LOCAL_STREAM_STALE_TIMEOUT`.

A **detecção de não-stream obsoleto** encerra chamadas não-streaming que não produzem resposta por tempo demais. Por padrão, o Hermes desativa isso em endpoints locais para evitar falsos positivos durante prefills longos. Se você definir explicitamente `providers.<id>.stale_timeout_seconds`, `providers.<id>.models.<model>.stale_timeout_seconds`, ou `HERMES_API_CALL_STALE_TIMEOUT`, esse valor explícito é respeitado mesmo em endpoints locais.

## Avisos de Pressão de Contexto {#context-pressure-warnings}

Separadamente da pressão do orçamento de iteração, a pressão de contexto rastreia o quão perto a conversa está do **limite de compactação** — o ponto em que a compressão de contexto dispara para resumir mensagens mais antigas. Isso ajuda tanto você quanto o agente a entenderem quando a conversa está ficando longa.

| Progresso | Nível | O que acontece |
|----------|-------|-------------|
| **≥ 60%** até o limite | Info | A CLI mostra uma barra de progresso ciano; o gateway envia um aviso informativo |
| **≥ 85%** até o limite | Aviso | A CLI mostra uma barra amarela em negrito; o gateway avisa que a compactação é iminente |

Na CLI, a pressão de contexto aparece como uma barra de progresso no feed de saída de ferramentas:

```
  ◐ context ████████████░░░░░░░░ 62% to compaction  48k threshold (50%) · approaching compaction
```

Nas plataformas de mensagens, uma notificação em texto simples é enviada:

```
◐ Context: ████████████░░░░░░░░ 62% to compaction (threshold: 50% of window).
```

Se a compressão automática estiver desativada, o aviso informa que o contexto pode ser truncado em vez disso.

A pressão de contexto é automática — nenhuma configuração necessária. Ela dispara puramente como uma notificação voltada ao usuário e não modifica o fluxo de mensagens nem injeta nada no contexto do modelo.

## Estratégias de Pool de Credenciais {#credential-pool-strategies}

Quando você tem múltiplas chaves de API ou tokens OAuth para o mesmo provedor, configure a estratégia de rotação:

```yaml
credential_pool_strategies:
  openrouter: round_robin    # cycle through keys evenly
  anthropic: least_used      # always pick the least-used key
```

Opções: `fill_first` (padrão), `round_robin`, `least_used`, `random`. Veja [Credential Pools](/user-guide/features/credential-pools) para a documentação completa.

## Cache de prompt {#prompt-caching}

O Hermes ativa automaticamente o cache de prompt entre sessões quando o provedor ativo o suporta — nenhuma configuração do usuário é necessária.

Para Claude via **Anthropic nativo**, **OpenRouter** e **Nous Portal**, o Hermes anexa pontos de interrupção `cache_control` com o TTL de 1 hora (`ttl: "1h"`) no prompt de sistema e nos blocos de skill. O primeiro envio dentro de uma hora nova paga as taxas de entrada completas; envios subsequentes em qualquer sessão dentro da mesma hora usam o cache com a taxa de leitura em cache com desconto. Isso significa que o prompt de sistema, o conteúdo de skill carregado e a parte inicial de qualquer inclusão de contexto longo são reutilizados entre sessões do `hermes` e entre subagentes bifurcados durante a primeira hora.

O upstream do Qwen Cloud (Alibaba DashScope) limita o TTL de cache a 5 minutos, então o Hermes usa o TTL de ponto de interrupção de 5 minutos lá. Outros caminhos de Claude via terceiros (AWS Bedrock, Azure Foundry) recorrem aos padrões de cache próprios do provedor. O xAI Grok usa um mecanismo separado de id de conversa fixado por sessão — veja [xAI prompt caching](/integrations/providers#xai-grok--responses-api--prompt-caching).

Não existe um ajuste para desativar isso — o cache está sempre ativo e economiza dinheiro mesmo em conversas de turno único, porque o prompt de sistema sozinho já é uma fração significativa da contagem de tokens de entrada.

O único ajuste explícito é o nível de TTL de cache que o Hermes solicita em pontos de interrupção no estilo Anthropic:

```yaml
prompt_caching:
  cache_ttl: "5m"   # "5m" or "1h" (Anthropic-supported tiers); other values are ignored
```

`cache_ttl` seleciona o TTL de ponto de interrupção que o Hermes anexa para Claude via a API Anthropic nativa, OpenRouter e Nous Portal. Apenas os dois níveis suportados pela Anthropic (`"5m"`, `"1h"`) são respeitados — qualquer outro valor é ignorado. Provedores com seus próprios limites (por exemplo, Qwen Cloud, que tem um máximo de 5 minutos) ainda se ajustam ao que o upstream permite.

## Modelos Auxiliares {#auxiliary-models}

O Hermes usa modelos "auxiliares" para tarefas paralelas, como análise de imagem, resumo de página web, análise de captura de tela do navegador, geração de título de sessão e compressão de contexto. Por padrão (`auxiliary.*.provider: "auto"`), o Hermes roteia toda tarefa auxiliar para o seu **modelo de chat principal** — o mesmo provedor/modelo que você escolheu em `hermes model`. Você não precisa configurar nada para começar, mas esteja ciente de que, em modelos de raciocínio caros (Opus, MiniMax M2.7, etc.), tarefas auxiliares adicionam custo significativo. Se você quiser tarefas paralelas baratas e rápidas independentemente do seu modelo principal, defina `auxiliary.<task>.provider` e `auxiliary.<task>.model` explicitamente (por exemplo, Gemini Flash no OpenRouter para visão e extração web).

:::note Por que "auto" usa seu modelo principal
Versões anteriores separavam usuários de agregadores (OpenRouter, Nous Portal) para um padrão barato do lado do provedor. Isso era surpreendente — usuários que pagaram por uma assinatura de agregador veriam um modelo diferente lidando com seu tráfego auxiliar. Agora `auto` usa o modelo principal para todos, e sobrescritas por tarefa em `config.yaml` ainda prevalecem (veja [Referência completa de configuração auxiliar](#full-auxiliary-config-reference) abaixo).
:::

### Configurando modelos auxiliares interativamente {#configuring-auxiliary-models-interactively}

Em vez de editar YAML manualmente, execute `hermes model` e escolha **"Configure auxiliary models"** no menu. Você obterá um seletor interativo por tarefa:

```
$ hermes model
→ Configure auxiliary models

[ ] vision               currently: auto / main model
[ ] web_extract          currently: auto / main model
[ ] title_generation     currently: openrouter / google/gemini-3-flash-preview
[ ] tts_audio_tags       currently: auto / main model
[ ] compression          currently: auto / main model
[ ] approval             currently: auto / main model
[ ] triage_specifier     currently: auto / main model
[ ] kanban_decomposer    currently: auto / main model
[ ] profile_describer    currently: auto / main model
```

Selecione uma tarefa, escolha um provedor (fluxos OAuth abrem um navegador; provedores por chave de API perguntam), escolha um modelo. A mudança persiste em `auxiliary.<task>.*` em `config.yaml`. Mesmo mecanismo do seletor de modelo principal — nenhuma sintaxe extra para aprender.

Se você não quiser que o Hermes gere títulos automaticamente após a primeira troca, defina
`auxiliary.title_generation.enabled: false`. Títulos manuais ainda funcionam através de
`/title` e `hermes sessions rename`.

### Endpoints somente-stream {#stream-only-endpoints}

Alguns endpoints compatíveis com OpenAI rejeitam completamente solicitações de chat não-streaming (por exemplo, o Tencent Copilot retorna HTTP 400 `"Non-stream chat request is currently not supported"`). O chat interativo já faz streaming, mas tarefas auxiliares (geração de título, compressão, extração web) usam chamadas não-streaming e falhariam em cada tentativa. O Hermes sempre trata `copilot.tencent.com` como somente-stream; para qualquer outro endpoint assim, liste uma substring de URL sob `auxiliary.stream_only_base_urls`:

```yaml
auxiliary:
  stream_only_base_urls:
    - "my-stream-only-proxy.example.com"
```

Chamadas auxiliares correspondentes são enviadas com `stream=True` e os pedaços (incluindo deltas de chamada de ferramenta) são agregados no lado do cliente — nenhuma mudança de comportamento para qualquer outro endpoint.

### Tutorial em Vídeo {#video-tutorial}

<div style={{position: 'relative', width: '100%', aspectRatio: '16 / 9', marginBottom: '1.5rem'}}>
  <iframe
    src="https://www.youtube.com/embed/NoF-YajElIM"
    title="Hermes Agent — Auxiliary Models Tutorial"
    style={{position: 'absolute', top: 0, left: 0, width: '100%', height: '100%', border: 0}}
    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
    allowFullScreen
  />
</div>

### O padrão universal de configuração {#the-universal-config-pattern}

Todo slot de modelo no Hermes — tarefas auxiliares, compressão, fallback — usa os mesmos três ajustes:

| Chave | O que faz | Padrão |
|-----|-------------|---------|
| `provider` | Qual provedor usar para autenticação e roteamento | `"auto"` |
| `model` | Qual modelo solicitar | padrão do provedor |
| `base_url` | Endpoint personalizado compatível com OpenAI (sobrescreve o provedor) | não definido |

Blocos de tarefa auxiliar aceitam adicionalmente um ajuste `reasoning_effort`:

| Chave | O que faz | Padrão |
|-----|-------------|---------|
| `reasoning_effort` | Nível de raciocínio para as chamadas de LLM dessa tarefa: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra` | não definido (padrão do provedor) |

Essa é a contraparte por tarefa do `agent.reasoning_effort` global: rode a compressão em `low` ou a visão em `none` para reduzir a latência e o custo de tarefas paralelas quando seu modelo principal é um modelo de raciocínio caro, sem tocar no comportamento do seu chat principal. Funciona em todo bloco de tarefa auxiliar (`vision`, `web_extract`, `compression`, `title_generation`, `curator`, `background_review`, ...), em todos os três formatos de conexão auxiliares (chat completions, Codex Responses, Anthropic Messages). Um `extra_body.reasoning` explícito no mesmo bloco de tarefa prevalece sobre o atalho.

O MoA é a única exceção: a profundidade de raciocínio para o Mixture-of-Agents é configurada **por slot** no preset MoA (`moa.presets.<name>.reference_models[].reasoning_effort` / `aggregator.reasoning_effort`), não nos blocos auxiliares `moa_reference`/`moa_aggregator` — veja [Mixture of Agents](/user-guide/features/mixture-of-agents).

```yaml
auxiliary:
  compression:
    reasoning_effort: "low"    # summaries don't need deep thinking
  vision:
    reasoning_effort: "none"   # disable thinking for image description
```

Quando `base_url` está definido, o Hermes ignora o provedor e chama esse endpoint diretamente (usando `api_key` ou `OPENAI_API_KEY` para autenticação). Quando apenas `provider` está definido, o Hermes usa a autenticação e URL base embutidas desse provedor.

Provedores disponíveis para tarefas auxiliares: `auto`, `main`, além de qualquer provedor no [registro de provedores](/reference/environment-variables) — `openrouter`, `nous`, `openai-codex`, `copilot`, `copilot-acp`, `anthropic`, `gemini`, `qwen-oauth`, `zai`, `kimi-coding`, `kimi-coding-cn`, `minimax`, `minimax-cn`, `minimax-oauth`, `deepseek`, `nvidia`, `xai`, `xai-oauth`, `ollama-cloud`, `alibaba`, `bedrock`, `huggingface`, `arcee`, `xiaomi`, `kilocode`, `opencode-zen`, `opencode-go`, `ai-gateway`, `azure-foundry` — ou qualquer provedor personalizado nomeado do seu dict `providers:` (por exemplo, `provider: "beans"`).

:::tip MiniMax OAuth
`minimax-oauth` faz login via OAuth pelo navegador (nenhuma chave de API necessária). Execute `hermes model` e selecione **MiniMax (OAuth)** para autenticar. Tarefas auxiliares usam `MiniMax-M2.7-highspeed` automaticamente. Veja o [guia MiniMax OAuth](../guides/minimax-oauth.md).
:::

:::tip xAI Grok OAuth
`xai-oauth` faz login via OAuth pelo navegador para assinantes SuperGrok e X Premium+ (nenhuma chave de API necessária). Execute `hermes model` e selecione **xAI Grok OAuth (SuperGrok / Premium+)** para autenticar. O mesmo token OAuth é reutilizado para toda superfície direta-para-xAI (chat, tarefas auxiliares, TTS, geração de imagem, geração de vídeo, transcrição). Veja o [guia xAI Grok OAuth](../guides/xai-grok-oauth.md), e se o Hermes estiver em um host remoto veja [OAuth over SSH / Remote Hosts](../guides/oauth-over-ssh.md).
:::

:::warning `"main"` é apenas para tarefas auxiliares
A opção de provedor `"main"` significa "usar o mesmo provedor que meu agente principal usa" — só é válida dentro de `auxiliary:`, `compression:` e entradas primárias de fallback (`fallback_providers:` ou o legado `fallback_model:`). Ela **não** é um valor válido para sua configuração de nível superior `model.provider`. Se você usa um endpoint personalizado compatível com OpenAI, defina `provider: custom` na sua seção `model:`. Veja [AI Providers](/integrations/providers) para todas as opções de provedor do modelo principal.
:::

### Referência completa de configuração auxiliar {#full-auxiliary-config-reference}

```yaml
auxiliary:
  # Image analysis (vision_analyze tool + browser screenshots)
  vision:
    provider: "auto"           # "auto", "openrouter", "nous", "codex", "main", etc.
    model: ""                  # e.g. "openai/gpt-4o", "google/gemini-2.5-flash"
    base_url: ""               # Custom OpenAI-compatible endpoint (overrides provider)
    api_key: ""                # API key for base_url (falls back to OPENAI_API_KEY)
    timeout: 120               # seconds — LLM API call timeout; vision payloads need generous timeout
    download_timeout: 30       # seconds — image HTTP download; increase for slow connections
    max_concurrency: 8         # max concurrent image encode/resize bursts across the process
                               # (default: host CPU core count, no ceiling) — bounds only the
                               # CPU-bound encode step so a video-frame fan-out can't saturate
                               # every core and starve the event loop; LLM calls stay fully
                               # concurrent. Minimum 1; values < 1 are ignored.

  # Web page summarization + browser page text extraction
  web_extract:
    provider: "auto"
    model: ""                  # e.g. "google/gemini-2.5-flash"
    base_url: ""
    api_key: ""
    timeout: 360               # seconds (6min) — per-attempt LLM summarization

  # Dangerous command approval classifier
  approval:
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30                # seconds

  # Gemini 3.1 TTS hidden audio-tag insertion
  tts_audio_tags:
    provider: "auto"
    model: ""                  # empty = main chat model
    base_url: ""
    api_key: ""
    timeout: 30

  # Context compression timeout (separate from compression.* config)
  compression:
    timeout: 120               # seconds — compression summarizes long conversations, needs more time
    # fallback_chain:           # Optional — providers to try on rate-limit / connectivity failure
    #   - provider: nous
    #     model: deepseek/deepseek-chat
    #   - provider: openrouter
    #     model: google/gemini-2.5-flash
    #     base_url: ""
    #     api_key: ""

  # Auto-generated session titles. Empty language follows the conversation;
  # set e.g. "English" or "Japanese" to pin titles to one language.
  title_generation:
    enabled: true              # set false to disable auto-title generation
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30
    language: ""

  # Skills hub — skill matching and search
  skills_hub:
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30

  # MCP tool dispatch
  mcp:
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30

  # Kanban triage specifier — `hermes kanban specify <id>` (or the
  # dashboard's ✨ Specify button on Triage-column cards) uses this
  # slot to expand a one-liner into a concrete spec and promote the
  # task to `todo`. Cheap fast models work well here; spec expansion
  # is short and doesn't need reasoning depth.
  triage_specifier:
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 120
```

:::tip
Cada tarefa auxiliar tem um `timeout` configurável (em segundos). Padrões: vision 120s, web_extract 360s, approval 30s, compression 120s. Aumente-os se você usar modelos locais lentos para tarefas auxiliares. A visão também tem um `download_timeout` separado (padrão 30s) para o download HTTP da imagem — aumente para conexões lentas ou servidores de imagem auto-hospedados.
:::

:::info
A compressão de contexto tem seu próprio bloco `compression:` para limites e um bloco `auxiliary.compression:` para configurações de modelo/provedor — veja [Context Compression](#context-compression) acima. A cadeia de fallback principal usa uma lista `fallback_providers:` de nível superior — veja [Fallback Providers](/integrations/providers#fallback-providers). Todos os três seguem o mesmo padrão provider/model/base_url.
:::

### Cadeia de fallback por tarefa para tarefas auxiliares {#per-task-fallback-chain-for-auxiliary-tasks}

Cada tarefa auxiliar pode opcionalmente definir uma `fallback_chain` — uma lista de entradas provider/model que o Hermes tenta quando o provedor auxiliar principal falha devido a limites de taxa, problemas de conectividade ou restrições de pagamento:

```yaml
auxiliary:
  compression:
    provider: openrouter
    model: openai/gpt-4o-mini
    fallback_chain:
      - provider: nous
        model: deepseek/deepseek-chat
      - provider: openrouter
        model: google/gemini-2.5-flash
```

Quando o provedor auxiliar principal (`openrouter` / `openai/gpt-4o-mini`) retorna um erro de limite de taxa, timeout de conexão ou pagamento obrigatório, o Hermes percorre a `fallback_chain` em ordem. Ele pula entradas cujo provedor corresponde ao provedor já falhado, e tenta cada entrada restante até que uma tenha sucesso ou a cadeia se esgote. Se todos os fallbacks falharem, o Hermes recorre ao modelo do agente principal como uma rede de segurança final.

Cada entrada suporta os mesmos três ajustes de qualquer configuração de tarefa auxiliar:

| Chave | Descrição |
|-----|-------------|
| `provider` | Nome do provedor (`nous`, `openrouter`, `anthropic`, `gemini`, `main`, etc.) |
| `model` | Nome do modelo para esse provedor |
| `base_url` | (Opcional) Endpoint personalizado compatível com OpenAI |

`fallback_chain` está disponível em qualquer tarefa auxiliar — `compression`, `vision`, `web_extract`, `approval`, `skills_hub`, `mcp`, etc.

### Roteamento OpenRouter e Pareto Code para tarefas auxiliares {#openrouter-routing--pareto-code-for-auxiliary-tasks}

Quando uma tarefa auxiliar resolve para o OpenRouter (seja explicitamente ou via `provider: "main"` enquanto seu agente principal está no OpenRouter), as configurações `provider_routing` e `openrouter.min_coding_score` do agente principal **não são propagadas** — por design, cada tarefa auxiliar é independente. Para definir preferências de roteamento de provedor do OpenRouter ou usar o [roteador Pareto Code](/integrations/providers#openrouter-pareto-code-router) para uma tarefa auxiliar específica, defina-as por tarefa via `extra_body`:

```yaml
auxiliary:
  compression:
    provider: openrouter
    model: openrouter/pareto-code         # use the Pareto Code router for this task
    extra_body:
      provider:                            # OpenRouter provider routing prefs
        order: [anthropic, google]         # try these providers in order
        sort: throughput                   # or "price" | "latency"
        # only: [anthropic]                # restrict to a specific provider
        # ignore: [deepinfra]              # exclude specific providers
      plugins:                             # OpenRouter Pareto Code router knob
        - id: pareto-router
          min_coding_score: 0.5            # 0.0–1.0; higher = stronger coders
```

O formato espelha o que o OpenRouter aceita no corpo da requisição de chat completions. O Hermes encaminha todo o `extra_body` literalmente, então qualquer outro campo do corpo de requisição do OpenRouter documentado em [openrouter.ai/docs](https://openrouter.ai/docs) funciona da mesma forma.

### Mudando o Modelo de Visão {#changing-the-vision-model}

Para usar o GPT-4o em vez do Gemini Flash para análise de imagem:

```yaml
auxiliary:
  vision:
    model: "openai/gpt-4o"
```

Ou via variável de ambiente (em `~/.hermes/.env`):

```bash
AUXILIARY_VISION_MODEL=openai/gpt-4o
```

### Opções de Provedor {#provider-options}

Essas opções se aplicam às **configurações de tarefa auxiliar** (`auxiliary:`, `compression:`) e às entradas primárias de fallback (`fallback_providers:` ou o legado `fallback_model:`), não à sua configuração principal `model.provider`.

| Provedor | Descrição | Requisitos |
|----------|-------------|-------------|
| `"auto"` | Melhor disponível (padrão). A visão tenta OpenRouter → Nous → Codex. | — |
| `"openrouter"` | Força o OpenRouter — roteia para qualquer modelo (Gemini, GPT-4o, Claude, etc.) | `OPENROUTER_API_KEY` |
| `"nous"` | Força o Nous Portal | `hermes auth` |
| `"codex"` | Força OAuth do Codex (conta ChatGPT). Suporta visão (gpt-5.3-codex). | `hermes model` → Codex |
| `"minimax-oauth"` | Força o MiniMax OAuth (login pelo navegador, sem chave de API). Usa MiniMax-M2.7-highspeed para tarefas auxiliares. | `hermes model` → MiniMax (OAuth) |
| `"xai-oauth"` | Força o xAI Grok OAuth (login pelo navegador para assinantes SuperGrok ou X Premium+, sem chave de API). O mesmo token OAuth cobre chat, TTS, imagem, vídeo e transcrição. | `hermes model` → xAI Grok OAuth (SuperGrok / Premium+) |
| `"main"` | Usa seu endpoint principal/personalizado ativo. Isso pode vir de `OPENAI_BASE_URL` + `OPENAI_API_KEY` ou de um endpoint personalizado salvo via `hermes model` / `config.yaml`. Funciona com OpenAI, modelos locais, ou qualquer API compatível com OpenAI. **Apenas para tarefas auxiliares — não válido para `model.provider`.** | Credenciais de endpoint personalizado + URL base |

Provedores diretos por chave de API do catálogo principal de provedores também funcionam aqui quando você quer que tarefas paralelas contornem seu roteador padrão. Por exemplo, `gmi` é válido assim que `GMI_API_KEY` estiver configurado, e `fireworks` é válido assim que `FIREWORKS_API_KEY` estiver configurado:

```yaml
auxiliary:
  compression:
    provider: "gmi"
    model: "anthropic/claude-opus-4.6"
```

Para roteamento auxiliar via GMI, use o ID de modelo exato retornado pelo endpoint `/v1/models` do GMI. IDs de modelo do Fireworks usam o formato nativo com barra do provedor, por exemplo `accounts/fireworks/models/glm-5p2`.

### Configurações Comuns {#common-setups-1}

**Usando um endpoint personalizado direto** (mais claro que `provider: "main"` para APIs locais/auto-hospedadas):
```yaml
auxiliary:
  vision:
    base_url: "http://localhost:1234/v1"
    api_key: "local-key"
    model: "qwen2.5-vl"
```

`base_url` tem precedência sobre `provider`, então essa é a forma mais explícita de rotear uma tarefa auxiliar para um endpoint específico. Para sobrescritas de endpoint diretas, o Hermes usa o `api_key` configurado ou recorre a `OPENAI_API_KEY`; ele não reutiliza `OPENROUTER_API_KEY` para esse endpoint personalizado.

**Usando uma chave de API OpenAI para visão:**
```yaml
# In ~/.hermes/.env:
# OPENAI_BASE_URL=https://api.openai.com/v1
# OPENAI_API_KEY=sk-...

auxiliary:
  vision:
    provider: "main"
    model: "gpt-4o"       # or "gpt-4o-mini" for cheaper
```

**Usando o OpenRouter para visão** (roteia para qualquer modelo):
```yaml
auxiliary:
  vision:
    provider: "openrouter"
    model: "openai/gpt-4o"      # or "google/gemini-2.5-flash", etc.
```

**Usando o Codex OAuth** (conta ChatGPT Pro/Plus — nenhuma chave de API necessária):
```yaml
auxiliary:
  vision:
    provider: "codex"     # uses your ChatGPT OAuth token
    # model defaults to gpt-5.3-codex (supports vision)
```

**Usando o MiniMax OAuth** (login pelo navegador, nenhuma chave de API necessária):
```yaml
model:
  default: MiniMax-M2.7
  provider: minimax-oauth
  base_url: https://api.minimax.io/anthropic
```
Execute `hermes model` e selecione **MiniMax (OAuth)** para fazer login e definir isso automaticamente. Para a região da China, a URL base será `https://api.minimaxi.com/anthropic`. Veja o [guia MiniMax OAuth](../guides/minimax-oauth.md) para o passo a passo completo.

**Usando um modelo local/auto-hospedado:**
```yaml
auxiliary:
  vision:
    provider: "main"      # uses your active custom endpoint
    model: "my-local-model"
```

`provider: "main"` usa qualquer provedor que o Hermes use para chat normal — seja um provedor personalizado nomeado (por exemplo, `beans`), um provedor embutido como `openrouter`, ou um endpoint legado `OPENAI_BASE_URL`.

:::tip
Se você usa o Codex OAuth como seu provedor de modelo principal, a visão funciona automaticamente — nenhuma configuração extra necessária. O Codex está incluído na cadeia de detecção automática para visão.
:::

:::warning
**A visão requer um modelo multimodal.** Se você definir `provider: "main"`, certifique-se de que seu endpoint suporta multimodal/visão — caso contrário, a análise de imagem falhará.
:::

### Variáveis de Ambiente (legado) {#environment-variables-legacy}

Modelos auxiliares também podem ser configurados via variáveis de ambiente. No entanto, `config.yaml` é o método preferido — é mais fácil de gerenciar e suporta todas as opções, incluindo `base_url` e `api_key`.

| Configuração | Variável de Ambiente |
|---------|---------------------|
| Provedor de visão | `AUXILIARY_VISION_PROVIDER` |
| Modelo de visão | `AUXILIARY_VISION_MODEL` |
| Endpoint de visão | `AUXILIARY_VISION_BASE_URL` |
| Chave de API de visão | `AUXILIARY_VISION_API_KEY` |
| Provedor de extração web | `AUXILIARY_WEB_EXTRACT_PROVIDER` |
| Modelo de extração web | `AUXILIARY_WEB_EXTRACT_MODEL` |
| Endpoint de extração web | `AUXILIARY_WEB_EXTRACT_BASE_URL` |
| Chave de API de extração web | `AUXILIARY_WEB_EXTRACT_API_KEY` |

Configurações de compressão e modelo de fallback são exclusivas do config.yaml.

:::tip
Execute `hermes config` para ver suas configurações atuais de modelo auxiliar. Sobrescritas só aparecem quando diferem dos padrões.
:::

## Esforço de Raciocínio {#reasoning-effort}

Controle quanto "pensamento" o modelo faz antes de responder:

```yaml
agent:
  reasoning_effort: ""   # empty = medium. Options: none, minimal, low, medium, high, xhigh, max, ultra
```

Quando não definido (padrão), o esforço de raciocínio assume "medium" — um nível equilibrado que funciona bem para a maioria das tarefas. Definir um valor o sobrescreve — esforço de raciocínio mais alto oferece melhores resultados em tarefas complexas ao custo de mais tokens e latência.

:::note Modelos de pensamento adaptativo (Claude 4.6+, classe Fable/Mythos) via OpenRouter
Esses modelos usam pensamento *adaptativo* e não aceitam o campo usual `reasoning.effort`
— o OpenRouter o ignora para eles. O Hermes roteia seu
`reasoning_effort` de forma transparente para o parâmetro `verbosity` do OpenRouter em vez disso (que mapeia para
`output_config.effort` da Anthropic), então o mesmo ajuste de esforço continua funcionando com
os níveis suportados pelo modelo selecionado. `none` (ou não definido) deixa o modelo
no seu próprio padrão adaptativo. O
provedor Anthropic nativo já controla o esforço diretamente e não é afetado.
:::

Você também pode mudar o esforço de raciocínio em tempo de execução com o comando `/reasoning`:

```
/reasoning                # Show current effort level and display state
/reasoning high           # Set reasoning effort to high (this session only)
/reasoning high --global  # Set effort and persist to config.yaml
/reasoning none           # Disable reasoning (this session only)
/reasoning show           # Show model thinking above each response
/reasoning hide           # Hide model thinking
```

Mudanças de esforço têm escopo de sessão por padrão; adicione `--global` para salvar o
novo nível como seu padrão `agent.reasoning_effort`.

#### Sobrescritas de Raciocínio por Modelo {#per-model-reasoning-overrides}

Você pode definir níveis de esforço de raciocínio diferentes para modelos diferentes. Isso é útil quando você quer raciocínio alto para modelos complexos, mas médio para os mais rápidos:

```yaml
agent:
  reasoning_effort: "medium"       # global default
  reasoning_overrides:
    "openrouter/anthropic/claude-opus-4.5": "xhigh"
    "openai/gpt-5": "low"
    "claude-sonnet-4.6": "high"    # bare model name also works
```

A correspondência de chave é **tolerante à grafia** — qualquer grafia razoável corresponderá:
- `claude-opus-4.5`, `claude-opus-4-5`, `claude-opus.4.5` (pontos e traços são intercambiáveis)
- `anthropic/claude-opus-4.5`, `openrouter/anthropic/claude-opus-4.5` (prefixo de provedor opcional)
- Correspondências exatas têm precedência sobre variantes

:::note
Não há suporte de `hermes config set` para chaves de `reasoning_overrides` — edite o arquivo YAML diretamente. Isso ocorre porque nomes de modelo frequentemente contêm pontos (por exemplo, `claude-opus-4.5`), que conflitam com a sintaxe de chave pontuada da CLI.
:::

**Prioridade de resolução:**

1. Sobrescrita de `/reasoning --session` com escopo de sessão (apenas gateway)
2. Sobrescrita por modelo de `agent.reasoning_overrides` (tolerante à grafia)
3. `agent.reasoning_effort` global
4. Padrão do provedor

A sobrescrita se aplica automaticamente em todo lugar: inicialização da CLI, gateway de mensagens, Desktop/TUI, jobs de cron, trocas de `/model` no meio da sessão e ativação de modelo de fallback.

## Imposição do Uso de Ferramentas {#tool-use-enforcement}

Alguns modelos ocasionalmente descrevem ações pretendidas como texto em vez de fazer chamadas de ferramenta ("Eu executaria os testes..." em vez de realmente chamar o terminal). A imposição do uso de ferramentas injeta orientação no prompt de sistema que direciona o modelo de volta a realmente chamar ferramentas.

```yaml
agent:
  tool_use_enforcement: "auto"   # "auto" | true | false | ["model-substring", ...]
```

| Valor | Comportamento |
|-------|----------|
| `"auto"` (padrão) | Habilitado para modelos que correspondem a: `gpt`, `codex`, `gemini`, `gemma`, `grok`, `glm`, `qwen`, `deepseek`. Desabilitado para todos os outros (por exemplo, Claude). |
| `true` | Sempre habilitado, independentemente do modelo. Útil se você notar que seu modelo atual está descrevendo ações em vez de realizá-las. |
| `false` | Sempre desabilitado, independentemente do modelo. |
| `["gpt", "codex", "qwen", "llama"]` | Habilitado apenas quando o nome do modelo contém uma das substrings listadas (sem diferenciar maiúsculas/minúsculas). |

### O que é injetado {#what-it-injects}

Quando habilitado, três camadas de orientação podem ser adicionadas ao prompt de sistema:

1. **Imposição geral do uso de ferramentas** (todos os modelos correspondentes) — instrui o modelo a fazer chamadas de ferramenta imediatamente em vez de descrever intenções, continuar trabalhando até que a tarefa esteja completa, e nunca terminar um turno com a promessa de uma ação futura.

2. **Disciplina de execução da OpenAI** (modelos GPT, Codex e Grok) — orientação adicional abordando modos de falha específicos do GPT: abandonar o trabalho em resultados parciais, pular buscas de pré-requisitos, alucinar em vez de usar ferramentas, e declarar "concluído" sem verificação.

3. **Orientação operacional do Google** (apenas modelos Gemini e Gemma) — concisão, caminhos absolutos, chamadas de ferramenta em paralelo, e padrões de verificar-antes-de-editar.

Isso é transparente para o usuário e afeta apenas o prompt de sistema. Modelos que já usam ferramentas de forma confiável (como o Claude) não precisam dessa orientação, por isso o `"auto"` os exclui.

### Quando ativar {#when-to-turn-it-on}

Se você estiver usando um modelo que não está na lista automática padrão e notar que ele frequentemente descreve o que *faria* em vez de fazer, defina `tool_use_enforcement: true` ou adicione a substring do modelo à lista:

```yaml
agent:
  tool_use_enforcement: ["gpt", "codex", "gemini", "grok", "my-custom-model"]
```

## Proteções de Loop de Ferramentas {#tool-loop-guardrails}

O Hermes detecta quando o agente está preso em um loop de chamada de ferramenta improdutivo — a mesma chamada de ferramenta falhando repetidamente, a mesma ferramenta falhando repetidamente, ou uma chamada idempotente retornando o mesmo resultado sem progresso. Por padrão, ele injeta um **aviso** no resultado da ferramenta para que o modelo se autocorrija; ele não interrompe forçadamente, já que uma pessoa observando a CLI/TUI pode intervir.

Para implantações não supervisionadas de gateway/servidor, habilite as interrupções forçadas para que um agente travado tenha o circuito interrompido em vez de queimar o orçamento de iteração:

```yaml
tool_loop_guardrails:
  warnings_enabled: true       # inject warnings into tool results (default: true)
  hard_stop_enabled: false     # also BLOCK the call past the hard-stop threshold (default: false)
  warn_after:
    exact_failure: 2           # identical failing call repeated N times
    same_tool_failure: 3       # same tool failing N times (different args)
    idempotent_no_progress: 2  # same result, no progress, N times
  hard_stop_after:
    exact_failure: 5
    same_tool_failure: 8
    idempotent_no_progress: 5
  loop_caps:
    max_web_searches: 50       # max web_search calls per turn (0 = unlimited)
    max_subagents: 50          # max subagents spawned per turn (0 = unlimited)
```

`hard_stop_enabled` assume `false` por padrão porque sessões interativas têm um humano no circuito. Em implantações não supervisionadas (gateway, cron, workers do kanban), defina como `true` para que falhas repetidas sejam bloqueadas em vez de apenas avisadas. Veja também [Docker / implantações não supervisionadas](docker.md).

### Limites de loop descontrolado por turno {#per-turn-runaway-loop-caps}

Separadamente dos limites baseados em falha acima, `loop_caps` define tetos rígidos para quantas chamadas de `web_search` e geração de subagentes um único loop de agente (turno) pode fazer. Os contadores reiniciam no começo de cada turno, então uma sessão legítima com múltiplos turnos nunca é privada — mas um único turno que espirala em uma busca ou delegação ilimitada é interrompido. Esses limites estão sempre ativos e disparam independentemente de `hard_stop_enabled`. Um único turno emitindo dezenas de buscas web ou gerando dezenas de subagentes já é patológico, então os padrões são baixos. Quando um limite é atingido, a chamada de ferramenta ofensora é bloqueada com uma mensagem explicativa e o turno é encerrado de forma limpa, em vez de queimar o resto do orçamento. Defina qualquer valor como `0` para desativar esse limite completamente.

Um único lote de `delegate_task` conta cada tarefa em direção a `max_subagents` (um lote de 3 gasta 3), então o limite rastreia subagentes reais gerados em vez de invocações de `delegate_task`.

Isso espelha os limites por sessão de WebSearch e subagente do Claude Code (v2.1.212), que também assumem 200 como padrão e reiniciam em `/clear`.

## Configuração de TTS {#tts-configuration}

```yaml
tts:
  provider: "edge"              # "edge" | "elevenlabs" | "openai" | "minimax" | "mistral" | "gemini" | "xai" | "neutts" | "kittentts" | "piper" | "deepinfra"
  speed: 1.0                    # Global speed multiplier (fallback for all providers)
  edge:
    voice: "en-US-AriaNeural"   # 322 voices, 74 languages
    speed: 1.0                  # Speed multiplier (converted to rate percentage, e.g. 1.5 → +50%)
  elevenlabs:
    voice_id: "pNInz6obpgDQGcFmaJgB"
    model_id: "eleven_multilingual_v2"
  openai:
    model: "gpt-4o-mini-tts"
    voice: "alloy"              # alloy, echo, fable, onyx, nova, shimmer
    speed: 1.0                  # Speed multiplier (clamped to 0.25–4.0 by the API)
    base_url: "https://api.openai.com/v1"  # Override for OpenAI-compatible TTS endpoints
  minimax:
    speed: 1.0                  # Speech speed multiplier
    # base_url: ""              # Optional: override for OpenAI-compatible TTS endpoints
  mistral:
    model: "voxtral-mini-tts-2603"
    voice_id: "c69964a6-ab8b-4f8a-9465-ec0925096ec8"  # Paul - Neutral (default)
  gemini:
    model: "gemini-2.5-flash-preview-tts"   # or gemini-3.1-flash-tts-preview
    voice: "Kore"               # 30 prebuilt voices: Zephyr, Puck, Kore, Enceladus, etc.
    audio_tags: false           # Hidden Gemini 3.1 TTS audio-tag insertion
    persona_prompt_file: ""      # Optional Markdown/text file with Gemini voice direction
  xai:
    voice_id: "eve"             # xAI TTS voice
    language: "en"              # ISO 639-1
    sample_rate: 24000
    bit_rate: 128000            # MP3 bitrate
    # base_url: "https://api.x.ai/v1"
  neutts:
    ref_audio: ''
    ref_text: ''
    model: neuphonic/neutts-air-q4-gguf
    device: cpu
```

Isso controla tanto a ferramenta `text_to_speech` quanto as respostas faladas no modo de voz (`/voice tts` na CLI ou no gateway de mensagens).

**Hierarquia de fallback de velocidade:** velocidade específica do provedor (por exemplo, `tts.edge.speed`) → `tts.speed` global → padrão `1.0`. Defina o `tts.speed` global para aplicar uma velocidade uniforme em todos os provedores, ou sobrescreva por provedor para controle refinado.

## Configurações de Exibição {#display-settings}

```yaml
display:
  tool_progress: all      # off | new | all | verbose
  tool_progress_command: false  # Enable /verbose slash command in messaging gateway
  focus_view: false       # CLI focus view (/focus) — reduced output, display-only
  platforms: {}           # Per-platform display overrides (see below)
  interim_assistant_messages: true  # Gateway: send natural mid-turn assistant updates as separate messages
  show_commentary: true   # Codex models: deliver commentary-channel progress narration as visible mid-turn updates
  skin: default           # Built-in or custom CLI skin (see user-guide/features/skins)
  personality: ""         # Legacy cosmetic field still surfaced in some summaries
  compact: false          # Compact output mode (less whitespace)
  resume_display: full    # full (show previous messages on resume) | minimal (one-liner only)
  bell_on_complete: false # Play terminal bell when agent finishes (great for long tasks)
  show_reasoning: true    # Show model reasoning/thinking above each response (default: true; toggle with /reasoning show|hide)
  streaming: false        # Stream tokens to terminal as they arrive (real-time output)
  show_cost: false        # Show estimated $ cost in the CLI status bar
  timestamps: false       # When true, prefixes user and assistant labels with timestamps in the CLI / TUI transcript
  timestamp_format: "%H:%M"  # strftime format for those timestamps (e.g. "%b-%d %H:%M" for month-day)
  tool_preview_length: 0  # Max chars for tool call previews (0 = no limit, show full paths/commands)
  turn_summary: true      # CLI only: print a one-line post-turn accounting footer after each interactive turn
  spinner_token_flow: true # CLI only: append live cumulative turn tokens to the spinner timer
  runtime_footer:         # Gateway: append a runtime-context footer to final replies
    enabled: false
    fields: ["model", "context_pct", "cwd"]
  file_mutation_verifier: true    # Append an advisory footer when write_file/patch calls failed this turn
  credits_notices: true   # Nous credits status-bar notices (usage bands, grant-spent, depleted). false = silence them; /usage still works
  language: en            # UI language for static messages (approval prompts, some gateway replies). en | zh | zh-hant | ja | de | es | fr | tr | uk | af | ko | it | ga | pt | ru | hu
```

### Resumo por turno e fluxo de tokens no spinner {#per-turn-summary-and-spinner-token-flow}

`display.turn_summary` (padrão `true`) imprime uma linha discreta de contabilidade após cada turno **interativo da CLI**, resumindo o que aquele turno realmente fez:

```
⋯ 12.4s · edited 2 files +18 -3 · read 4 files · ran 3 commands
```

A contagem é observada a partir do feed de progresso de ferramentas que a CLI já recebe, então não custa nada extra. Detalhes:

- O tempo de relógio é a duração real do turno (`2m05s` a partir da marca de um minuto).
- Chamadas de ferramenta são agrupadas por verbo (`edited`, `read`, `ran`, `searched`, …) com pluralização correta; ferramentas de plugin/MCP sem um verbo curado colapsam em `called N tools`.
- Deltas de linha `+X -Y` aparecem apenas quando o resultado da ferramenta já reporta um diff (atualmente `patch`). O Hermes nunca chama o git para calculá-los, então uma edição via `write_file` é contada sem um delta.
- **Chamadas de ferramenta falhas não são contadas** — uma escrita negada nunca aparece como uma edição bem-sucedida (veja o [verificador de mutação de arquivo](#file-mutation-verifier) para o aviso complementar).
- Turnos longos limitam-se a quatro segmentos de verbo mais uma cauda `+N more`, para que a linha nunca quebre.
- Um turno rápido sem chamadas de ferramenta não imprime nada.

`display.spinner_token_flow` (padrão `true`) anexa os tokens de saída cumulativos do turno em execução ao temporizador ao vivo do spinner da CLI:

```
  ⚡ Reading cli.py  (  2.3s · ↓ 1.2k tok)
```

A contagem é por turno (os totais da sessão são zerados no início do turno) e é atualizada conforme cada chamada de API no turno reporta o uso. Nada é renderizado antes que o primeiro relatório de uso chegue, então você nunca vê um enganoso `↓ 0 tok`.

Ambas as chaves são apenas de exibição e exclusivas da CLI: são suprimidas no modo silencioso, quando `display.tool_progress` é `off`, em execuções de consulta única/lote (`-Q`), e em superfícies de gateway/mensagens (essas usam `display.runtime_footer` em vez disso). Defina qualquer uma das chaves como `false` para desativá-la.

### Verificador de mutação de arquivo {#file-mutation-verifier}

Quando `display.file_mutation_verifier` é `true` (padrão), o Hermes anexa um aviso de uma linha à resposta final do assistente sempre que uma chamada `write_file` ou `patch` falhar durante o turno e nunca for substituída por uma escrita bem-sucedida no mesmo caminho. Isso captura a classe de exagero "lote de patches paralelos, metade falha silenciosamente, o modelo resume sucesso" sem exigir que você execute manualmente `git status` após cada edição.

Exemplo de rodapé:

```
⚠️ File-mutation verifier: 3 file(s) were NOT modified this turn despite any wording above that may suggest otherwise. Run `git status` or `read_file` to confirm.
  • concepts/automatic-organization.md — [patch] Could not find match for old_string
  • concepts/lora.md — [patch] Could not find match for old_string
  • concepts/rag-pipeline.md — [patch] Could not find match for old_string
```

Defina `file_mutation_verifier: false` (ou `HERMES_FILE_MUTATION_VERIFIER=0`) para suprimir o rodapé. O verificador só dispara quando falhas reais permanecem pendentes no fim do turno — um modelo que tenta novamente um patch falho e tem sucesso dentro do mesmo turno não o disparará para aquele arquivo.

**Confie no verificador em vez do resumo do modelo.** O rodapé significa que os arquivos listados **não** foram modificados em disco, mesmo que a mensagem de encerramento do assistente diga que a tarefa está concluída. Causas comuns:

- **Escrita negada** — o caminho está na lista de bloqueio de credenciais ou fora de `HERMES_WRITE_SAFE_ROOT` (veja [Segurança de escrita de arquivo](./security.md#file-write-safety))
- **Incompatibilidade de patch** — `old_string` não correspondeu ao arquivo em disco
- **Barreira de sintaxe** — o conteúdo candidato falhou na validação JSON/YAML/TOML antes da escrita

Exemplo de rodapé quando escritas são bloqueadas:

```
⚠️ File-mutation verifier: 2 file(s) were NOT modified this turn despite any wording above that may suggest otherwise. Run `git status` or `read_file` to confirm.
  • ~/.hermes/cron/jobs.json — [patch] Write denied: '…' is outside HERMES_WRITE_SAFE_ROOT (/path/to/project)
  • ~/.hermes/scripts/monitor.py — [write_file] Write denied: '…' is outside HERMES_WRITE_SAFE_ROOT (/path/to/project)
```

Se escritas no estado do Hermes (jobs de cron, skills, scripts sob `~/.hermes/`) estiverem falhando, verifique se `HERMES_WRITE_SAFE_ROOT` está definido no seu ambiente. Para mudanças de cron, use a ferramenta `cronjob` ou `hermes cron edit` em vez de aplicar patch em `jobs.json` diretamente.

### Idioma de UI para mensagens estáticas {#ui-language-for-static-messages}

A configuração `display.language` traduz um pequeno conjunto de mensagens estáticas voltadas ao usuário — o prompt de aprovação da CLI, algumas respostas de comando de barra do gateway (por exemplo, avisos de drenagem de reinício, "aprovação expirada", "objetivo limpo"). Ela **não** traduz respostas do agente, linhas de log, saída de ferramentas, tracebacks de erro ou descrições de comandos de barra — esses permanecem em inglês. Se você quiser que o próprio agente responda em outro idioma, basta dizer isso no seu prompt ou mensagem de sistema.

Valores suportados: `en` (padrão), `zh` (chinês simplificado), `zh-hant` (chinês tradicional), `ja` (japonês), `de` (alemão), `es` (espanhol), `fr` (francês), `tr` (turco), `uk` (ucraniano), `af` (africâner), `ko` (coreano), `it` (italiano), `ga` (irlandês), `pt` (português), `ru` (russo), `hu` (húngaro). Valores desconhecidos recorrem ao inglês.

Você também pode definir isso por sessão com a variável de ambiente `HERMES_LANGUAGE`, que sobrescreve o valor de configuração.

```yaml
display:
  language: zh   # CLI approval prompts appear in Chinese
```

| Modo | O que você vê |
|------|-------------|
| `off` | Silencioso — apenas a resposta final |
| `new` | Indicador de ferramenta apenas quando a ferramenta muda |
| `all` | Cada chamada de ferramenta com uma prévia curta (padrão) |
| `verbose` | Argumentos completos, resultados e logs de depuração |

Na CLI, alterne entre esses modos com `/verbose`. Para usar `/verbose` em plataformas de mensagens (Telegram, Discord, Slack, etc.), defina `tool_progress_command: true` na seção `display` acima. O comando então alternará o modo e salvará na configuração.

O progresso de ferramentas requer um adaptador de gateway que possa exibir atualizações de progresso com segurança. Plataformas sem suporte de edição de mensagem, incluindo Signal, suprimem as bolhas de progresso de ferramenta mesmo que `/verbose` salve um modo diferente de `off`.

### Visão focada (`/focus`, CLI + TUI) {#focus-view-focus-cli--tui}

`display.focus_view: true` habilita a **visão focada** — um modo de exibição de saída reduzida para quando você quer a resposta, não a narração passo a passo. É uma camada fina sobre o mesmo mecanismo `tool_progress`, em vez de um segundo caminho de supressão:

- ativá-la fixa `tool_progress` em `off` e guarda seu modo anterior em `display.focus_saved_tool_progress`;
- `/focus off` restaura esse modo exatamente, então uma configuração `/verbose verbose` sobrevive a uma ida e volta;
- cada turno concluído termina com uma linha discreta de recuperação — `⋯ 7 tool lines hidden · /focus off to show` — contada em relação ao seu modo *pré-focus*, então nunca alega ter escondido linhas que você já havia desativado;
- um selo persistente `◉ focus` fica na barra de status (tanto na CLI prompt_toolkit quanto no TUI Ink), então o modo reduzido nunca é invisível;
- alternar `/verbose` enquanto o foco está ativo devolve o modo ao `/verbose` e limpa o selo.

A visão focada é **apenas de exibição**. Ela nunca edita o histórico de conversa, o prompt de sistema, os esquemas de ferramenta, ou qualquer payload de requisição — o detalhe escondido é suprimido na tela, nunca descartado, e o cache de prompt é completamente inafetado.

### Rodapé de metadados de runtime (apenas gateway) {#runtime-metadata-footer-gateway-only}

Quando `display.runtime_footer.enabled: true`, o Hermes anexa um pequeno rodapé de contexto de runtime à mensagem **final** de cada turno do gateway. O rodapé atual pode mostrar o modelo, a porcentagem da janela de contexto e o diretório de trabalho atual. Desativado por padrão; ative por gateway se sua equipe quiser que toda resposta inclua essa proveniência.

```yaml
display:
  runtime_footer:
    enabled: true
    fields: ["model", "context_pct", "cwd"]   # supported fields: model, context_pct, cwd
```

O comando de barra `/footer` alterna isso em tempo de execução em qualquer sessão.

Exemplo de rodapé anexado a uma resposta do Telegram/Discord/Slack:

```
— claude-opus-4.7 · 12 tool calls · 2m 14s · $0.042
```

Apenas a mensagem **final** de um turno recebe o rodapé; atualizações intermediárias permanecem limpas.

### Sobrescritas de progresso por plataforma {#per-platform-progress-overrides}

Plataformas diferentes têm necessidades de verbosidade diferentes. Use `display.platforms` para definir modos por plataforma:

```yaml
display:
  tool_progress: all          # global default
  platforms:
    signal:
      tool_progress: 'off'    # Signal cannot currently display tool-progress bubbles
    telegram:
      tool_progress: verbose  # detailed progress on Telegram
    slack:
      tool_progress: 'off'    # quiet in shared Slack workspace
```

Plataformas sem uma sobrescrita recorrem ao valor global de `tool_progress`. Chaves de plataforma válidas: `telegram`, `discord`, `slack`, `signal`, `whatsapp`, `matrix`, `mattermost`, `email`, `sms`, `homeassistant`, `dingtalk`, `feishu`, `wecom`, `weixin`, `bluebubbles`, `qqbot`. A chave legada `display.tool_progress_overrides` ainda carrega para retrocompatibilidade, mas está descontinuada e é migrada para `display.platforms` no primeiro carregamento.

Signal é listado como uma chave de plataforma válida porque a configuração pode ser salva por plataforma, mas o adaptador atual do Signal não pode editar mensagens enviadas e não renderiza bolhas de progresso de ferramenta. Mantenha `tool_progress` do Signal definido como `off`; use a CLI ou uma plataforma de mensagens com capacidade de edição se precisar observar cada chamada de ferramenta ao vivo.

`interim_assistant_messages` é exclusivo do gateway. Quando habilitado, o Hermes envia atualizações intermediárias completas do assistente como mensagens de chat separadas. Isso é independente do `tool_progress` e não requer streaming do gateway.

`show_commentary` (padrão `true`) controla o canal de comentários dos modelos Codex Responses — a narração de progresso polida que esses modelos produzem junto com seu raciocínio privado. Quando habilitado, cada mensagem de comentário concluída é entregue como uma atualização intermediária visível (no gateway isso também requer `interim_assistant_messages`). Defina como `false` se a narração extra incomodar: os comentários então recorrem ao canal de raciocínio e só são mostrados quando `show_reasoning` está habilitado.

## Privacidade {#privacy}

```yaml
privacy:
  redact_pii: false  # Strip PII from LLM context (gateway only)
```

Quando `redact_pii` é `true`, o gateway remove informações de identificação pessoal do prompt de sistema antes de enviá-lo ao LLM em plataformas suportadas:

| Campo | Tratamento |
|-------|-----------|
| Números de telefone (ID de usuário no WhatsApp/Signal) | Hash para `user_<12-char-sha256>` |
| IDs de usuário | Hash para `user_<12-char-sha256>` |
| IDs de chat | Parte numérica com hash, prefixo de plataforma preservado (`telegram:<hash>`) |
| IDs de canal doméstico | Parte numérica com hash |
| Nomes de usuário / usernames | **Não afetados** (escolhidos pelo usuário, publicamente visíveis) |

**Suporte por plataforma:** A redação se aplica a WhatsApp, Signal e Telegram. Discord e Slack são excluídos porque seus sistemas de menção (`<@user_id>`) exigem o ID real no contexto do LLM.

Os hashes são determinísticos — o mesmo usuário sempre mapeia para o mesmo hash, então o modelo ainda pode distinguir entre usuários em chats em grupo. Roteamento e entrega usam os valores originais internamente.

## Fala-para-Texto (STT) {#speech-to-text-stt}

```yaml
stt:
  enabled: true                # Auto-transcribe inbound voice messages (default: true)
  echo_transcripts: true       # Post raw transcripts back to the chat as 🎙️ "..." (default: true)
  provider: "local"            # "local" | "groq" | "openai" | "mistral" | "xai" | "elevenlabs" | "deepinfra" | ...
  language: "en"               # GLOBAL language hint for every provider (per-provider language wins); set "" for auto-detect
  local:
    model: "base"              # tiny, base, small, medium, large-v3
    language: ""               # per-provider override of stt.language
    initial_prompt: ""         # optional whisper prompt to bias vocabulary/script (e.g. Simplified Chinese)
    vad: true                  # Silero VAD filter (default on) — silence never reaches whisper; false = raw behavior (music/ambient)
    vad_min_silence_ms: 500    # min silence (ms) that splits speech chunks when vad is on
    no_speech_prob_threshold: 0.6  # drop a segment only when no_speech_prob > this...
    logprob_threshold: -1.0        # ...AND avg_logprob < this (both must hit — quiet real speech survives)
  groq:
    language: ""               # per-provider override of stt.language
  openai:
    model: "whisper-1"         # whisper-1 | gpt-4o-mini-transcribe | gpt-4o-transcribe | gpt-transcribe
    language: ""               # per-provider override of stt.language
  # model: "whisper-1"         # Legacy fallback key still respected
```

A resolução de idioma é a mesma para **todo** provedor STT (local, groq, openai, mistral, xai, elevenlabs, deepinfra, provedores de comando e plugins): `stt.<provider>.language` → `stt.language` → variável de ambiente `HERMES_LOCAL_STT_LANGUAGE` → detecção automática do provedor. **O padrão é `stt.language: "en"`** — a detecção automática do Whisper frequentemente identifica incorretamente clipes curtos ou com sotaque, o que aparece como notas de voz transcritas no idioma errado. Falantes de outros idiomas além do inglês devem definir `stt.language` para seu código de idioma uma vez (por exemplo, `"es"`, `"zh"`, `"uk"`); defina como `""` para restaurar a detecção automática para uso multilíngue.

Defina `stt.echo_transcripts: false` quando o gateway deve transcrever notas de voz para o agente, mas não deve postar a transcrição bruta de volta no chat (por exemplo, bots de WhatsApp voltados ao cliente).

Comportamento do provedor:

- `local` usa `faster-whisper` rodando na sua máquina. Instale-o separadamente com `pip install faster-whisper`. O reforço contra alucinação de silêncio está ativado por padrão: um filtro Silero VAD impede que silêncio/ruído chegue ao Whisper, o condicionamento entre janelas é desativado, e segmentos que o próprio modelo sinaliza como provavelmente-não-fala *e* de baixa confiança são descartados. Defina `stt.local.vad: false` para transcrever áudio que não seja fala (música, ambiente) com o comportamento bruto.
- `groq` usa o endpoint compatível com Whisper do Groq e lê `GROQ_API_KEY`. Passe `stt.groq.language` (ou a variável de ambiente global `HERMES_LOCAL_STT_LANGUAGE`) para pular a detecção automática e reduzir a latência.
- `openai` usa a API de fala da OpenAI e lê `VOICE_TOOLS_OPENAI_KEY`.

Se o provedor solicitado não estiver disponível, o Hermes recorre automaticamente nesta ordem: `local` → `groq` → `openai`.

Sobrescritas de modelo do Groq e OpenAI são orientadas por variável de ambiente:

```bash
STT_GROQ_MODEL=whisper-large-v3-turbo
STT_OPENAI_MODEL=whisper-1
GROQ_BASE_URL=https://api.groq.com/openai/v1
STT_OPENAI_BASE_URL=https://api.openai.com/v1
```

## Modo de Voz (CLI) {#voice-mode-cli}

```yaml
voice:
  record_key: "ctrl+b"         # Push-to-talk key inside the CLI
  max_recording_seconds: 120    # Hard stop for long recordings
  auto_tts: false               # Enable spoken replies automatically when /voice on
  beep_enabled: true            # Play record start/stop beeps in CLI voice mode
  beep_volume: 0.3              # Beep amplitude (0.0-1.0); raise it on quiet systems / headphones
  silence_threshold: 200        # RMS threshold for speech detection
  silence_duration: 3.0         # Seconds of silence before auto-stop
```

Use `/voice on` na CLI para habilitar o modo de microfone, `record_key` para iniciar/parar a gravação, e `/voice tts` para alternar respostas faladas. Veja [Voice Mode](/user-guide/features/voice-mode) para a configuração completa e o comportamento específico por plataforma.

## Streaming {#streaming}

Faça streaming de tokens para o terminal ou plataformas de mensagens conforme chegam, em vez de esperar pela resposta completa.

### Streaming na CLI {#cli-streaming}

```yaml
display:
  streaming: true         # Stream tokens to terminal in real-time
  show_reasoning: true    # Also stream reasoning/thinking tokens (optional)
```

Quando habilitado, as respostas aparecem token por token dentro de uma caixa de streaming. Chamadas de ferramenta ainda são capturadas silenciosamente. Se o provedor não suportar streaming, recorre automaticamente à exibição normal.

### Streaming do Gateway (Telegram, Discord, Slack) {#gateway-streaming-telegram-discord-slack}

```yaml
streaming:
  enabled: true           # Enable progressive message editing (default: false)
  transport: auto         # "auto" (default) | "edit" (progressive message editing) | "off"
  edit_interval: 0.8      # Seconds between message edits (default: 0.8)
  buffer_threshold: 24    # Characters before forcing an edit flush (default: 24)
  cursor: " ▉"            # Cursor shown during streaming
  fresh_final_after_seconds: 0    # Opt in to fresh final (Telegram) when preview is this old
```

Quando habilitado, o bot envia uma mensagem no primeiro token, depois a edita progressivamente conforme mais tokens chegam. Plataformas que não suportam edição de mensagem (Signal, Email, Home Assistant) são detectadas automaticamente na primeira tentativa — o streaming é desativado graciosamente para essa sessão sem uma enxurrada de mensagens.

Para atualizações naturais e separadas do assistente no meio do turno sem edição progressiva de token, defina `display.interim_assistant_messages: true`.

**Tratamento de overflow:** Se o texto em streaming exceder o limite de comprimento de mensagem da plataforma (~4096 caracteres), a mensagem atual é finalizada e uma nova começa automaticamente.

**Final novo (Telegram):** O `editMessageText` do Telegram preserva o timestamp original da mensagem, então uma resposta em streaming de longa duração manteria o timestamp do primeiro token mesmo após a conclusão. Defina `fresh_final_after_seconds > 0` para optar por entregar prévias antigas como mensagens finais totalmente novas, com exclusão de prévia em melhor esforço. O padrão é `0`, que sempre finaliza respostas em streaming no lugar e evita a breve sequência de mensagem duplicada/exclusão em clientes que mostram ambas as operações.

:::note Padrões de streaming por plataforma
O interruptor mestre `streaming.enabled` é `false` por padrão — nada faz streaming até você ativá-lo. Uma vez habilitado, o streaming é decidido **por plataforma**: o Telegram vem com `display.platforms.telegram.streaming: true` (faz streaming) e o Discord com `display.platforms.discord.streaming: false` (não faz). Então, depois de habilitar o streaming, o Telegram faz streaming imediatamente e o Discord permanece com respostas de mensagem completa até você mudar seu ajuste. Você pode ajustar esses interruptores por plataforma nos toggles de **Channels** do dashboard ou diretamente em `~/.hermes/config.yaml`.
:::

## Isolamento de Sessão em Chat em Grupo {#group-chat-session-isolation}

Limite quantas sessões de chat podem estar ativas ao mesmo tempo entre CLI, TUI/dashboard,
e gateway de mensagens:

```yaml
max_concurrent_sessions: null  # null/0 = unlimited; positive integer = active session cap
```

Um slot é ocupado quando uma sessão executa seu **primeiro turno**, não quando uma janela de chat
é aberta. Abrir, retomar ou reconectar a um chat não custa nada até você
enviar uma mensagem, então abas de desktop ociosas (e as retomadas em segundo plano que um websocket
instável dispara) não podem privar o gateway de mensagens que compartilha esse limite.

Quando o limite é atingido, o Hermes retorna uma mensagem direta de limite nomeando quais
superfícies ocupam os slots. Sessões ativas existentes mantêm seu comportamento normal.
Execute `hermes status` para ver o uso atual de slots e cada detentor.

A chave canônica é `max_concurrent_sessions` de nível superior. O Hermes também aceita
`gateway.max_concurrent_sessions` como alternativa, mas a chave de nível superior prevalece quando
ambas estão definidas.

O limite é imposto com um arquivo de concessão de runtime local e é de melhor esforço: o Hermes
falha de forma aberta se o registro não puder ser lido ou bloqueado, para que os usuários não fiquem encalhados.
Destina-se a um runtime único de host/perfil, não a um `$HERMES_HOME` compartilhado
montado em múltiplas máquinas.

Controle se chats compartilhados mantêm uma conversa por sala ou uma conversa por participante:

```yaml
group_sessions_per_user: true  # true = per-user isolation in groups/channels, false = one shared session per chat
```

- `true` é a configuração padrão e recomendada. Em canais do Discord, grupos do Telegram, canais do Slack e contextos compartilhados similares, cada remetente recebe sua própria sessão quando a plataforma fornece um ID de usuário.
- `false` reverte ao comportamento antigo de sala compartilhada. Isso pode ser útil se você explicitamente quiser que o Hermes trate um canal como uma conversa colaborativa única, mas também significa que os usuários compartilham contexto, custos de token e estado de interrupção.
- Mensagens diretas não são afetadas. O Hermes ainda chaveia DMs pelo ID de chat/DM como de costume.
- Threads permanecem isoladas do seu canal pai de qualquer forma; com `true`, cada participante também recebe sua própria sessão dentro da thread.

Para os detalhes do comportamento e exemplos, veja [Sessions](/user-guide/sessions) e o [guia do Discord](/user-guide/messaging/discord).

## Comportamento de DM Não Autorizada {#unauthorized-dm-behavior}

Controle o que o Hermes faz quando um usuário desconhecido envia uma mensagem direta:

```yaml
unauthorized_dm_behavior: pair

whatsapp:
  unauthorized_dm_behavior: ignore
```

- `pair` é o padrão para plataformas de DM no estilo chat. O Hermes nega o acesso, mas responde com um código de pareamento único nas DMs.
- `ignore` descarta silenciosamente DMs não autorizadas.
- O Email assume `ignore` por padrão, a menos que `platforms.email.unauthorized_dm_behavior: pair` esteja definido, porque caixas de entrada podem conter emails não relacionados não lidos.
- Seções de plataforma sobrescrevem o padrão global, então você pode manter o pareamento habilitado amplamente enquanto torna uma plataforma mais silenciosa.

## Comandos Rápidos {#quick-commands}

Defina comandos personalizados que executam comandos de shell sem invocar o LLM, ou que aliasam um comando de barra para outro. Comandos rápidos do tipo exec custam zero tokens e são úteis a partir de plataformas de mensagens (Telegram, Discord, etc.) para verificações rápidas de servidor ou scripts utilitários.

```yaml
quick_commands:
  status:
    type: exec
    command: systemctl status hermes-agent
  disk:
    type: exec
    command: df -h /
  update:
    type: exec
    command: cd ~/.hermes/hermes-agent && git pull && uv pip install -e .
  gpu:
    type: exec
    command: nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader
  restart:
    type: alias
    target: /gateway restart
```

Uso: digite `/status`, `/disk`, `/update`, `/gpu`, ou `/restart` na CLI ou em qualquer plataforma de mensagens. Comandos `exec` rodam localmente no host e retornam a saída diretamente — nenhuma chamada de LLM, nenhum token consumido. Comandos `alias` reescrevem para o alvo de comando de barra configurado.

- **Timeout de 30 segundos** — comandos de longa duração são interrompidos com uma mensagem de erro
- **Prioridade** — comandos rápidos são verificados antes dos comandos de skill, então você pode sobrescrever nomes de skill
- **Autocompletar** — comandos rápidos são resolvidos no momento do despacho e não são mostrados nas tabelas de autocompletar de comando de barra embutidas
- **Tipo** — os tipos suportados são `exec` e `alias`; outros tipos mostram um erro
- **Funciona em todo lugar** — CLI, Telegram, Discord, Slack, WhatsApp, Signal, Email, Home Assistant

Atalhos de prompt apenas em string não são comandos rápidos válidos. Para fluxos de trabalho de prompt reutilizáveis, crie uma skill ou um alias para um comando de barra existente.

## Atraso Humano {#human-delay}

Simule o ritmo de resposta parecido com humano em plataformas de mensagens:

```yaml
human_delay:
  mode: "off"                  # off | natural | custom
  min_ms: 800                  # Minimum delay (custom mode)
  max_ms: 2500                 # Maximum delay (custom mode)
```

## Execução de Código {#code-execution}

Configure a ferramenta `execute_code`:

```yaml
code_execution:
  mode: project                # project (default) | strict
  timeout: 300                 # Max execution time in seconds
  max_tool_calls: 50           # Max tool calls within code execution
```

**`mode`** controla o diretório de trabalho e o interpretador Python para scripts:

- **`project`** (padrão) — scripts rodam no diretório de trabalho da sessão com o python do ambiente virtual/conda ativo. Dependências do projeto (`pandas`, `torch`, pacotes do projeto) e caminhos relativos (`.env`, `./data.csv`) são resolvidos naturalmente, correspondendo ao que `terminal()` vê.
- **`strict`** — scripts rodam em um diretório temporário de staging com `sys.executable` (o próprio python do Hermes). Reprodutibilidade máxima, mas dependências do projeto e caminhos relativos não serão resolvidos.

A limpeza de ambiente (remove `*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_CREDENTIAL`, `*_PASSWD`, `*_AUTH`) e a lista branca de ferramentas se aplicam identicamente em ambos os modos — mudar de modo não altera a postura de segurança.

## Backends de Busca Web {#web-search-backends}

As ferramentas `web_search` e `web_extract` suportam cinco provedores de backend. Configure o backend em `config.yaml` ou via `hermes tools`:

```yaml
web:
  backend: firecrawl    # firecrawl | searxng | parallel | tavily | exa

  # Or use per-capability keys to mix providers (e.g. free search + paid extract):
  search_backend: "searxng"
  extract_backend: "firecrawl"
```

| Backend | Variável de Ambiente | Busca | Extração |
|---------|---------|--------|---------|
| **Firecrawl** (padrão) | `FIRECRAWL_API_KEY` | ✔ | ✔ |
| **SearXNG** | `SEARXNG_URL` | ✔ | — |
| **Parallel** | `PARALLEL_API_KEY` | ✔ | ✔ |
| **Tavily** | `TAVILY_API_KEY` | ✔ | ✔ |
| **Exa** | `EXA_API_KEY` | ✔ | ✔ |

**Seleção de backend:** Se `web.backend` não estiver definido, o backend é detectado automaticamente a partir das chaves de API disponíveis. Se apenas `SEARXNG_URL` estiver definido, o SearXNG é usado. Se apenas `EXA_API_KEY` estiver definido, o Exa é usado. Se apenas `TAVILY_API_KEY` estiver definido, o Tavily é usado. Se apenas `PARALLEL_API_KEY` estiver definido, o Parallel é usado. Caso contrário, o Firecrawl é o padrão.

**SearXNG** é um motor de metabusca gratuito, auto-hospedado e que respeita a privacidade, que consulta mais de 70 motores de busca. Nenhuma chave de API necessária — apenas defina `SEARXNG_URL` para sua instância (por exemplo, `http://localhost:8080`). O SearXNG é apenas para busca; `web_extract` requer um provedor de extração separado (defina `web.extract_backend`). Veja o [guia de configuração de Web Search](/user-guide/features/web-search) para instruções de configuração com Docker.

**Firecrawl auto-hospedado:** Defina `FIRECRAWL_API_URL` para apontar para sua própria instância. Quando uma URL personalizada está definida, a chave de API se torna opcional (defina `USE_DB_AUTHENTICATION=*** no servidor para desativar a autenticação).

**Modos de busca do Parallel:** Defina `PARALLEL_SEARCH_MODE` para controlar o comportamento de busca — `fast`, `one-shot`, ou `agentic` (padrão: `agentic`).

**Exa:** Defina `EXA_API_KEY` em `~/.hermes/.env`. Suporta filtragem por `category` (`company`, `research paper`, `news`, `people`, `personal site`, `pdf`) e filtros de domínio/data.

## Navegador {#browser}

Configure o comportamento de automação do navegador:

```yaml
browser:
  inactivity_timeout: 120        # Seconds before auto-closing idle sessions
  command_timeout: 30             # Timeout in seconds for browser commands (screenshot, navigate, etc.)
  record_sessions: false         # Auto-record browser sessions as WebM videos to ~/.hermes/browser_recordings/
  # Optional CDP override — when set, Hermes attaches directly to your own
  # Chromium-family browser (via /browser connect) rather than starting a headless browser.
  cdp_url: ""
  # Dialog supervisor — controls how native JS dialogs (alert / confirm / prompt)
  # are handled when a CDP backend is attached (Browserbase, local Chromium-family
  # browser via /browser connect). Ignored on Camofox and default local agent-browser mode.
  dialog_policy: must_respond    # must_respond | auto_dismiss | auto_accept
  dialog_timeout_s: 300          # Safety auto-dismiss under must_respond (seconds)
  camofox:
    managed_persistence: false   # When true, Camofox sessions persist cookies/logins across restarts
    user_id: ""                  # Optional externally managed Camofox userId
    session_key: ""              # Optional session key sent when Hermes creates a tab
    adopt_existing_tab: false    # Reuse an existing tab for this identity before creating one
```

**Políticas de diálogo:**

- `must_respond` (padrão) — captura o diálogo, expõe-o em `browser_snapshot.pending_dialogs`, e espera o agente chamar `browser_dialog(action=...)`. Após `dialog_timeout_s` segundos sem resposta, o diálogo é dispensado automaticamente para evitar que a thread JS da página trave para sempre.
- `auto_dismiss` — captura, dispensa imediatamente. O agente ainda vê o registro do diálogo em `browser_snapshot.recent_dialogs` com `closed_by="auto_policy"` posteriormente.
- `auto_accept` — captura, aceita imediatamente. Útil para páginas com prompts agressivos de `beforeunload`.

Veja a [página de recursos do navegador](./features/browser.md#browser_dialog) para o fluxo de trabalho completo do diálogo.

O conjunto de ferramentas do navegador suporta múltiplos provedores. Veja a [página de recursos do Browser](/user-guide/features/browser) para detalhes sobre Browserbase, Browser Use, e configuração local de CDP da família Chromium.

## Fuso Horário {#timezone}

Sobrescreve o fuso horário local do servidor com uma string de fuso horário IANA. Afeta timestamps em logs, agendamento de cron e injeção de horário do prompt de sistema.

```yaml
timezone: "America/New_York"   # IANA timezone (default: "" = server-local time)
```

Valores suportados: qualquer identificador de fuso horário IANA (por exemplo, `America/New_York`, `Europe/London`, `Asia/Kolkata`, `UTC`). Deixe vazio ou omita para o horário local do servidor.

## Discord {#discord}

Configure o comportamento específico do Discord para o gateway de mensagens:

```yaml
discord:
  require_mention: true          # Require @mention to respond in server channels
  free_response_channels: ""     # Comma-separated channel IDs where bot responds without @mention
  auto_thread: true              # Auto-create threads on @mention in channels
```

- `require_mention` — quando `true` (padrão), o bot só responde em canais de servidor quando mencionado com `@BotName`. DMs sempre funcionam sem menção.
- `free_response_channels` — lista separada por vírgulas de IDs de canal onde o bot responde a toda mensagem sem exigir uma menção.
- `auto_thread` — quando `true` (padrão), menções em canais criam automaticamente uma thread para a conversa, mantendo os canais limpos (semelhante ao threading do Slack).

## Segurança {#security}

Varredura de segurança pré-execução e redação de segredos:

```yaml
security:
  redact_secrets: true           # Redact API key patterns in tool output and logs (on by default)
  tirith_enabled: true           # Enable Tirith security scanning for terminal commands
  tirith_path: "tirith"          # Path to tirith binary (default: "tirith" in $PATH)
  tirith_timeout: 5              # Seconds to wait for tirith scan before timing out
  tirith_fail_open: true         # Allow command execution if tirith is unavailable
  website_blocklist:             # See Website Blocklist section below
    enabled: false
    domains: []
    shared_files: []
```

- `redact_secrets` — quando `true`, detecta e redige automaticamente padrões que parecem chaves de API, tokens e senhas na saída de ferramentas antes que entre no contexto da conversa e nos logs. **Ativado por padrão**. Defina como `false` explicitamente apenas quando você precisar de strings brutas parecidas com credenciais para depuração ou desenvolvimento de redator.
- `tirith_enabled` — quando `true`, comandos de terminal são escaneados pelo [Tirith](https://github.com/sheeki03/tirith) antes da execução para detectar operações potencialmente perigosas.
- `tirith_path` — caminho para o binário tirith. Defina isso se o tirith estiver instalado em um local não padrão.
- `tirith_timeout` — segundos máximos para esperar por uma varredura do tirith. Os comandos prosseguem se a varredura expirar.
- `tirith_fail_open` — quando `true` (padrão), os comandos podem ser executados se o tirith estiver indisponível ou falhar. Defina como `false` para bloquear comandos quando o tirith não puder verificá-los.

## Lista de Bloqueio de Sites {#website-blocklist}

Bloqueie domínios específicos de serem acessados pelas ferramentas web e de navegador do agente:

```yaml
security:
  website_blocklist:
    enabled: false               # Enable URL blocking (default: false)
    domains:                     # List of blocked domain patterns
      - "*.internal.company.com"
      - "admin.example.com"
      - "*.local"
    shared_files:                # Load additional rules from external files
      - "/etc/hermes/blocked-sites.txt"
```

Quando habilitado, qualquer URL que corresponda a um padrão de domínio bloqueado é rejeitada antes que a ferramenta web ou de navegador seja executada. Isso se aplica a `web_search`, `web_extract`, `browser_navigate`, e qualquer ferramenta que acesse URLs.

Regras de domínio suportam:
- Domínios exatos: `admin.example.com`
- Subdomínios curinga: `*.internal.company.com` (bloqueia todos os subdomínios)
- Curingas de TLD: `*.local`

Arquivos compartilhados contêm uma regra de domínio por linha (linhas em branco e comentários `#` são ignorados). Arquivos ausentes ou ilegíveis registram um aviso, mas não desativam outras ferramentas web.

A política é armazenada em cache por 30 segundos, então mudanças na configuração têm efeito rapidamente sem reinicialização.

## Aprovações Inteligentes {#smart-approvals}

Controle como o Hermes lida com comandos potencialmente perigosos:

```yaml
approvals:
  mode: smart   # smart | manual | off
```

| Modo | Comportamento |
|------|----------|
| `smart` (padrão) | Usa um LLM auxiliar para avaliar se um comando sinalizado é realmente perigoso. Comandos de baixo risco são aprovados automaticamente apenas para esse comando. Comandos genuinamente arriscados são negados; decisões incertas são escaladas ao usuário. |
| `manual` | Pergunta ao usuário antes de executar qualquer comando sinalizado. Na CLI, mostra uma caixa de diálogo de aprovação interativa. Em mensagens, enfileira uma solicitação de aprovação pendente. |
| `off` | Pula todas as verificações de aprovação. Equivalente a `HERMES_YOLO_MODE=true`. **Use com cautela.** |

O modo smart é particularmente útil para reduzir a fadiga de aprovação — permite que o agente trabalhe de forma mais autônoma em operações seguras, ao mesmo tempo em que ainda captura comandos genuinamente destrutivos.

:::warning
Definir `approvals.mode: off` desativa todas as verificações de segurança para comandos de terminal. Use isso apenas em ambientes confiáveis e isolados (sandboxed).
:::

### Disjuntor de negações {#denial-circuit-breaker}

`approvals.denial_breaker_threshold` (padrão `3`) protege contra o agente tentando novamente variações de um comando que o revisor de aprovação inteligente continua negando — cada nova tentativa consome outra chamada de LLM guardiã. Após esse número de negações consecutivas em uma sessão, a mensagem de negação escala para uma instrução de interrupção forçada dizendo ao agente para parar, relatar a operação bloqueada, e pedir para você executá-la manualmente ou usar `/approve`. Qualquer aprovação reinicia a contagem; defina `0` para desativar:

```yaml
approvals:
  denial_breaker_threshold: 3   # 0 disables the breaker
```

### Regras de negação {#deny-rules}

`approvals.deny` é uma lista de padrões glob que bloqueiam comandos de terminal correspondentes incondicionalmente — mesmo sob `--yolo`, `/yolo`, ou `mode: off`. É a contraparte editável pelo usuário da lista de bloqueio embutida (hardline):

```yaml
approvals:
  deny:
    - "git push --force*"
    - "*curl*|*sh*"
```

Os padrões são globs fnmatch que não diferenciam maiúsculas de minúsculas e devem ser citados em YAML (um `*` inicial sem aspas é um erro de análise). Veja [Security — User-Defined Deny Rules](/user-guide/security#user-defined-deny-rules-approvalsdeny) para detalhes.

### Política personalizada de aprovação inteligente {#custom-smart-approval-policy}

`approvals.smart_policy` permite anexar suas próprias regras às instruções do revisor de aprovação inteligente. Quando definido, o texto é adicionado ao prompt de sistema do LLM guardião (o canal confiável — nunca junto com o texto de comando não confiável), então você pode apertar ou relaxar seu julgamento para o seu ambiente sem editar código:

```yaml
approvals:
  smart_policy: |
    Always ESCALATE commands that modify anything under /etc.
    APPROVE docker compose restarts in ~/deploys — they are routine here.
```


## Checkpoints {#checkpoints}

Snapshots automáticos do sistema de arquivos antes de operações destrutivas de arquivo. Veja [Checkpoints & Rollback](/user-guide/checkpoints-and-rollback) para detalhes.

```yaml
checkpoints:
  enabled: false                 # Enable automatic checkpoints (also: hermes chat --checkpoints). Default: false (opt-in).
  max_snapshots: 20              # Max checkpoints to keep per directory (default: 20)
```


## Delegação {#delegation}

Configure o comportamento de subagente para a ferramenta de delegação:

```yaml
delegation:
  # model: "google/gemini-3-flash-preview"  # Override model (empty = inherit parent)
  # provider: "openrouter"                  # Override provider (empty = inherit parent)
  # base_url: "http://localhost:1234/v1"    # Direct OpenAI-compatible endpoint (takes precedence over provider)
  # api_key: "local-key"                    # API key for base_url (falls back to OPENAI_API_KEY)
  # api_mode: ""                            # Wire protocol for base_url: "chat_completions", "codex_responses", or "anthropic_messages". Empty = auto-detect from URL (e.g. /anthropic suffix → anthropic_messages). Set explicitly for non-standard endpoints the heuristic can't detect.
  max_concurrent_children: 3                # Parallel children per batch (floor 1, no ceiling). Also via DELEGATION_MAX_CONCURRENT_CHILDREN env var.
  max_spawn_depth: 1                        # Delegation tree depth cap (1-3, clamped). 1 = flat (default): parent spawns leaves that cannot delegate. 2 = orchestrator children can spawn leaf grandchildren. 3 = three levels.
  orchestrator_enabled: true                # Global kill switch. When false, role="orchestrator" is ignored and every child is forced to leaf regardless of max_spawn_depth.
```

**Sobrescrita de provider:model do subagente:** Por padrão, os subagentes herdam o provedor e modelo do agente pai. Defina `delegation.provider` e `delegation.model` para rotear subagentes para um par provedor:modelo diferente — por exemplo, use um modelo barato/rápido para subtarefas com escopo restrito enquanto seu agente principal roda um modelo de raciocínio caro.

**Sobrescrita de endpoint direto:** Se você quiser o caminho óbvio de endpoint personalizado, defina `delegation.base_url`, `delegation.api_key`, e `delegation.model`. Isso envia os subagentes diretamente para esse endpoint compatível com OpenAI e tem precedência sobre `delegation.provider`. Se `delegation.api_key` for omitido, o Hermes recorre apenas a `OPENAI_API_KEY`.

**Protocolo de conexão (`api_mode`):** O Hermes detecta automaticamente o protocolo de conexão a partir de `delegation.base_url` (por exemplo, caminhos terminados em `/anthropic` → `anthropic_messages`; hostnames do Codex/Anthropic nativo/Kimi-coding mantêm sua detecção existente). Para endpoints que a heurística não consegue classificar — por exemplo, Azure AI Foundry, MiniMax, Zhipu GLM, ou proxies LiteLLM expondo um backend no formato Anthropic — defina `delegation.api_mode` explicitamente para um de `chat_completions`, `codex_responses`, ou `anthropic_messages`. Deixe vazio (o padrão) para manter a detecção automática.

O provedor de delegação usa a mesma resolução de credenciais que a inicialização da CLI/gateway. Todos os provedores configurados são suportados: `openrouter`, `nous`, `copilot`, `zai`, `kimi-coding`, `minimax`, `minimax-cn`. Quando um provedor é definido, o sistema resolve automaticamente a URL base, chave de API e modo de API corretos — nenhuma conexão manual de credenciais necessária.

**Precedência:** `delegation.base_url` na config → `delegation.provider` na config → provedor pai (herdado). `delegation.model` na config → modelo pai (herdado). Definir apenas `model` sem `provider` muda apenas o nome do modelo mantendo as credenciais do pai (útil para trocar de modelo dentro do mesmo provedor, como o OpenRouter).

**Largura e profundidade:** `max_concurrent_children` limita quantos subagentes rodam em paralelo por lote (padrão `3`, piso de 1, sem teto). Também pode ser definido via a variável de ambiente `DELEGATION_MAX_CONCURRENT_CHILDREN`. Quando o modelo envia um array `tasks` mais longo do que o limite, `delegate_task` retorna um erro de ferramenta explicando o limite em vez de truncar silenciosamente. `max_spawn_depth` controla a profundidade da árvore de delegação (limitada de 1 a 3). No padrão `1`, a delegação é plana: filhos não podem gerar netos, e passar `role="orchestrator"` degrada silenciosamente para `leaf`. Aumente para `2` para que filhos orquestradores possam gerar netos folha; `3` para árvores de três níveis. O agente opta pela orquestração por chamada via `role="orchestrator"`; `orchestrator_enabled: false` força todo filho de volta para leaf independentemente. O custo escala multiplicativamente — em `max_spawn_depth: 3` com `max_concurrent_children: 3`, a árvore pode alcançar 3×3×3 = 27 agentes folha concorrentes. Veja [Subagent Delegation → Depth Limit and Nested Orchestration](features/delegation.md#depth-limit-and-nested-orchestration) para padrões de uso.

## Clarificação {#clarify}

Configure quanto tempo o gateway espera por uma resposta a uma pergunta de esclarecimento. A chave canônica é `agent.clarify_timeout` (padrão `3600` segundos); uma chave legada de nível superior `clarify.timeout` ainda é respeitada se definida explicitamente:

```yaml
agent:
  clarify_timeout: 3600        # Seconds to wait for user clarification response (0 or less = unlimited)
```

## Arquivos de Contexto (SOUL.md, AGENTS.md) {#context-files-soulmd-agentsmd}

O Hermes usa dois escopos de contexto diferentes:

| Arquivo | Propósito | Escopo |
|------|---------|-------|
| `SOUL.md` | **Identidade primária do agente** — define quem é o agente (slot #1 no prompt de sistema) | `~/.hermes/SOUL.md` ou `$HERMES_HOME/SOUL.md` |
| `.hermes.md` / `HERMES.md` | Instruções específicas do projeto (prioridade mais alta) | Sobe até a raiz do git |
| `AGENTS.md` | Instruções específicas do projeto, convenções de código | Percurso recursivo de diretório |
| `CLAUDE.md` | Arquivos de contexto do Claude Code (também detectado) | Apenas diretório de trabalho |
| `.cursorrules` | Regras do Cursor IDE (também detectado) | Apenas diretório de trabalho |
| `.cursor/rules/*.mdc` | Arquivos de regra do Cursor (também detectado) | Apenas diretório de trabalho |

- **SOUL.md** é a identidade primária do agente. Ocupa o slot #1 no prompt de sistema, substituindo completamente a identidade padrão embutida. Edite-o para personalizar totalmente quem é o agente.
- Se o SOUL.md estiver ausente, vazio, ou não puder ser carregado, o Hermes recorre a uma identidade padrão embutida.
- **Arquivos de contexto de projeto usam um sistema de prioridade** — apenas UM tipo é carregado (a primeira correspondência vence): `.hermes.md` → `AGENTS.md` → `CLAUDE.md` → `.cursorrules`. O SOUL.md é sempre carregado independentemente.
- **AGENTS.md** é hierárquico: se subdiretórios também têm AGENTS.md, todos são combinados.
- O Hermes semeia automaticamente um `SOUL.md` padrão se um ainda não existir.
- Todos os arquivos de contexto carregados são limitados a `context_file_max_chars` caracteres (padrão 20.000) com truncamento inteligente.

Veja também:
- [Personality & SOUL.md](/user-guide/features/personality)
- [Context Files](/user-guide/features/context-files)

## Diretório de Trabalho {#working-directory}

| Contexto | Padrão |
|---------|---------|
| **CLI (`hermes`)** | Diretório atual onde você executa o comando |
| **Gateway de mensagens** | `terminal.cwd` de `~/.hermes/config.yaml`; se não definido, diretório home `~` |
| **Docker / Singularity / Modal / SSH** | Diretório home do usuário dentro do contêiner ou máquina remota |

Sobrescreva o diretório de trabalho:
```yaml
# In ~/.hermes/config.yaml:
terminal:
  cwd: /home/myuser/projects
```

Entradas `MESSAGING_CWD` e `TERMINAL_CWD` diretas em `~/.hermes/.env` são alternativas de compatibilidade legada. Configurações novas devem usar `terminal.cwd`.

## Rede {#network}

Contornos de conectividade para HTTP de saída:

```yaml
network:
  force_ipv4: false   # Force IPv4 for outbound connections (default: false)
```

`force_ipv4` — em servidores com IPv6 quebrado ou inacessível, o Python resolve registros AAAA primeiro e pode travar pelo timeout TCP completo antes de recorrer ao IPv4. Defina isso como `true` para pular o IPv6 completamente e conectar via IPv4 diretamente.

## Onboarding {#onboarding}

Dicas de onboarding do primeiro contato e a oferta estruturada de construção de perfil:

```yaml
onboarding:
  profile_build: "ask"   # "ask" (default) | "off"
  seen: {}               # internal latch — leave empty
```

- `profile_build` — controla o caminho de construção de perfil oferecido na primeiríssima mensagem do gateway. `"ask"` (padrão) oferece construir um perfil de usuário; a oferta é **opt-in e sujeita a consentimento** — o agente pergunta antes de qualquer consulta e nunca lê contas conectadas silenciosamente. `"off"` mostra apenas uma introdução simples. A oferta dispara no máximo uma vez.
- `seen` — estado interno. O Hermes trava cada dica mostrada aqui para que nunca dispare novamente; a oferta de construção de perfil também é registrada aqui uma vez mostrada. Não edite manualmente — apague toda a seção `onboarding` se você quiser ver todas as dicas novamente.

## Dashboard {#dashboard}

Configuração para o [dashboard web](/user-guide/features/web-dashboard) — tema visual, URL pública, e provedores de autenticação. Os provedores de autenticação (OAuth, senha básica, drenagem) estão documentados em detalhes na página do dashboard web; isso é o formato do `config.yaml`.

```yaml
dashboard:
  theme: "default"            # "default" | "midnight" | "ember" | "mono" | "cyberpunk" | "rose"
  show_token_analytics: false # Re-enable the (local-estimate-only) token/cost analytics surfaces
  public_url: ""              # Full public authority for OAuth redirect_uri (env: HERMES_DASHBOARD_PUBLIC_URL)
  oauth:                      # Portal OAuth gate (engaged with --host and not --insecure)
    client_id: ""             # agent:{instance_id} — Portal provisions this
    portal_url: ""            # blank → plugin default (production Portal)
  basic_auth:                 # Self-hosted username/password gate (dashboard_auth/basic plugin)
    username: ""              # blank → plugin no-op
    password_hash: ""         # scrypt$... (preferred — no plaintext at rest)
    password: ""              # plaintext fallback (hashed in-memory at load)
    secret: ""                # token-signing key; blank → random per-process
    session_ttl_seconds: 0    # 0 → plugin default (12h)
  drain_auth:                 # Drain-control service-credential gate (dashboard_auth/drain plugin)
    scope: "drain"            # capability label on the verified principal
    min_secret_chars: 43      # entropy bar (url-safe-b64 chars; 43 ≈ 256 bits)
```

- `theme` — tema visual do dashboard.
- `show_token_analytics` — desativado por padrão. A página Analytics e os números de token/custo são uma **estimativa local de limite inferior** (excluem chamadas auxiliares, novas tentativas, fallbacks e escritas em cache), então podem aparecer bem abaixo da fatura real do provedor. Defina como `true` apenas se você entender que não são valores de faturamento.
- `public_url` — quando definido, essa é a autoridade completa (esquema + host + prefixo de caminho opcional) a partir da qual o `redirect_uri` do OAuth é construído. Defina isso para implantações atrás de proxies reversos que não encaminham de forma confiável os cabeçalhos `X-Forwarded-*`. Deixe vazio para usar a reconstrução via cabeçalho de proxy.
- `oauth` / `basic_auth` / `drain_auth` — configuração de provedor de autenticação lida pelos plugins de autenticação de dashboard empacotados. O segredo de drenagem em si **não** é definido aqui; ele é provisionado via a variável de ambiente `HERMES_DASHBOARD_DRAIN_SECRET`. Veja [Web Dashboard](/user-guide/features/web-dashboard) para a configuração completa de autenticação.
