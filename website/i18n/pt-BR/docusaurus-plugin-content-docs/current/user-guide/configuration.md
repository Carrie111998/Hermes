---
sidebar_position: 2
title: "Configuração"
description: "Configure o Hermes Agent — config.yaml, providers, modelos, chaves de API e muito mais"
---

# Configuração

Todas as configurações ficam no diretório `~/.hermes/` para facilitar o acesso.

:::tip Caminho mais fácil para um `config.yaml` funcional
Execute `hermes setup --portal` — um OAuth configura um provider de modelo e as quatro ferramentas do Tool Gateway sem editar YAML manualmente. Assinantes do Portal também ganham 10% de desconto em providers cobrados por token. Veja [Nous Portal](/integrations/nous-portal).
:::

## Estrutura de diretórios {#directory-structure}

```text
~/.hermes/
├── config.yaml     # Configurações (modelo, terminal, TTS, compressão, etc.)
├── .env            # Chaves de API e segredos
├── auth.json       # Credenciais OAuth de providers (Nous Portal, etc.)
├── SOUL.md         # Identidade principal do agente (slot #1 no system prompt)
├── memories/       # Memória persistente (MEMORY.md, USER.md)
├── skills/         # Skills criadas pelo agente (gerenciadas via ferramenta skill_manage)
├── cron/           # Jobs agendados
├── sessions/       # Sessões do gateway
└── logs/           # Logs (errors.log, gateway.log — segredos redigidos automaticamente)
```

## Gerenciando a configuração {#managing-configuration}

```bash
hermes config              # Ver configuração atual
hermes config edit         # Abrir config.yaml no seu editor
hermes config get KEY      # Imprimir um valor resolvido
hermes config set KEY VAL  # Definir um valor específico
hermes config unset KEY    # Remover um valor definido pelo usuário
hermes config check        # Verificar opções ausentes (após updates)
hermes config migrate      # Adicionar opções ausentes interativamente

# Exemplos:
hermes config get model
hermes config set model anthropic/claude-opus-4
hermes config set terminal.backend docker
hermes config unset terminal.backend
hermes config set OPENROUTER_API_KEY sk-or-...  # Salva em .env
```

:::tip
O comando `hermes config set` encaminha os valores automaticamente para o arquivo certo — chaves de API vão para `.env`, todo o resto para `config.yaml`.
:::

## Precedência de configuração {#configuration-precedence}

As configurações são resolvidas nesta ordem (maior prioridade primeiro):

1. **Argumentos da CLI** — ex.: `hermes chat --model anthropic/claude-sonnet-4` (override por invocação)
2. **`~/.hermes/config.yaml`** — arquivo principal de configuração para todas as opções não secretas
3. **`~/.hermes/.env`** — fallback para variáveis de ambiente; **obrigatório** para segredos (chaves de API, tokens, senhas)
4. **Defaults embutidos** — defaults seguros hardcoded quando nada mais está definido

:::info Regra prática
Segredos (chaves de API, tokens de bot, senhas) vão em `.env`. Todo o resto (modelo, backend de terminal, configurações de compressão, limites de memória, toolsets) vai em `config.yaml`. Quando ambos estão definidos, `config.yaml` prevalece para configurações não secretas.
:::

:::tip Deployments organizacionais
Um administrador pode fixar valores específicos de config e segredos que um usuário
padrão não pode sobrescrever, via um diretório gerenciado em nível de sistema. Veja
[Managed Scope](/user-guide/managed-scope).
:::

## Substituição de variáveis de ambiente {#environment-variable-substitution}

Você pode referenciar variáveis de ambiente em `config.yaml` usando a sintaxe `${VAR_NAME}`:

```yaml
auxiliary:
  vision:
    api_key: ${GOOGLE_API_KEY}
    base_url: ${CUSTOM_VISION_URL}

delegation:
  api_key: ${DELEGATION_KEY}
```

Múltiplas referências em um único valor funcionam: `url: "${HOST}:${PORT}"`. Se uma variável referenciada não estiver definida, o placeholder é mantido literalmente (`${UNDEFINED_VAR}` permanece como está) e um aviso é registrado no log. `$VAR` simples não é expandido.

A sintaxe SecretRef estilo Cursor também é aceita: `${env:VAR_NAME}` resolve exatamente como `${VAR_NAME}` (o prefixo `env:` é removido), então snippets de MCP ou provider copiados de configs do Cursor / Claude funcionam inalterados tanto em `config.yaml` quanto no bloco `mcp_servers`. Outras fontes SecretRef (`${file:...}`, `${vault:...}`, `${bitwarden:...}`) **não** são resolvidas inline — backends de segredos externos injetam seus valores no ambiente na inicialização via o bloco `secrets:`, então referencie-os como `${env:NAME}`; prefixos desconhecidos avisam uma vez e permanecem literais.

Para configuração de providers de IA (OpenRouter, Anthropic, Copilot, endpoints customizados, LLMs self-hosted, modelos de fallback, etc.), veja [AI Providers](/integrations/providers).

### Timeouts de provider {#provider-timeouts}

Você pode definir `providers.<id>.request_timeout_seconds` para um timeout de requisição em todo o provider, além de `providers.<id>.models.<model>.timeout_seconds` para um override específico por modelo. Aplica-se ao cliente principal de turno em todo transport (OpenAI-wire, Anthropic nativo, compatível com Anthropic), à cadeia de fallback, rebuilds após rotação de credenciais e (para OpenAI-wire) ao kwarg de timeout por requisição — então o valor configurado prevalece sobre a env var legada `HERMES_API_TIMEOUT`.

Você também pode definir `providers.<id>.stale_timeout_seconds` para o detector de chamadas stale não-streaming, além de `providers.<id>.models.<model>.stale_timeout_seconds` para um override específico por modelo. Isso prevalece sobre a env var legada `HERMES_API_CALL_STALE_TIMEOUT`.

Deixar esses valores indefinidos mantém os defaults legados (`HERMES_API_TIMEOUT=1800`s, `HERMES_API_CALL_STALE_TIMEOUT=90`s, Anthropic nativo 900s). O detector stale não-streaming é desabilitado automaticamente para endpoints locais quando deixado implícito e pode escalar para contextos muito grandes. Atualmente não está conectado ao AWS Bedrock (tanto os caminhos `bedrock_converse` quanto AnthropicBedrock SDK usam boto3 com sua própria configuração de timeout). Veja o exemplo comentado em [`cli-config.yaml.example`](https://github.com/NousResearch/hermes-agent/blob/main/cli-config.yaml.example).

## Comportamento de atualização {#update-behavior}

As configurações de `hermes update` ficam em `updates` no `config.yaml`:

```yaml
updates:
  pre_update_backup: quick       # quick (snapshot de state, default) | full (snapshot + zip do HERMES_HOME) | off
  backup_keep: 5                 # Quantidade de zips completos de backup pré-update a manter
  non_interactive_local_changes: stash  # stash | discard
```

`pre_update_backup` é o único knob de segurança pré-update: `quick` (default) faz snapshot de arquivos críticos de state (dados de pairing, cron jobs, config, auth; arquivos acima de 1 GiB são ignorados) em `state-snapshots/`; `full` adicionalmente compacta todo o `HERMES_HOME` em `backups/` e pode levar minutos em homes grandes; `off` desabilita ambos. Booleanos legados são respeitados (`true` → `full`, `false` → `off`).

Para instalações git, o Hermes faz auto-stash de arquivos rastreados sujos e arquivos não rastreados antes de fazer checkout da branch de update ou pull. Updates interativos no terminal pedem confirmação antes de restaurar esse stash. Updates não interativos (desktop/chat app, gateway ou `--yes`) usam `updates.non_interactive_local_changes`: `stash` restaura edições locais de source após um pull bem-sucedido, enquanto `discard` descarta o stash criado pelo update após um pull bem-sucedido. Use `discard` apenas em instalações gerenciadas onde edições locais de source nunca devem persistir.

Antes desse passo de stash, o Hermes também restaura diffs rastreados de `package-lock.json` deixados por churn de npm install/build. Faça commit ou stash manual de edições intencionais do lockfile antes de atualizar.

## Configuração do backend de terminal {#terminal-backend-configuration}

O Hermes suporta sete backends de terminal. Cada um determina onde os comandos shell do agente realmente executam — sua máquina local, um container Docker, um servidor remoto via SSH, um sandbox na nuvem Modal (direto ou via gateway gerenciado pela Nous), um workspace Daytona, um Vercel Sandbox ou um container Singularity/Apptainer.

```yaml
terminal:
  backend: local    # local | docker | ssh | modal | daytona | vercel_sandbox | singularity
  cwd: "."          # Diretório de trabalho do gateway/cron (CLI sempre usa o dir de lançamento)
  font_family: ""   # Fonte do terminal no Desktop; ex.: "MesloLGS NF"
  timeout: 180      # Timeout por comando em segundos
  home_mode: auto   # auto | real | profile — política de HOME dos subprocessos
  env_passthrough: []  # Nomes de env vars a encaminhar para execução sandboxed (terminal + execute_code)
  singularity_image: "docker://nikolaik/python-nodejs:python3.11-nodejs20"  # Imagem do container para backend Singularity
  modal_image: "nikolaik/python-nodejs:python3.11-nodejs20"                 # Imagem do container para backend Modal
  daytona_image: "nikolaik/python-nodejs:python3.11-nodejs20"               # Imagem do container para backend Daytona
```

`terminal.font_family` controla o terminal embutido no Hermes Desktop. Aceita um nome de família instalada localmente (por exemplo, `MesloLGS NF`) ou uma pilha de fontes CSS. O Hermes anexa sua pilha JetBrains Mono incluída como fallback, e um valor vazio mantém o default. Você pode editar a mesma configuração com escopo de profile em **Settings → Appearance → Terminal Font**; não é necessário download do Google Fonts nem permissão de fonte do sistema.

Para sandboxes na nuvem como Modal, Daytona e Vercel Sandbox, `container_persistent: true` significa que o Hermes tentará preservar o state do filesystem entre recriações de sandbox. Isso não garante que o mesmo sandbox ativo, espaço de PID ou processos em background ainda estarão rodando depois.

### Visão geral dos backends {#backend-overview}

| Backend | Onde os comandos rodam | Isolamento | Melhor para |
|---------|-------------------|-----------|----------|
| **local** | Sua máquina diretamente | Nenhuma | Desenvolvimento, uso pessoal |
| **docker** | Container Docker persistente único (compartilhado entre sessão, `/new`, subagentes) | Total (namespaces, cap-drop) | Sandboxing seguro, CI/CD |
| **ssh** | Servidor remoto via SSH | Limite de rede | Dev remoto, hardware potente |
| **modal** | Sandbox na nuvem Modal | Total (VM na nuvem) | Compute efêmero na nuvem, evals |
| **daytona** | Workspace Daytona | Total (container na nuvem) | Ambientes de dev gerenciados na nuvem |
| **vercel_sandbox** | Vercel Sandbox | Total (microVM na nuvem) | Execução na nuvem com persistência de filesystem via snapshot |
| **singularity** | Container Singularity/Apptainer | Namespaces (--containall) | Clusters HPC, máquinas compartilhadas |

### Backend local {#local-backend}

O default. Comandos rodam diretamente na sua máquina sem isolamento. Nenhuma configuração especial necessária.

```yaml
terminal:
  backend: local
```

Por padrão, subprocessos de ferramentas locais mantêm o `HOME` real do usuário do SO. Isso permite que
CLIs externos como `git`, `ssh`, `gh`, `az`, `npm`, Claude Code e Codex
encontrem as credenciais e configs que já usam no seu shell normal. O state do Hermes
continua com escopo de profile via `HERMES_HOME`; `HOME` não é como profiles
selecionam config, memória, sessões ou skills.

O Hermes **não** altera seu `HOME` em todo o sistema, seus arquivos de startup do shell nem
o home da conta do sistema operacional. Esta configuração controla apenas o ambiente
passado aos subprocessos que o Hermes lança via ferramentas como `terminal`,
processos de terminal em background, `execute_code` e processos auxiliares ACP.

#### `terminal.home_mode` {#terminalhome_mode}

| Modo | Instalações no host | Containers | Tradeoff |
|---|---|---|---|
| `auto` | Mantém o `HOME` real do usuário do SO | Usa `{HERMES_HOME}/home` | Default recomendado. CLIs do host continuam funcionando; state do container persiste. |
| `real` | Força o `HOME` real do usuário do SO | Força o `HOME` real do usuário do SO se visível | Útil se um processo pai iniciou acidentalmente com `HOME` apontando para um home de profile. |
| `profile` | Usa `{HERMES_HOME}/home` quando existir | Usa `{HERMES_HOME}/home` quando existir | Isolamento estrito de config de CLI por profile, mas `~/.ssh`, `~/.gitconfig`, `~/.azure`, `~/.config/gh`, auth Claude/Codex, state npm, etc. normais não ficarão visíveis a menos que você os inicialize ou vincule dentro do home do profile. |

A desvantagem do default é que profiles no host compartilham as mesmas
credenciais/configs de CLI em nível de usuário sob `~`. Se você precisa de um profile com
identidade git separada, chaves SSH, login GitHub CLI, config npm ou login de CLI na nuvem,
use `home_mode: profile` e inicialize essas ferramentas dentro daquele home de profile
deliberadamente.

Se você quer intencionalmente isolamento estrito de config de ferramentas por profile, defina:

```yaml
terminal:
  home_mode: profile
```

Nesse modo, subprocessos de ferramentas usam `{HERMES_HOME}/home` como `HOME`. O Hermes também
define `HERMES_REAL_HOME` para que scripts ainda possam localizar o home real do usuário quando
precisarem. Backends de container continuam usando `{HERMES_HOME}/home` no modo `auto`
porque esse diretório fica no volume persistente de dados do Hermes.

Scripts que precisam distinguir state de profile do home real do usuário devem
preferir `HERMES_HOME` para dados do Hermes e `HERMES_REAL_HOME` para o home da conta:

```python
from pathlib import Path
import os

hermes_home = Path(os.environ["HERMES_HOME"])
real_home = Path(os.environ.get("HERMES_REAL_HOME", os.environ["HOME"]))
```

:::warning
O agente tem o mesmo acesso ao filesystem que sua conta de usuário. Use `hermes tools` para desabilitar ferramentas que não quer, ou mude para Docker para sandboxing.
:::

### Backend Docker {#docker-backend}

Executa comandos dentro de um container Docker com hardening de segurança (todas as capabilities removidas, sem escalação de privilégio, limites de PID).

**Container persistente único, compartilhado entre processos Hermes.** O Hermes inicia UM container de longa duração no primeiro uso e encaminha toda chamada de terminal, arquivo e `execute_code` via `docker exec` para esse mesmo container — entre sessões, `/new`, `/reset` e subagentes de `delegate_task`. Mudanças de diretório de trabalho, pacotes instalados, arquivos em `/workspace` e **processos em background** persistem de uma chamada de ferramenta para a próxima, e de um processo Hermes para o outro. Quando você fecha uma sessão TUI, executa `/quit` ou inicia uma nova invocação `hermes`, o container continua rodando e o próximo processo Hermes o reutiliza via lookup por label. Veja **Container lifecycle** abaixo para as regras exatas de teardown.

```yaml
terminal:
  backend: docker
  docker_image: "nikolaik/python-nodejs:python3.11-nodejs20"
  docker_mount_cwd_to_workspace: false  # Montar dir de lançamento em /workspace
  docker_run_as_host_user: false   # Veja "Running container as host user" abaixo
  docker_forward_env:              # Env vars do host a encaminhar para o container
    - "GITHUB_TOKEN"
  docker_env:                      # Env vars literais a injetar (KEY=value)
    DEBUG: "1"
    PYTHONUNBUFFERED: "1"
  docker_volumes:                  # Montagens de diretório do host
    - "/home/user/projects:/workspace/projects"
    - "/home/user/data:/data:ro"   # :ro para read-only
  docker_extra_args:               # Flags extras anexadas literalmente ao `docker run`
    - "--gpus=all"
    - "--network=host"
  docker_network: true             # false = air-gap do container (--network=none)

  # Limites de recursos
  container_cpu: 1                 # Núcleos de CPU (0 = ilimitado)
  container_memory: 5120           # MB (0 = ilimitado)
  container_disk: 51200            # MB (requer overlay2 em XFS+pquota)
  container_persistent: true       # Persistir dirs bind-mount /workspace e /root

  # Reutilização de container entre processos (defaults correspondem ao contrato de "um container de longa duração
  # compartilhado entre sessões" — veja Container lifecycle).
  docker_persist_across_processes: true   # Reutilizar container entre restarts do Hermes
  docker_orphan_reaper: true              # Varrer containers Exited abandonados na inicialização

  # Configurações de lifecycle cross-backend (aplicam-se ao docker também)
  timeout: 180                     # Timeout por comando em segundos
  lifetime_seconds: 300            # Janela do idle-reaper; também alimenta threshold 2× do orphan-reaper
```

**`docker_env`** vs **`docker_forward_env`**: o primeiro injeta pares literais `KEY=value` que você especifica na config (os valores ficam no seu `config.yaml` ou são passados como dict JSON via `TERMINAL_DOCKER_ENV='{"DEBUG":"1"}'`). O segundo encaminha valores do seu shell ou `~/.hermes/.env`, então o segredo real nunca aparece no arquivo de config. Use `docker_forward_env` para tokens e `docker_env` para knobs estáticos que o container precisa.

**`terminal.docker_extra_args`** (também sobrescrevível via `TERMINAL_DOCKER_EXTRA_ARGS='["--gpus=all"]'`) permite passar flags arbitrárias de `docker run` que o Hermes não expõe como chaves de primeira classe — `--gpus`, `--network`, `--add-host`, overrides alternativos de `--security-opt`, etc. Cada entrada deve ser uma string; a lista é anexada por último à invocação `docker run` montada para poder sobrescrever os defaults do Hermes se necessário. Use com moderação — flags que conflitam com o hardening do sandbox (capability drops, `--user`, bind mount do workspace) enfraquecem o isolamento silenciosamente.

**`terminal.docker_network`** (default `true`; env: `TERMINAL_DOCKER_NETWORK`) — defina como `false` para rodar o container sandbox com `--network=none`, cortando todo egress de rede dos comandos do agente. Isso se aplica ao container de execução usado por `terminal`, `execute_code` e as ferramentas de arquivo. Como containers persistem entre processos Hermes, mudar isso para `false` enquanto um container antigo com rede existe remove esse container e inicia um novo air-gapped (um aviso é registrado); processos em background rodando dentro dele são perdidos. Prefira esta chave em vez de passar `--network=none` via `docker_extra_args`.

**Requisitos:** Docker Desktop ou Docker Engine instalado e rodando. O Hermes verifica `$PATH` mais locais comuns de instalação no macOS (`/usr/local/bin/docker`, `/opt/homebrew/bin/docker`, app bundle do Docker Desktop). Podman é suportado out of the box: defina `HERMES_DOCKER_BINARY=podman` (ou o caminho completo) para forçá-lo quando ambos estão instalados.

#### Ciclo de vida do container {#container-lifecycle}

Todo container gerenciado pelo Hermes recebe três labels para que processos subsequentes (e o orphan reaper) possam identificá-lo:

- `hermes-agent=1` — marca como gerenciado pelo Hermes
- `hermes-task-id=<sanitized task_id>` — chaveia a sonda de reutilização por task
- `hermes-profile=<sanitized profile name>` — delimita reutilização e reaping ao profile Hermes ativo

Na inicialização, o Hermes executa `docker ps --filter label=hermes-task-id=<id> --filter label=hermes-profile=<profile>` e **anexa ao container existente** quando encontra um. Se o container está `exited` (ex.: após restart do daemon Docker), ele recebe `docker start` e é reutilizado — state do filesystem e pacotes instalados sobrevivem, mas processos em background dentro do container não.

Quando um processo Hermes encerra — `/quit`, fechar sessão TUI, shutdown do gateway, até SIGKILL — o caminho de cleanup é **no-op para o container no modo default**. O container continua rodando. O próximo processo Hermes anexa a ele em milissegundos via sonda de label. Esse é o comportamento que o contrato de "um container de longa duração compartilhado entre sessões" exige: é a única forma de processos em background (watchers npm, dev servers, pytest longo) sobreviverem entre sessões.

**O container só é derrubado (stopped e `docker rm -f`'d) nestes casos:**

| Gatilho | Quando dispara |
|---|---|
| `docker_persist_across_processes: false` | Isolamento explícito por processo. Todo `cleanup()` faz `stop` + `rm -f`. Corresponde ao comportamento pré-issue-#20561. |
| Idle reaper (`lifetime_seconds`, default 300s) | Apenas quando o env é `persist_across_processes=false`. Envs em modo persist são no-op'd; container sobrevive ao sweep idle. |
| Orphan reaper na próxima inicialização | Varre containers com label hermes **Exited** mais antigos que `2 × lifetime_seconds` (default 600s = 10 min), com escopo do profile atual. **Containers Running nunca são tocados** — segurança entre processos irmãos. Defina `docker_orphan_reaper: false` para desabilitar. |
| Ação direta do usuário | `docker rm -f`, `docker system prune`, restart do Docker Desktop. Não definimos `--restart=always`, então reboot do host deixa o container `Exited` (sua camada CoW sobrevive e é reutilizada na próxima inicialização, mas processos bg somem). |

Casos extremos que valem conhecer:

- **OOM kill do PID 1 dentro do container** transiciona o container para `Exited`. A próxima reutilização fará `docker start`; state do filesystem sobrevive, processos bg não.
- **Trocar profiles** isola containers entre si — um container com label `hermes-profile=work` é invisível para um processo Hermes rodando sob `hermes-profile=research`. O orphan reaper também é com escopo de profile, então containers cross-profile não são reaped acidentalmente, mas também não serão limpos automaticamente até você iniciar o Hermes novamente sob o profile original.

Subagentes paralelos spawnados via `delegate_task(tasks=[...])` compartilham este único container — `cd` concorrente, mutações de env e writes no mesmo path colidirão. Se um subagente precisa de sandbox isolado, deve registrar override de imagem por task via `register_task_env_overrides()`, o que ambientes RL e benchmark (TerminalBench2, HermesSweEnv, etc.) fazem automaticamente para suas imagens Docker por task.

**Hardening de segurança:**
- `--cap-drop ALL` com apenas `DAC_OVERRIDE`, `CHOWN`, `FOWNER` readicionadas
- `--security-opt no-new-privileges`
- `--pids-limit 256`
- tmpfs com tamanho limitado para `/tmp` (512MB), `/var/tmp` (256MB), `/run` (64MB)

**Encaminhamento de credenciais:** Env vars listadas em `docker_forward_env` são resolvidas do seu ambiente shell primeiro, depois `~/.hermes/.env`. Skills também podem declarar `required_environment_variables`, que são mescladas automaticamente.

#### Overrides de variáveis de ambiente {#environment-variable-overrides}

Toda chave sob `terminal:` tem um override de env var no formato `TERMINAL_<KEY_UPPERCASE>`. As mais úteis para o backend Docker:

| Env var | Maps to | Notes |
|---|---|---|
| `TERMINAL_DOCKER_IMAGE` | `docker_image` | Imagem base |
| `TERMINAL_DOCKER_FORWARD_ENV` | `docker_forward_env` | Array JSON: `'["GITHUB_TOKEN","OPENAI_API_KEY"]'` |
| `TERMINAL_DOCKER_ENV` | `docker_env` | Dict JSON: `'{"DEBUG":"1"}'` |
| `TERMINAL_DOCKER_VOLUMES` | `docker_volumes` | Array JSON de strings `"host:container[:ro]"` |
| `TERMINAL_DOCKER_EXTRA_ARGS` | `docker_extra_args` | Array JSON |
| `TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE` | `docker_mount_cwd_to_workspace` | `true` / `false` |
| `TERMINAL_DOCKER_RUN_AS_HOST_USER` | `docker_run_as_host_user` | `true` / `false` |
| `TERMINAL_DOCKER_NETWORK` | `docker_network` | `true` / `false` — default `true`; `false` = `--network=none` |
| `TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES` | `docker_persist_across_processes` | `true` / `false` — default `true` |
| `TERMINAL_DOCKER_ORPHAN_REAPER` | `docker_orphan_reaper` | `true` / `false` — default `true` |
| `TERMINAL_CONTAINER_CPU` | `container_cpu` | Núcleos de CPU |
| `TERMINAL_CONTAINER_MEMORY` | `container_memory` | MB |
| `TERMINAL_CONTAINER_DISK` | `container_disk` | MB |
| `TERMINAL_CONTAINER_PERSISTENT` | `container_persistent` | `true` / `false` — controla dirs de workspace bind-mount, distinto de `docker_persist_across_processes` |
| `TERMINAL_LIFETIME_SECONDS` | `lifetime_seconds` | Janela do idle reaper |
| `TERMINAL_TIMEOUT` | `timeout` | Timeout por comando |
| `HERMES_DOCKER_BINARY` | _none_ | Força caminho específico do binário docker/podman |

### Backend SSH {#ssh-backend}

Executa comandos em um servidor remoto via SSH. Usa ControlMaster para reutilização de conexão (keepalive idle de 5 minutos). Shell persistente habilitado por padrão — state (cwd, env vars) sobrevive entre comandos.

```yaml
terminal:
  backend: ssh
  persistent_shell: true           # Manter sessão bash de longa duração (default: true)
```

**Variáveis de ambiente obrigatórias:**

```bash
TERMINAL_SSH_HOST=my-server.example.com
TERMINAL_SSH_USER=ubuntu
```

**Opcionais:**

| Variável | Default | Descrição |
|----------|---------|-------------|
| `TERMINAL_SSH_PORT` | `22` | Porta SSH |
| `TERMINAL_SSH_KEY` | (default do sistema) | Caminho para chave privada SSH |
| `TERMINAL_SSH_PERSISTENT` | `true` | Habilitar shell persistente |

**Como funciona:** Conecta na inicialização com `BatchMode=yes` e `StrictHostKeyChecking=accept-new`. Shell persistente mantém um único processo `bash -l` vivo no host remoto, comunicando via arquivos temporários. Comandos que precisam de `stdin_data` ou `sudo` fazem fallback automático para modo one-shot.

### Backend Modal {#modal-backend}

Executa comandos em um sandbox na nuvem [Modal](https://modal.com). Cada task recebe uma VM isolada com CPU, memória e disco configuráveis. O filesystem pode ser snapshot/restaurado entre sessões.

```yaml
terminal:
  backend: modal
  container_cpu: 1                 # Núcleos de CPU
  container_memory: 5120           # MB (5GB)
  container_disk: 51200            # MB (50GB)
  container_persistent: true       # Snapshot/restauração do filesystem
```

**Obrigatório:** Variáveis de ambiente `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET`, ou arquivo de config `~/.modal.toml`.

**Persistência:** Quando habilitada, o filesystem do sandbox é snapshotado no cleanup e restaurado na próxima sessão. Snapshots são rastreados em `~/.hermes/modal_snapshots.json`. Isso preserva state do filesystem, não processos vivos, espaço de PID ou jobs em background.

**Arquivos de credenciais:** Montados automaticamente de `~/.hermes/` (tokens OAuth, etc.) e sincronizados antes de cada comando.

### Backend Daytona {#daytona-backend}

Executa comandos em um workspace gerenciado [Daytona](https://daytona.io). Suporta stop/resume para persistência.

```yaml
terminal:
  backend: daytona
  container_cpu: 1                 # Núcleos de CPU
  container_memory: 5120           # MB → convertido para GiB
  container_disk: 10240            # MB → convertido para GiB (máx. 10 GiB)
  container_persistent: true       # Stop/resume em vez de delete
```

**Obrigatório:** Variável de ambiente `DAYTONA_API_KEY`.

**Persistência:** Quando habilitada, sandboxes são stopped (não deleted) no cleanup e resumed na próxima sessão. Nomes de sandbox seguem o padrão `hermes-{task_id}`.

**Limite de disco:** Daytona impõe máximo de 10 GiB. Requisições acima disso são limitadas com aviso.

### Backend Vercel Sandbox {#vercel-sandbox-backend}

Executa comandos em uma microVM na nuvem [Vercel Sandbox](https://vercel.com/docs/vercel-sandbox). O Hermes usa as superfícies normais de terminal e ferramentas de arquivo; não há ferramentas model-facing específicas da Vercel.

```yaml
terminal:
  backend: vercel_sandbox
  vercel_runtime: node24          # node24 | node22 | python3.13
  cwd: /vercel/sandbox            # raiz padrão do workspace
  container_persistent: true      # Snapshot/restauração do filesystem
  container_disk: 51200           # Apenas default compartilhado; disco customizado não é suportado
```

**Instalação obrigatória:** Instale o extra opcional do SDK:

```bash
pip install 'hermes-agent[vercel]'
```

**Autenticação obrigatória:** Configure auth por access token com os três: `VERCEL_TOKEN`, `VERCEL_PROJECT_ID` e `VERCEL_TEAM_ID`. Este é o setup suportado para deployments e processos Hermes de longa duração em Render, Railway, Docker e hosts similares.

Para desenvolvimento local pontual, o Hermes também aceita tokens OIDC Vercel de curta duração:

```bash
VERCEL_OIDC_TOKEN="$(vc project token <project-name>)" hermes chat
```

De um diretório de projeto Vercel linkado, você pode omitir o nome do projeto:

```bash
VERCEL_OIDC_TOKEN="$(vc project token)" hermes chat
```

Tokens OIDC são de curta duração e não devem ser usados como caminho de deployment documentado.

**Runtime:** `terminal.vercel_runtime` suporta `node24`, `node22` e `python3.13`. Se indefinido, o Hermes usa default `node24`.

**Persistência:** Quando `container_persistent: true`, o Hermes faz snapshot do filesystem do sandbox durante o cleanup e restaura um sandbox posterior para a mesma task desse snapshot. O conteúdo do snapshot pode incluir credenciais sincronizadas pelo Hermes, skills e arquivos de cache copiados para o sandbox. Isso preserva apenas state do filesystem; não preserva identidade viva do sandbox, espaço de PID, state do shell ou processos em background rodando.

**Comandos em background:** `terminal(background=true)` usa o fluxo genérico de processos em background não-local do Hermes. Você pode spawnar, fazer poll, wait, ver logs e matar processos via a ferramenta de processo normal enquanto o sandbox está vivo. O Hermes não fornece recuperação nativa de processos detached Vercel após cleanup ou restart.

**Dimensionamento de disco:** Vercel Sandbox atualmente não suporta o knob de recurso `container_disk` do Hermes. Deixe `container_disk` indefinido ou no default compartilhado `51200`; valores não-default falham em diagnostics e criação de backend em vez de serem ignorados silenciosamente.

### Backend Singularity/Apptainer {#singularityapptainer-backend}

Executa comandos em um container [Singularity/Apptainer](https://apptainer.org). Projetado para clusters HPC e máquinas compartilhadas onde Docker não está disponível.

```yaml
terminal:
  backend: singularity
  singularity_image: "docker://nikolaik/python-nodejs:python3.11-nodejs20"
  container_cpu: 1                 # Núcleos de CPU
  container_memory: 5120           # MB
  container_persistent: true       # Overlay gravável persiste entre sessões
```

**Requisitos:** Binário `apptainer` ou `singularity` em `$PATH`.

**Tratamento de imagem:** URLs Docker (`docker://...`) são convertidas automaticamente para arquivos SIF e cacheadas. Arquivos `.sif` existentes são usados diretamente.

**Diretório scratch:** Resolvido nesta ordem: `TERMINAL_SCRATCH_DIR` → `TERMINAL_SANDBOX_DIR/singularity` → `/scratch/$USER/hermes-agent` (convenção HPC) → `~/.hermes/sandboxes/singularity`.

**Isolamento:** Usa `--containall --no-home` para isolamento total de namespace sem montar o home do host.

### Problemas comuns de backend de terminal {#common-terminal-backend-issues}

Se comandos de terminal falham imediatamente ou a ferramenta terminal é reportada como desabilitada:

- **Local** — Sem requisitos especiais. O default mais seguro ao começar.
- **Docker** — Execute `docker version` para verificar se o Docker funciona. Se falhar, corrija o Docker ou `hermes config set terminal.backend local`.
- **SSH** — Tanto `TERMINAL_SSH_HOST` quanto `TERMINAL_SSH_USER` devem estar definidos. O Hermes registra erro claro se algum estiver ausente.
- **Modal** — Precisa de env var `MODAL_TOKEN_ID` ou `~/.modal.toml`. Execute `hermes doctor` para verificar.
- **Daytona** — Precisa de `DAYTONA_API_KEY`. O SDK Daytona gerencia a configuração de URL do servidor.
- **Singularity** — Precisa de `apptainer` ou `singularity` em `$PATH`. Comum em clusters HPC.

Na dúvida, defina `terminal.backend` de volta para `local` e verifique se os comandos rodam lá primeiro.

### Sincronização remoto→host no teardown {#remote-to-host-state-sync-on-teardown}

Para os backends **SSH**, **Modal** e **Daytona**, o Hermes envia seu state `~/.hermes/` (arquivos de credenciais, skills, cache) para o sandbox remoto durante a sessão, e no teardown **sincroniza de volta arquivos de state alterados** para suas localizações originais no host. Arquivos que diferem do que foi enviado originalmente (comparados por hash de conteúdo) são aplicados de volta no lugar; arquivos remotos novos sob um diretório sincronizado (ex.: uma skill que o agente criou remotamente) são mapeados de volta para o path correspondente no host. Arquivos de credenciais upload-only nunca são sobrescritos no host.

- O sync-back tenta até 3 vezes com backoff e recusa extrair arquivos remotos maiores que 2 GiB.
- Docker e Singularity usam bind mounts (visão ao vivo do filesystem do host) e não precisam disso.
- Isso cobre state do Hermes (`~/.hermes/`), **não** arquivos arbitrários da working tree dentro do sandbox — peça ao agente para copiar artefatos importantes explicitamente (ex.: `scp`, `modal volume put`) antes do sandbox ser destruído.

### Montagens de volume Docker {#docker-volume-mounts}

Ao usar o backend Docker, `docker_volumes` permite compartilhar diretórios do host com o container. Cada entrada usa sintaxe Docker `-v` padrão: `host_path:container_path[:options]`.

```yaml
terminal:
  backend: docker
  docker_volumes:
    - "/home/user/projects:/workspace/projects"   # Leitura-escrita (default)
    - "/home/user/datasets:/data:ro"              # Somente leitura
    - "/home/user/.hermes/cache/documents:/output" # Exports visíveis ao gateway
```

Isso é útil para:
- **Fornecer arquivos** ao agente (datasets, configs, código de referência)
- **Receber arquivos** do agente (código gerado, relatórios, exports)
- **Workspaces compartilhados** onde você e o agente acessam os mesmos arquivos

Se você usa um gateway de messaging e quer que o agente envie arquivos gerados via
`MEDIA:/...`, prefira um mount de export dedicado visível ao host como
`/home/user/.hermes/cache/documents:/output`.

- Escreva arquivos dentro do Docker em `/output/...`
- Emita o **path do host** em `MEDIA:`, por exemplo:
  `MEDIA:/home/user/.hermes/cache/documents/report.txt`
- **Não** emita `/workspace/...` ou `/output/...` a menos que esse path exato também
  exista para o processo gateway no host

:::warning
Chaves YAML duplicadas sobrescrevem silenciosamente as anteriores. Se você já tem um
bloco `docker_volumes:`, mescle novos mounts na mesma lista em vez de adicionar
outra chave `docker_volumes:` mais adiante no arquivo.
:::

Também pode ser definido via variável de ambiente: `TERMINAL_DOCKER_VOLUMES='["/host:/container"]'` (array JSON).

### Encaminhamento de credenciais Docker {#docker-credential-forwarding}

Por padrão, sessões de terminal Docker não herdam credenciais arbitrárias do host. Se você precisa de um token específico dentro do container, adicione-o a `terminal.docker_forward_env`.

```yaml
terminal:
  backend: docker
  docker_forward_env:
    - "GITHUB_TOKEN"
    - "NPM_TOKEN"
```

O Hermes resolve cada variável listada do seu shell atual primeiro, depois faz fallback para `~/.hermes/.env` se foi salva com `hermes config set`.

:::warning
Tudo listado em `docker_forward_env` fica visível para comandos rodados dentro do container. Encaminhe apenas credenciais que você se sinta confortável em expor à sessão de terminal.
:::

### Executar o container como usuário do host {#running-the-container-as-your-host-user}

Por padrão, containers Docker rodam como `root` (UID 0). Arquivos criados dentro de `/workspace` ou outros bind-mounts acabam pertencendo ao root no host, então após uma sessão você precisa `sudo chown` neles antes de editá-los no editor do host. A flag `terminal.docker_run_as_host_user` corrige isso:

```yaml
terminal:
  backend: docker
  docker_run_as_host_user: true   # default: false
```

Quando habilitada, o Hermes anexa `--user $(id -u):$(id -g)` ao comando `docker run` para que arquivos escritos em diretórios bind-mounted (`/workspace`, `/root`, qualquer coisa em `docker_volumes`) pertençam ao seu usuário do host, não ao root. A troca: o container não pode mais `apt install` ou escrever em paths de root como `/root/.npm` — use uma imagem base cujo `HOME` pertença a usuário não-root (ou adicione suas ferramentas necessárias no build da imagem) se precisar de ambos.

Deixe `false` (o default) para comportamento retrocompatível. Ative quando seu workflow é majoritariamente "editar arquivos montados do host" e você está cansado de `sudo chown -R`.

### Opcional: montar o diretório de lançamento em `/workspace` {#optional-mount-the-launch-directory-into-workspace}

Sandboxes Docker permanecem isolados por padrão. O Hermes **não** passa seu diretório de trabalho atual do host para o container a menos que você opte explicitamente.

Habilite em `config.yaml`:

```yaml
terminal:
  backend: docker
  docker_mount_cwd_to_workspace: true
```

Quando habilitado:
- se você lançar o Hermes de `~/projects/my-app`, esse diretório do host é bind-mounted em `/workspace`
- o backend Docker inicia em `/workspace`
- ferramentas de arquivo e comandos de terminal veem o mesmo projeto montado

Quando desabilitado, `/workspace` permanece pertencente ao sandbox a menos que você monte algo explicitamente via `docker_volumes`.

Tradeoff de segurança:
- `false` preserva o limite do sandbox
- `true` dá ao sandbox acesso direto ao diretório de onde você lançou o Hermes

Use o opt-in apenas quando você quer intencionalmente que o container trabalhe em arquivos vivos do host.

### Shell persistente {#persistent-shell}

Por padrão, cada comando de terminal roda em seu próprio subprocesso — diretório de trabalho, variáveis de ambiente e variáveis de shell resetam entre comandos. Quando **shell persistente** está habilitado, um único processo bash de longa duração é mantido vivo entre chamadas `execute()` para que o state sobreviva entre comandos.

Isso é mais útil para o **backend SSH**, onde também elimina overhead de conexão por comando. Shell persistente está **habilitado por padrão para SSH** e desabilitado para o backend local.

```yaml
terminal:
  persistent_shell: true   # default — habilita shell persistente para SSH
```

Para desabilitar:

```bash
hermes config set terminal.persistent_shell false
```

**O que persiste entre comandos:**
- Diretório de trabalho (`cd /tmp` permanece para o próximo comando)
- Variáveis de ambiente exportadas (`export FOO=bar`)
- Variáveis de shell (`MY_VAR=hello`)

**Precedência:**

| Nível | Variável | Default |
|-------|----------|---------|
| Config | `terminal.persistent_shell` | `true` |
| Override SSH | `TERMINAL_SSH_PERSISTENT` | segue config |
| Override local | `TERMINAL_LOCAL_PERSISTENT` | `false` |

Variáveis de ambiente por backend têm precedência máxima. Se você quer shell persistente no backend local também:

```bash
export TERMINAL_LOCAL_PERSISTENT=true
```

:::note
Comandos que requerem `stdin_data` ou sudo fazem fallback automático para modo one-shot, já que o stdin do shell persistente já está ocupado pelo protocolo IPC.
:::

Veja [Code Execution](features/code-execution.md) e a [seção Terminal do README](features/tools.md) para detalhes de cada backend.

## Configurações de skills {#skill-settings}

Skills podem declarar suas próprias configurações via frontmatter do SKILL.md. São valores não secretos (paths, preferências, configurações de domínio) armazenados no namespace `skills.config` em `config.yaml`.

```yaml
skills:
  config:
    myplugin:
      path: ~/myplugin-data   # Exemplo — cada skill define suas próprias chaves
```

**Como as configurações de skills funcionam:**

- `hermes config migrate` escaneia todas as skills habilitadas, encontra configurações não definidas e oferece prompt
- `hermes config show` exibe todas as configurações de skills em "Skill Settings" com a skill a que pertencem
- Quando uma skill carrega, seus valores de config resolvidos são injetados automaticamente no contexto da skill

**Definindo valores manualmente:**

```bash
hermes config set skills.config.myplugin.path ~/myplugin-data
```

Para detalhes sobre declarar configurações nas suas próprias skills, veja [Creating Skills — Config Settings](/developer-guide/creating-skills#config-settings-configyaml).

### Proteção em writes de skills criadas pelo agente {#guard-on-agent-created-skill-writes}

Quando o agente usa `skill_manage` para criar, editar, fazer patch ou deletar uma skill, o Hermes pode opcionalmente escanear o conteúdo novo/atualizado em busca de padrões de palavras perigosos (captura de credenciais, prompt injection óbvio, instruções de exfil). O scanner está **desligado por padrão** — workflows reais de agente que legitimamente tocam `~/.ssh/` ou mencionam `$OPENAI_API_KEY` disparavam a heurística com frequência demais. Reative se quiser que o scanner peça confirmação antes dos writes de skill do agente:

```yaml
skills:
  guard_agent_created: true   # default: false
```

Quando ligado, qualquer write `skill_manage` sinalizado aparece como prompt de aprovação com a justificativa do scanner. Writes aceitos entram; writes negados retornam erro explicativo ao agente.

### Aprovação de escrita para skills {#write-approval-for-skill-writes}

Independente do scanner de conteúdo acima, `skills.write_approval` coloca **todo** write de skill do agente (create / edit / patch / delete / arquivos de suporte) atrás da sua aprovação explícita — o mesmo mecanismo approve/deny de comandos perigosos:

```yaml
skills:
  write_approval: false   # false = escrever livremente (default) | true = preparar todo write para revisão
```

Quando ligado, writes de skills ficam em staging em `~/.hermes/pending/skills/` e são revisados com `/skills pending`, `/skills diff <id>`, `/skills approve <id>`, `/skills reject <id>` — da CLI ou qualquer plataforma de messaging. Alterne em runtime com `/skills approval on|off`. Memória tem o mesmo gate (`memory.write_approval`, abaixo). Walkthrough completo: [Gating agent skill writes](/user-guide/features/skills#gating-agent-skill-writes-skillswrite_approval).

## Configuração de memória {#memory-configuration}

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200   # ~800 tokens
  user_char_limit: 1375     # ~500 tokens
  write_approval: false     # true = exigir aprovação antes de qualquer write de memória
```

Com `memory.write_approval: true`, writes de memória precisam da sua aprovação antes de entrar: turnos interativos na CLI pedem inline; sessões de messaging e a revisão de auto-melhoria em background colocam o write em staging para revisão `/memory pending` → `/memory approve <id>` / `/memory reject <id>`. Alterne em runtime com `/memory approval on|off`. Veja [Controlling memory writes](/user-guide/features/memory#controlling-memory-writes-write_approval).

## Truncamento de arquivos de contexto {#context-file-truncation}

Controla quanto conteúdo o Hermes carrega de cada arquivo de contexto automático antes de aplicar truncamento head/tail. Aplica-se a arquivos injetados no system prompt como `SOUL.md`, `.hermes.md`, `AGENTS.md`, `CLAUDE.md` e `.cursorrules`. **Não** afeta a ferramenta `read_file`.

```yaml
context_file_max_chars: null  # default — cap dinâmico escalado à janela de contexto do modelo (piso 20K, teto 500K chars)
```

Defina um inteiro positivo para fixar um cap fixo em vez do comportamento dinâmico:

```yaml
context_file_max_chars: 25000
```

## Segurança de leitura de arquivos {#file-read-safety}

Controla quanto conteúdo uma única chamada `read_file` pode retornar. Leituras que excedem o limite são rejeitadas com erro instruindo o agente a usar `offset` e `limit` para um intervalo menor. Isso impede que uma única leitura de bundle JS minificado ou arquivo de dados grande inundem a janela de contexto.

```yaml
file_read_max_chars: 100000  # default — ~25-35K tokens
```

Aumente se você usa um modelo com janela de contexto grande e lê arquivos grandes com frequência. Diminua para modelos de contexto pequeno para manter leituras eficientes:

```yaml
# Modelo de contexto grande (200K+)
file_read_max_chars: 200000

# Modelo local pequeno (16K context)
file_read_max_chars: 30000
```

O agente também deduplica leituras de arquivo automaticamente — se a mesma região do arquivo é lida duas vezes e o arquivo não mudou, um stub leve é retornado em vez de reenviar o conteúdo. Isso reseta na compressão de contexto para o agente poder reler arquivos após o conteúdo ser resumido.

## Limites de truncamento de saída de ferramentas {#tool-output-truncation-limits}

Três caps relacionados controlam quanta saída bruta uma ferramenta pode retornar antes do Hermes truncar:

```yaml
tool_output:
  max_bytes: 50000        # cap de saída do terminal (chars)
  max_lines: 2000         # cap de paginação do read_file
  max_line_length: 2000   # cap por linha na view numerada do read_file
```

- **`max_bytes`** — Quando um comando `terminal` produz mais que este número de caracteres combinados de stdout/stderr, o Hermes mantém os primeiros 40% e últimos 60% e insere aviso `[OUTPUT TRUNCATED]` entre eles. Default `50000` (≈12-15K tokens em tokenisers típicos).
- **`max_lines`** — Limite superior no parâmetro `limit` de uma única chamada `read_file`. Requisições acima disso são limitadas para uma leitura não inundar a janela de contexto. Default `2000`.
- **`max_line_length`** — Cap por linha aplicado quando `read_file` emite a view numerada. Linhas mais longas são truncadas para este número de chars seguido de `... [truncated]`. Default `2000`.

Aumente os limites em modelos com janelas de contexto grandes que podem absorver mais saída bruta por chamada. Diminua para modelos de contexto pequeno para manter resultados de ferramentas compactos:

```yaml
# Modelo de contexto grande (200K+)
tool_output:
  max_bytes: 150000
  max_lines: 5000

# Modelo local pequeno (16K context)
tool_output:
  max_bytes: 20000
  max_lines: 500
```

## Desabilitação global de toolsets {#global-toolset-disable}

Para suprimir toolsets específicos na CLI e em toda plataforma de gateway em um
único lugar, liste seus nomes em `agent.disabled_toolsets`:

```yaml
agent:
  disabled_toolsets:
    - memory       # ocultar ferramentas de memória + injeção MEMORY_GUIDANCE
    - web          # sem web_search / web_extract em lugar nenhum
```

Isso se aplica **depois** da config de ferramentas por plataforma (`platform_toolsets` escrita por
`hermes tools`), então um toolset listado aqui é sempre removido — mesmo se a
config salva de uma plataforma ainda o listar. Use quando quer um único
switch para "desligar X em todo lugar" em vez de editar 15+ linhas de plataforma na
UI `hermes tools`.

Deixar a lista vazia, ou omitir a chave, é no-op.

## Isolamento de git worktree {#git-worktree-isolation}

Habilite git worktrees isolados para rodar múltiplos agentes em paralelo no mesmo repo:

```yaml
worktree: true    # Sempre criar worktree (igual a hermes -w)
# worktree: false # Default — apenas quando a flag -w é passada
```

Quando habilitado, cada sessão CLI cria um worktree novo em `.worktrees/` com sua própria branch. Agentes podem editar arquivos, commitar, push e criar PRs sem interferir uns nos outros. Worktrees limpos são removidos ao sair; sujos são mantidos para recuperação manual.

Por padrão o novo worktree faz branch da **ponta remota recém-fetchada** (upstream da branch atual, senão a branch default do remote) para começar atualizado com o projeto em vez do `HEAD` possivelmente desatualizado do clone local. Isso mantém o diff de um PR limitado à mudança real em vez de herdar o quanto o clone local estava atrás. Defina `worktree_sync: false` para fazer branch do `HEAD` local — útil offline, ou quando você quer deliberadamente o state exato atual do clone como base. Se o remote não puder ser alcançado, faz fallback automático para `HEAD` local.

```yaml
worktree_sync: true    # Default — branch da ponta remota fetchada
# worktree_sync: false # Branch do HEAD local (offline / base fixada)
```

Você também pode listar arquivos gitignored para copiar em worktrees via `.worktreeinclude` na raiz do repo:

```
# .worktreeinclude
.env
.venv/
node_modules/
```

## Compressão de contexto {#context-compression}

O Hermes comprime automaticamente conversas longas para permanecer dentro da janela de contexto do seu modelo. O summarizer de compressão é uma chamada LLM separada — você pode apontá-la para qualquer provider ou endpoint.

Todas as configurações de compressão ficam em `config.yaml` (sem variáveis de ambiente).

### Referência completa {#full-reference}

```yaml
compression:
  enabled: true                                     # Ligar/desligar compressão
  progress_notices: false                           # Opt-in: entregar avisos rotineiros de progresso de compressão a plataformas de chat — veja abaixo
  threshold: 0.50                                   # Comprimir neste % do limite de contexto
  threshold_tokens: null                            # Cap absoluto de tokens (opcional) — usa o menor entre ratio vs absoluto
  target_ratio: 0.20                                # Fração do threshold a preservar como tail recente
  protect_last_n: 20                                # Mín. mensagens recentes a manter não comprimidas
  protect_first_n: 3                                # Mensagens head não-system fixadas em compactações (0 = não fixar nada)
  in_place: true                                    # Compactar no mesmo session id (sem rotação) — veja abaixo
  idle_compact_after_seconds: 0                     # Compactação idle opt-in (0 = desabilitado) — veja abaixo
  hygiene_hard_message_limit: 5000                  # Válvula de segurança do gateway — veja abaixo
  hygiene_timeout_seconds: 30                       # Máx. segundos SEM saída do modelo de summary antes de cortar compressão hygiene
  hygiene_total_ceiling_seconds: 600                # Cap absoluto na espera hygiene mesmo com tokens ainda em stream
  hygiene_failure_cooldown_seconds: 300             # Pular tentativas hygiene falhas repetidas para esta sessão
  context_timeout_seconds: 120                      # Orçamento de inatividade para compress_context in-agent (loop /compress / preflight) — veja abaixo
  context_total_ceiling_seconds: 600                # Cap absoluto na espera in-agent *pré-commit* de compress_context mesmo com tokens em stream (commit SessionDB já iniciado nunca é abandonado; overruns são logados + expostos)
  proactive_prune_tokens: 0                         # Trigger opt-in de tokens para prune no-LLM de tool-result (0 = off; veja abaixo)
  proactive_prune_min_result_chars: 8000            # Pass de summarize do prune só toca tool results maiores que isto (limitado >= 200)
  proactive_prune_min_reclaim_tokens: 4096          # Prune só faz commit quando recupera pelo menos tantos tokens (0 = commit any)

# O modelo/provider de summarização é configurado em auxiliary:
auxiliary:
  compression:
    model: ""                                       # Vazio = usar modelo de chat principal. Override ex.: "google/gemini-3-flash-preview" para compressão mais barata/rápida.
    provider: "auto"                                # Provider: "auto", "openrouter", "nous", "codex", "main", etc.
    base_url: null                                  # Endpoint OpenAI-compatible customizado (sobrescreve provider)
```

:::info Migração de config legada
Configs antigas com `compression.summary_model`, `compression.summary_provider` e `compression.summary_base_url` são migradas automaticamente para `auxiliary.compression.*` no primeiro load (config version 17). Nenhuma ação manual necessária.
:::

`progress_notices` (default `false`) controla se status de progresso **rotineiros** de compressão chegam a plataformas de chat (Telegram, Discord, Slack, etc.). Por design, compressão automática é silenciosa em superfícies de chat — roda em background com logging apenas no servidor. Defina `progress_notices: true` para optar por ver o ciclo de vida rotineiro em plataformas de chat: aviso inicial "Compacting context…", triggers de compressão preflight/pré-API, compactação idle, progresso de retry ("Compressed 30 → 12 messages, retrying…") e aviso "Context compaction complete". O gate é limitado a status de compressão — ruído operacional não relacionado (falhas de modelo auxiliar, chatter de rate-limit/retry de provider) permanece suprimido de qualquer forma. Avisos de **falha** de compressão e feedback manual de `/compress` são sempre visíveis independentemente desta configuração. Editar este valor em um gateway rodando entra em vigor na próxima mensagem.

`hygiene_hard_message_limit` é uma **válvula de segurança pré-compressão** somente do gateway. Existe para quebrar uma espiral de morte: quando chamadas de API continuam desconectando em sessão oversized, o gateway nunca recebe dados de uso de tokens, então o threshold baseado em tokens não dispara, o transcript continua crescendo e desconexões pioram. Este piso baseado em contagem dispara apenas na contagem de mensagens (sempre conhecida, independente de falhas de API) para forçar compressão e recuperar a sessão. Default `5000` — bem acima de qualquer sessão normal, incluindo modelos de contexto grande (1M+) fazendo milhares de turnos curtos, que comprimem no threshold de tokens muito antes disso. Aumente para plataformas incomuns, diminua para forçar compressão mais agressiva. Editar este valor em gateway rodando entra em vigor na próxima mensagem (veja abaixo).

`hygiene_timeout_seconds` é o **orçamento de inatividade** do gateway para este pass de compressão pré-agente — não um cap total de wall-clock. A chamada de summary de compressão faz stream do modelo, e cada token que chega conta como progresso: um modelo de raciocínio lento que ainda está gerando continua estendendo seu próprio deadline, então modelos de summary lentos mas saudáveis nunca são cortados no meio da geração. Apenas quando o modelo de summary produz **nenhuma saída** por este número de segundos (backend down, conexão travada, provider silencioso) o gateway avisa o usuário, continua a mensagem entrante sem compressão e registra cooldown temporário de falha por sessão em vez de parecer travado.

`hygiene_total_ceiling_seconds` (default `600`) limita a espera total mesmo com tokens ainda fluindo, para um stream degenerado de gotejamento não prender um turno indefinidamente. É limitado a no mínimo `hygiene_timeout_seconds`.

`hygiene_failure_cooldown_seconds` controla esse cooldown por sessão após timeout ou abort de compressão hygiene. Durante o cooldown, o gateway pula tentativas hygiene repetidas para a mesma sessão oversized para que toda mensagem entrante não bloqueie no mesmo backend auxiliar quebrado. `/compress`, `/reset` ou um turno saudável posterior ainda podem recuperar a sessão.

`context_timeout_seconds` (default `120`) é o mesmo **orçamento de inatividade** para `compress_context` in-agent — loop de conversa, compactação preflight e `/compress` manual — para que um modelo de summary travado não paralise uma sessão indefinidamente. Tokens de summary em stream estendem a espera; apenas um worker silencioso é cortado. No timeout o Hermes pula compactação, mantém as mensagens existentes e avisa o usuário. Defina `0` para desabilitar. Hygiene de sessão do gateway mantém seu próprio caminho `hygiene_timeout_seconds` e não é envolvido duas vezes.

`context_total_ceiling_seconds` (default `600`) limita a espera in-agent **pré-commit** (fase summary / stream) mesmo com tokens ainda fluindo. É limitado a no mínimo `context_timeout_seconds`. A garantia exata: **a fase de summary é limitada por este teto; a fase de commit é logada e exposta se excedê-lo.** Uma vez que o worker entrou na fence de commit de compressão e a mutação SessionDB está em flight, o commit nunca é abandonado no meio — isso arriscaria divergência de transcript — mas a espera deixa de ser silenciosa: se o commit passar do teto, o Hermes loga o overrun (WARNING, escalando para ERROR em repetição), envia aviso one-shot pelo canal de warning visível ao usuário e continua esperando em incrementos limitados até o commit completar.

`protect_first_n` controla quantas mensagens head **não-system** são fixadas em toda compactação. Default `3` — a troca inicial user/assistant sobrevive a todo pass do summarizer para o objetivo original permanecer visível. Em sessões longas de compactação rolling onde o turno inicial não é mais relevante, defina `protect_first_n: 0` para não fixar nada além de system prompt + summary + tail. O system prompt em si é sempre preservado independentemente desta configuração.

`in_place` (default `true`) controla o que acontece com a identidade da sessão quando a compactação dispara. Quando `true`, a compactação reescreve a lista de mensagens e reconstrói o system prompt **sem rotacionar o session id** — a conversa mantém um id durável por toda a vida (sem cadeia `parent_session_id`, sem renumeração `name #2` / `#3` em listas de sessão). Compactação é não-destrutiva: o contexto vivo é compactado, mas os turnos pré-compactação são soft-archived sob o mesmo id (marcados inactive/compacted) — ainda pesquisáveis via `session_search` e recuperáveis, não deletados. Hooks veem o modo via o campo `in_place` no evento `session:compress`. Defina `in_place: false` para restaurar o comportamento legado onde cada compactação rotaciona para novo session id ligado ao antigo.

`threshold_tokens` define um **cap absoluto de tokens** opcional para o trigger de compressão. Quando definido, compressão dispara no menor entre o `threshold` baseado em ratio e esta contagem absoluta — então compressão nunca dispara depois do número de tokens preferido do usuário independentemente do modelo ativo. Isso resolve o problema de trocar entre modelos com janelas de contexto diferentes (ex.: 1M → 400K) deslocando o ponto de trigger absoluto. O cap é limitado ao context length do modelo, então defini-lo acima do que o modelo suporta é seguro — o threshold baseado em ratio é usado. Default `null` (desabilitado — apenas threshold baseado em ratio). O cap sobrevive trocas de modelo e ativações de fallback.

`idle_compact_after_seconds` é um trigger **opt-in, baseado em tempo** que complementa o `threshold` baseado em tamanho. Default `0` (desabilitado). Quando definido acima de 0, uma sessão que retoma após pelo menos tantos segundos de inatividade compacta seu histórico acumulado antecipadamente, antes da primeira resposta — para que um thread longo (ex.: conversa Telegram que você retoma horas depois) não releia todo o contexto stale a cada turno subsequente. Nunca dispara quando o contexto já está no ou abaixo do alvo pós-compressão (`threshold × target_ratio`), e honra os mesmos guards de failure-cooldown, anti-thrash e lock por sessão de toda compactação automática. Exemplo: `idle_compact_after_seconds: 1800` compacta após 30 minutos idle.

`proactive_prune_tokens` habilita um prune determinístico, sem LLM, de payloads antigos de tool-result que roda independentemente do `threshold`. Em modelos de janela grande a compactação `threshold` (≈50% da janela) raramente dispara, então saídas volumosas de ferramentas (dumps de terminal, leituras de arquivo, web extracts) viajam no histórico e são reenviadas a cada turno subsequente. Quando histórico reenviado excede `proactive_prune_tokens` (default `0` = off; tente `48000` para habilitar), o prune deduplica resultados idênticos, resume os oversized mais antigos e trunca argumentos grandes de tool-call — protegendo as `protect_last_n` mensagens mais recentes e nunca chamando o modelo. Saídas completas permanecem recuperáveis do session store. `proactive_prune_min_result_chars` (default `8000`, limitado a ≥ 200) define o tamanho abaixo do qual um tool result fica intacto. `proactive_prune_min_reclaim_tokens` (default `4096`) impede que um prune faça commit a menos que recupere pelo menos tantos tokens — um prune committed reescreve histórico já enviado e invalida o prefixo de prompt-cache do provider, então este gate mantém essas quebras de cache episódicas e amortizadas (uma quebra significativa, como limite de compressão) em vez de disparar a cada iteração de ferramenta. Roda apenas sob o engine `compressor` embutido; outros context engines herdam no-op.

:::tip Hot-reload de compressão e context length no gateway
A partir de releases recentes, editar `model.context_length` ou qualquer chave `compression.*` em `config.yaml` em um gateway rodando entra em vigor na próxima mensagem — sem restart do gateway, sem `/reset`, sem rotação de sessão. A assinatura do cached-agent inclui essas chaves, então o gateway reconstrói o agente transparentemente quando vê mudança. Chaves de API e config de tool/skill ainda exigem os caminhos de reload habituais.
:::

### Configurações comuns {#common-setups}

**Default (auto-detect) — nenhuma configuração necessária:**
```yaml
compression:
  enabled: true
  threshold: 0.50
```
Usa seu provider principal e modelo principal. Override por task (ex.: `auxiliary.compression.provider: openrouter` + `model: google/gemini-2.5-flash`) se quiser compressão em modelo mais barato que seu modelo de chat principal.

**Forçar um provider específico** (OAuth ou baseado em API key):
```yaml
auxiliary:
  compression:
    provider: nous
    model: gemini-3-flash
```
Funciona com qualquer provider: `nous`, `openrouter`, `codex`, `anthropic`, `main`, etc.

**Endpoint customizado** (self-hosted, Ollama, zai, DeepSeek, etc.):
```yaml
auxiliary:
  compression:
    model: glm-4.7
    base_url: https://api.z.ai/api/coding/paas/v4
```
Aponta para endpoint OpenAI-compatible customizado. Usa `OPENAI_API_KEY` para auth.

### Como os três knobs interagem {#how-the-three-knobs-interact}

| `auxiliary.compression.provider` | `auxiliary.compression.base_url` | Result |
|---------------------|---------------------|--------|
| `auto` (default) | not set | Auto-detect do melhor provider disponível |
| `nous` / `openrouter` / etc. | not set | Força aquele provider, usa sua auth |
| any | set | Usa o endpoint customizado diretamente (provider ignorado) |

:::warning Requisito de context length do modelo de summary
O modelo de summary **deve** ter janela de contexto pelo menos tão grande quanto a do seu modelo principal. O compressor envia a seção central completa da conversa ao modelo de summary — se a janela de contexto desse modelo for menor que a do modelo principal, a chamada de summarização falhará com erro de context length. Quando isso acontece, os turnos centrais são **descartados sem summary**, perdendo contexto da conversa silenciosamente. Se você sobrescrever o modelo, verifique se seu context length atende ou excede o do modelo principal.
:::

## Watchdog de sessão travada {#session-stall-watchdog}

O gateway roda um watchdog de sessão travada notify-only (`agent.session_stall_timeout`, default `300` segundos, `0` = desabilitado). Quando uma sessão ocupada tem **follow-up inbound pendente** e o relógio de atividade compartilhado do agente ficou idle por pelo menos este tempo, o gateway registra WARNING e envia notificação one-shot ao usuário:

```
⚠️ Agent session appears stalled (last activity N min ago). Try /new to reset.
```

Semântica:

- **Somente notificação.** O watchdog nunca mata o turno — contrasta com `agent.gateway_timeout`, que cancela uma execução após inatividade prolongada. O aviso de stall apenas informa que o agente parece travado para você decidir (`/new`, `/stop` ou continuar esperando).
- **Uma notificação por episódio de stall.** O latch limpa quando o inbound pendente drena ou a atividade retoma, então uma sessão que se recupera e trava de novo notifica novamente.
- Progresso vem apenas do snapshot de atividade compartilhado (tool calls, progresso de stream de API, heartbeats de compressão). Inbound pendente é gate de notificação, não relógio de progresso.

```yaml
agent:
  session_stall_timeout: 300   # segundos; 0 desabilita o watchdog
```

## Context engine {#context-engine}

O context engine controla como conversas são gerenciadas ao se aproximar do limite de tokens do modelo. O engine `compressor` embutido usa summarização lossy (veja [Context Compression](/developer-guide/context-compression-and-caching)). Plugin engines podem substituí-lo por estratégias alternativas.

```yaml
context:
  engine: "compressor"    # default — summarização lossy embutida
```

Para usar um plugin engine (ex.: LCM para gerenciamento lossless de contexto):

```yaml
context:
  engine: "lcm"          # deve corresponder ao nome do plugin
```

Plugin engines **nunca são auto-ativados** — você deve definir explicitamente `context.engine` para o nome do plugin. Engines disponíveis podem ser navegados e selecionados via `hermes plugins` → Provider Plugins → Context Engine.

Veja [Memory Providers](/user-guide/features/memory-providers) para o sistema análogo de seleção única para plugins de memória.

## Orçamento de iterações {#iteration-budget}

Quando o agente trabalha em uma tarefa complexa com muitas tool calls, pode esgotar seu orçamento de iterações (default: 500 turnos). O Hermes **não** injeta avisos de pressão no meio da tarefa — builds anteriores avisavam o modelo em 70%/90% do budget, o que fazia modelos abandonarem tarefas complexas prematuramente e foi removido em abril de 2026.

Em vez disso, quando o budget é realmente esgotado (500/500), o Hermes injeta uma mensagem pedindo ao modelo para encerrar e permite uma única **grace call** para entregar resposta final. Se essa grace call ainda não produzir texto, o agente é pedido para resumir o que realizou.

```yaml
agent:
  max_turns: 500               # Máx. iterações por turno de conversa (default: 500)
  api_max_retries: 3           # Retries por provider antes do fallback (default: 3)
```

Quando o orçamento de iterações esgota completamente, a CLI mostra notificação ao usuário: `⚠ Iteration budget reached (500/500) — response may be incomplete`.

`agent.api_max_retries` controla quantas vezes o Hermes retenta uma chamada de API do provider em erros transitórios (rate limits, quedas de conexão, 5xx) **antes** do fallback-provider. O default é `3` — quatro tentativas no total. Se você tem [fallback providers](/user-guide/features/fallback-providers) configurados e quer fail over mais rápido, reduza para `0` para o primeiro erro transitório no primary passar imediatamente ao fallback em vez de churn de retries no endpoint instável.

## Verify-on-Stop (verificação de código) {#verify-on-stop-coding-verification}

Quando habilitado, o Hermes recusa aceitar resposta final em um turno onde o agente editou código em um workspace mas não produziu evidência fresca de verificação (test run passando, build, lint, etc.) — injeta follow-up sintético pedindo ao agente para verificar ou explicar por que não pode. Edições apenas de doc/markdown/skill nunca disparam, e o loop é limitado para nunca prender o agente.

```yaml
agent:
  verify_on_stop: false        # true | false | "auto" (consciente de superfície: on para CLI/TUI/desktop, off para messaging)
  verify_guidance: true        # Anexar guidance de creative-UI / clean-diff ao nudge de evidência ausente
  max_verify_nudges: 3         # Cap de nudges consecutivos de continuação por turno (built-in + hooks pre_verify)
  coding_instructions: ""      # Regras de código permanentes do projeto anexadas ao coding brief
```

`verify_on_stop` aceita `true` (ligado em todo lugar), `false` (desligado) ou `"auto"` (ligado para superfícies interativas de código — CLI, TUI, desktop — e callers programáticos; desligado para superfícies de messaging como Telegram/Discord onde a narrativa de verificação soa como ruído de chat). A migração de config desliga **off** em instalações existentes, então trate off como default efetivo e opte explicitamente. A env var `HERMES_VERIFY_ON_STOP` sobrescreve o valor de config quando definida.

Para um gate de política user/plugin no mesmo ponto — manter o agente rodando com suas próprias verificações — veja o hook [`pre_verify`](/user-guide/features/hooks#pre_verify).

## Metas permanentes (`/goal`) {#standing-goals-goal}

Quando uma meta permanente está ativa, o Hermes julga se cada resposta do assistant a satisfaz. Se não, alimenta um prompt de continuação de volta na mesma sessão e continua trabalhando até a meta terminar, o turn budget esgotar ou o usuário pausar/limpar. O turn budget é o backstop real — falhas do judge falham **open** (continuam) para um judge instável nunca prender o progresso.

```yaml
goals:
  max_turns: 20   # Máx. turnos de continuação antes do Hermes auto-pausar a meta (default: 20)
```

`max_turns` limita quantos turnos de continuação uma meta pode conduzir antes do Hermes auto-pausá-la e pedir ao usuário `/goal resume`. Protege contra falsos negativos do judge (meta na verdade concluída mas judge diz continuar) e gasto ilimitado de modelo em metas fuzzy ou inatingíveis. Veja [Goals](/user-guide/features/goals) para o recurso completo.

### Timeouts de API {#api-timeouts}

O Hermes tem camadas de timeout separadas para streaming, mais um detector stale para chamadas não-streaming. Os detectores stale auto-ajustam para providers locais apenas quando deixados nos defaults implícitos.

| Timeout | Default | Providers locais | Config / env |
|---------|---------|----------------|--------------|
| Socket read timeout | 120s | Auto-aumentado para 1800s | `HERMES_STREAM_READ_TIMEOUT` |
| Detecção de stream stale | 180s | Aumentado para teto de 900s (`agent.local_stream_stale_timeout`) | `HERMES_STREAM_STALE_TIMEOUT` |
| Detecção non-stream stale | 90s | Auto-desabilitado quando implícito | `providers.<id>.stale_timeout_seconds` ou `HERMES_API_CALL_STALE_TIMEOUT` |
| Chamada de API (non-streaming) | 1800s | Inalterado | `providers.<id>.request_timeout_seconds` / `timeout_seconds` ou `HERMES_API_TIMEOUT` |

O **socket read timeout** controla quanto tempo o httpx espera pelo próximo chunk de dados do provider. LLMs locais podem levar minutos de prefill em contextos grandes antes do primeiro token, então o Hermes aumenta para 30 minutos quando detecta endpoint local. Se você definir explicitamente `HERMES_STREAM_READ_TIMEOUT`, esse valor é sempre usado independentemente da detecção de endpoint.

A **detecção de stream stale** mata conexões que recebem pings SSE keep-alive mas nenhum conteúdo real. Para providers locais (que não enviam keep-alive pings durante prefill) o default é aumentado para teto finito de 900 segundos em vez da base de 180s — configurável via `agent.local_stream_stale_timeout` ou env var `HERMES_LOCAL_STREAM_STALE_TIMEOUT`.

A **detecção non-stream stale** mata chamadas não-streaming que não produzem resposta por tempo demais. Por padrão o Hermes desabilita isso em endpoints locais para evitar falsos positivos durante prefills longos. Se você definir explicitamente `providers.<id>.stale_timeout_seconds`, `providers.<id>.models.<model>.stale_timeout_seconds` ou `HERMES_API_CALL_STALE_TIMEOUT`, esse valor explícito é respeitado mesmo em endpoints locais.

## Avisos de pressão de contexto {#context-pressure-warnings}

Separado da pressão de orçamento de iterações, a pressão de contexto rastreia quão perto a conversa está do **threshold de compactação** — o ponto onde a compressão de contexto dispara para resumir mensagens antigas. Isso ajuda você e o agente a entender quando a conversa está ficando longa.

| Progresso | Nível | O que acontece |
|----------|-------|-------------|
| **≥ 60%** do threshold | Info | CLI mostra barra de progresso ciano; gateway envia aviso informativo |
| **≥ 85%** do threshold | Warning | CLI mostra barra amarela em negrito; gateway avisa que compactação é iminente |

Na CLI, pressão de contexto aparece como barra de progresso no feed de saída de ferramentas:

```
  ◐ context ████████████░░░░░░░░ 62% to compaction  48k threshold (50%) · approaching compaction
```

Em plataformas de messaging, uma notificação em texto simples é enviada:

```
◐ Context: ████████████░░░░░░░░ 62% to compaction (threshold: 50% of window).
```

Se auto-compressão está desabilitada, o aviso informa que o contexto pode ser truncado.

Pressão de contexto é automática — nenhuma configuração necessária. Dispara puramente como notificação ao usuário e não modifica o stream de mensagens nem injeta nada no contexto do modelo.

## Estratégias de credential pool {#credential-pool-strategies}

Quando você tem múltiplas chaves de API ou tokens OAuth para o mesmo provider, configure a estratégia de rotação:

```yaml
credential_pool_strategies:
  openrouter: round_robin    # alternar chaves uniformemente
  anthropic: least_used      # sempre escolher a chave menos usada
```

Opções: `fill_first` (default), `round_robin`, `least_used`, `random`. Veja [Credential Pools](/user-guide/features/credential-pools) para documentação completa.

## Prompt caching {#prompt-caching}

O Hermes liga prompt caching cross-session automaticamente quando o provider ativo suporta — nenhuma config do usuário necessária.

Para Claude em **Anthropic nativo**, **OpenRouter** e **Nous Portal**, o Hermes anexa breakpoints `cache_control` com TTL de 1 hora (`ttl: "1h"`) no system prompt e blocos de skills. O primeiro envio dentro de uma hora fresca paga tarifas completas de input; envios subsequentes em qualquer sessão na mesma hora puxam do cache na tarifa reduzida de cached-read. Isso significa que system prompt, conteúdo de skills carregado e a porção inicial de qualquer include de contexto longo são reutilizados entre sessões `hermes` e entre subagentes forked na primeira hora.

O upstream Qwen Cloud (Alibaba DashScope) limita cache TTL a 5 minutos, então o Hermes usa TTL de breakpoint de 5 minutos lá. Outros caminhos Claude via terceiros (AWS Bedrock, Azure Foundry) fazem fallback aos defaults de caching do provider. xAI Grok usa mecanismo separado de conversation-id fixado por sessão — veja [xAI prompt caching](/integrations/providers#xai-grok--responses-api--prompt-caching).

Não existe knob para desabilitar — caching é always-on e economiza dinheiro mesmo em conversas de um turno porque o system prompt sozinho é fração significativa da contagem de tokens de input.

O único knob explícito é o tier de cache TTL que o Hermes solicita em breakpoints estilo Anthropic:

```yaml
prompt_caching:
  cache_ttl: "5m"   # "5m" ou "1h" (tiers suportados pela Anthropic); outros valores são ignorados
```

`cache_ttl` seleciona o TTL de breakpoint que o Hermes anexa para Claude via API Anthropic nativa, OpenRouter e Nous Portal. Apenas os dois tiers suportados pela Anthropic (`"5m"`, `"1h"`) são respeitados — qualquer outro valor é ignorado. Providers com seus próprios caps (ex.: Qwen Cloud, que limita a 5 minutos) ainda limitam ao que o upstream permite.

## Modelos auxiliares {#auxiliary-models}

O Hermes usa modelos "auxiliares" para tarefas laterais como análise de imagem, summarização de páginas web, análise de screenshots do browser, geração de títulos de sessão e compressão de contexto. Por padrão (`auxiliary.*.provider: "auto"`), o Hermes encaminha toda tarefa auxiliar para seu **modelo de chat principal** — o mesmo provider/modelo que você escolheu em `hermes model`. Você não precisa configurar nada para começar, mas saiba que em modelos de raciocínio caros (Opus, MiniMax M2.7, etc.) tarefas auxiliares adicionam custo significativo. Se quer tarefas laterais baratas e rápidas independentemente do modelo principal, defina `auxiliary.<task>.provider` e `auxiliary.<task>.model` explicitamente (por exemplo, Gemini Flash no OpenRouter para vision e web extraction).

:::note Por que "auto" usa seu modelo principal
Builds anteriores separavam usuários de agregadores (OpenRouter, Nous Portal) para um default barato no lado do provider. Isso surpreendia — usuários que pagavam assinatura de agregador viam um modelo diferente lidando com seu tráfego auxiliar. `auto` agora usa o modelo principal para todos, e overrides por task em `config.yaml` ainda prevalecem (veja [Full auxiliary config reference](#full-auxiliary-config-reference) abaixo).
:::

### Configurar modelos auxiliares interativamente {#configuring-auxiliary-models-interactively}

Em vez de editar YAML manualmente, execute `hermes model` e escolha **"Configurar modelos auxiliares"** no menu. Você recebe um picker interativo por task:

```
$ hermes model
→ Configurar modelos auxiliares

[ ] vision               atualmente: auto / main model
[ ] web_extract          atualmente: auto / main model
[ ] title_generation     atualmente: openrouter / google/gemini-3-flash-preview
[ ] tts_audio_tags       atualmente: auto / main model
[ ] compression          atualmente: auto / main model
[ ] approval             atualmente: auto / main model
[ ] triage_specifier     atualmente: auto / main model
[ ] kanban_decomposer    atualmente: auto / main model
[ ] profile_describer    atualmente: auto / main model
```

Selecione uma task, escolha um provider (fluxos OAuth abrem browser; providers com API key pedem), escolha um modelo. A mudança persiste em `auxiliary.<task>.*` no `config.yaml`. Mesma maquinaria do picker de modelo principal — nenhuma sintaxe extra para aprender.

Se não quer que o Hermes auto-gere títulos após a primeira troca, defina
`auxiliary.title_generation.enabled: false`. Títulos manuais ainda funcionam via
`/title` e `hermes sessions rename`.

### Endpoints somente stream {#stream-only-endpoints}

Alguns endpoints OpenAI-compatible rejeitam requisições de chat não-streaming de imediato (ex.: Tencent Copilot retorna HTTP 400 `"Non-stream chat request is currently not supported"`). Chat interativo já faz stream, mas tarefas auxiliares (title generation, compression, web extraction) usam chamadas não-streaming e falhariam em toda tentativa. O Hermes sempre trata `copilot.tencent.com` como stream-only; para qualquer outro endpoint assim, liste um substring de URL em `auxiliary.stream_only_base_urls`:

```yaml
auxiliary:
  stream_only_base_urls:
    - "my-stream-only-proxy.example.com"
```

Chamadas auxiliares correspondentes são enviadas com `stream=True` e os chunks (incluindo deltas de tool-call) são agregados client-side — nenhuma mudança de comportamento para outros endpoints.

### Tutorial em vídeo {#video-tutorial}

<div style={{position: 'relative', width: '100%', aspectRatio: '16 / 9', marginBottom: '1.5rem'}}>
  <iframe
    src="https://www.youtube.com/embed/NoF-YajElIM"
    title="Hermes Agent — Tutorial de Modelos Auxiliares"
    style={{position: 'absolute', top: 0, left: 0, width: '100%', height: '100%', border: 0}}
    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
    allowFullScreen
  />
</div>

### O padrão universal de config {#the-universal-config-pattern}

Todo slot de modelo no Hermes — tarefas auxiliares, compression, fallback — usa os mesmos três knobs:

| Key | O que faz | Default |
|-----|-------------|---------|
| `provider` | Qual provider usar para auth e roteamento | `"auto"` |
| `model` | Qual modelo solicitar | default do provider |
| `base_url` | Endpoint OpenAI-compatible customizado (sobrescreve provider) | not set |

Blocos de tarefas auxiliares também aceitam knob `reasoning_effort`:

| Key | O que faz | Default |
|-----|-------------|---------|
| `reasoning_effort` | Nível de thinking para chamadas LLM daquela task: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra` | not set (default do provider) |

Este é o counterpart por task do `agent.reasoning_effort` global: rode compression em `low` ou vision em `none` para cortar latência e custo de tarefas laterais quando seu modelo principal é um modelo de raciocínio caro, sem tocar no comportamento de chat principal. Funciona em todo bloco de tarefa auxiliar (`vision`, `web_extract`, `compression`, `title_generation`, `curator`, `background_review`, ...), nos três formatos wire auxiliares (chat completions, Codex Responses, Anthropic Messages). Um `extra_body.reasoning` explícito na mesma task prevalece sobre o shorthand.

MoA é a exceção: profundidade de raciocínio para Mixture-of-Agents é configurada **por slot** no preset MoA (`moa.presets.<name>.reference_models[].reasoning_effort` / `aggregator.reasoning_effort`), não nos blocos auxiliares `moa_reference`/`moa_aggregator` — veja [Mixture of Agents](/user-guide/features/mixture-of-agents).

```yaml
auxiliary:
  compression:
    reasoning_effort: "low"    # summaries não precisam de thinking profundo
  vision:
    reasoning_effort: "none"   # desabilitar thinking para descrição de imagem
```

Quando `base_url` está definido, o Hermes ignora o provider e chama aquele endpoint diretamente (usando `api_key` ou `OPENAI_API_KEY` para auth). Quando apenas `provider` está definido, o Hermes usa auth e base URL embutidos daquele provider.

Providers disponíveis para tarefas auxiliares: `auto`, `main`, mais qualquer provider no [provider registry](/reference/environment-variables) — `openrouter`, `nous`, `openai-codex`, `copilot`, `copilot-acp`, `anthropic`, `gemini`, `qwen-oauth`, `zai`, `kimi-coding`, `kimi-coding-cn`, `minimax`, `minimax-cn`, `minimax-oauth`, `deepseek`, `nvidia`, `xai`, `xai-oauth`, `ollama-cloud`, `alibaba`, `bedrock`, `huggingface`, `arcee`, `xiaomi`, `kilocode`, `opencode-zen`, `opencode-go`, `ai-gateway`, `azure-foundry` — ou qualquer provider customizado nomeado do seu dict `providers:` (ex.: `provider: "beans"`).

:::tip MiniMax OAuth
`minimax-oauth` faz login via browser OAuth (sem API key). Execute `hermes model` e selecione **MiniMax (OAuth)** para autenticar. Tarefas auxiliares usam `MiniMax-M2.7-highspeed` automaticamente. Veja o [guia MiniMax OAuth](../guides/minimax-oauth.md).
:::

:::tip xAI Grok OAuth
`xai-oauth` faz login via browser OAuth para assinantes SuperGrok e X Premium+ (sem API key). Execute `hermes model` e selecione **xAI Grok OAuth (SuperGrok / Premium+)** para autenticar. O mesmo token OAuth é reutilizado em toda superfície direct-to-xAI (chat, tarefas auxiliares, TTS, image gen, video gen, transcription). Veja o [guia xAI Grok OAuth](../guides/xai-grok-oauth.md), e se o Hermes estiver em host remoto veja [OAuth over SSH / Remote Hosts](../guides/oauth-over-ssh.md).
:::

:::warning `"main"` é apenas para tarefas auxiliares
A opção de provider `"main"` significa "usar o provider que meu agente principal usa" — é válida apenas dentro de `auxiliary:`, `compression:` e entradas de fallback primário (`fallback_providers:` ou `fallback_model:` legado). **Não** é valor válido para sua configuração top-level `model.provider`. Se você usa endpoint OpenAI-compatible customizado, defina `provider: custom` na seção `model:`. Veja [AI Providers](/integrations/providers) para todas as opções de provider de modelo principal.
:::

### Referência completa de config auxiliar {#full-auxiliary-config-reference}

```yaml
auxiliary:
  # Análise de imagem (ferramenta vision_analyze + screenshots do browser)
  vision:
    provider: "auto"           # "auto", "openrouter", "nous", "codex", "main", etc.
    model: ""                  # ex.: "openai/gpt-4o", "google/gemini-2.5-flash"
    base_url: ""               # Endpoint OpenAI-compatible customizado (sobrescreve provider)
    api_key: ""                # API key para base_url (fallback para OPENAI_API_KEY)
    timeout: 120               # segundos — timeout de chamada LLM; payloads de visão precisam timeout generoso
    download_timeout: 30       # segundos — download HTTP de imagem; aumente para conexões lentas
    max_concurrency: 8         # máx. bursts concorrentes de encode/resize de imagem no processo
                               # (default: contagem de núcleos CPU do host, sem teto) — limita apenas o
                               # passo de encode CPU-bound para fan-out de frames de vídeo não saturar
                               # todos os núcleos e famintar o event loop; chamadas LLM permanecem totalmente
                               # concorrentes. Mínimo 1; valores < 1 são ignorados.

  # Summarização de página web + extração de texto de página do browser
  web_extract:
    provider: "auto"
    model: ""                  # e.g. "google/gemini-2.5-flash"
    base_url: ""
    api_key: ""
    timeout: 360               # segundos (6min) — summarização LLM por tentativa

  # Classificador de aprovação de comandos perigosos
  approval:
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30                # segundos

  # Inserção oculta de audio-tag TTS Gemini 3.1
  tts_audio_tags:
    provider: "auto"
    model: ""                  # vazio = modelo de chat principal
    base_url: ""
    api_key: ""
    timeout: 30

  # Timeout de compressão de contexto (separado de compression.* config)
  compression:
    timeout: 120               # segundos — compression summarizes long conversations, needs more time
    # fallback_chain:           # Opcional — providers a tentar em rate-limit / falha de conectividade
    #   - provider: nous
    #     model: deepseek/deepseek-chat
    #   - provider: openrouter
    #     model: google/gemini-2.5-flash
    #     base_url: ""
    #     api_key: ""
    # max_concurrency: 2       # Opcional: limitar chamadas LLM de compressão simultâneas para
                               # múltiplas sessões não empilharem retries em provider degradado

  # Títulos de sessão auto-gerados. Idioma vazio segue a conversa;
  # defina ex.: "English" ou "Japanese" para fixar títulos em um idioma.
  title_generation:
    enabled: true              # defina false para desabilitar auto-geração de títulos
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30
    language: ""

  # Skills hub — matching e busca de skills
  skills_hub:
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30

  # Dispatch de ferramentas MCP
  mcp:
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30

  # Títulos curtos de sessão auto-gerados após a primeira troca
  title_generation:
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 30
    # max_concurrency: 2       # Opcional: limitar chamadas simultâneas de title-generation

  # Kanban triage specifier — `hermes kanban specify <id>` (ou o
  # botão ✨ Specify do dashboard em cards da coluna Triage) usa este
  # slot para expandir one-liner em spec concreta e promover a
  # task para `todo`. Modelos baratos e rápidos funcionam bem; expansão de spec
  # é curta e não precisa de profundidade de raciocínio.
  triage_specifier:
    provider: "auto"
    model: ""
    base_url: ""
    api_key: ""
    timeout: 120
```

:::tip
Cada tarefa auxiliar tem `timeout` configurável (em segundos). Defaults: vision 120s, web_extract 360s, approval 30s, compression 120s. Aumente se usa modelos locais lentos para tarefas auxiliares. Vision também tem `download_timeout` separado (default 30s) para download HTTP de imagem — aumente para conexões lentas ou servidores de imagem self-hosted.
:::

:::info
Compressão de contexto tem seu próprio bloco `compression:` para thresholds e bloco `auxiliary.compression:` para configurações de model/provider — veja [Context Compression](#context-compression) acima. A cadeia de fallback primária usa lista top-level `fallback_providers:` — veja [Fallback Providers](/integrations/providers#fallback-providers). Os três seguem o mesmo padrão provider/model/base_url.
:::

### Cadeia de fallback por tarefa para tarefas auxiliares {#per-task-fallback-chain-for-auxiliary-tasks}

Cada tarefa auxiliar pode opcionalmente definir `fallback_chain` — lista de entradas provider/modelo que o Hermes tenta quando o provider auxiliar primário falha por rate limits, problemas de conectividade ou restrições de pagamento:

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

Quando o provider auxiliar primário (`openrouter` / `openai/gpt-4o-mini`) retorna rate-limit, timeout de conexão ou erro payment-required, o Hermes percorre `fallback_chain` em ordem. Pula entradas cujo provider corresponde ao provider já falho e tenta cada entrada restante até uma ter sucesso ou a cadeia esgotar. Se todos os fallbacks falham, o Hermes faz fallback para o modelo do agente principal como rede de segurança final.

Cada entrada suporta os mesmos três knobs de qualquer config de tarefa auxiliar:

| Chave | Descrição |
|-----|-------------|
| `provider` | Nome do provider (`nous`, `openrouter`, `anthropic`, `gemini`, `main`, etc.) |
| `model` | Nome do modelo para aquele provider |
| `base_url` | (Opcional) Endpoint OpenAI-compatible customizado |

`fallback_chain` está disponível em qualquer tarefa auxiliar — `compression`, `vision`, `web_extract`, `approval`, `skills_hub`, `mcp`, etc.

### Limitar concorrência auxiliar {#limiting-auxiliary-concurrency}

`max_concurrency` limita chamadas LLM in-flight para tarefas auxiliares como `compression` e `title_generation` em todo o processo. `auxiliary.vision.max_concurrency` é excluído: já controla apenas workers CPU-bound de encode/resize de imagem do vision, não requisições LLM. É mais útil quando:

- Muitas sessões podem spawnar trabalho em background simultaneamente (canais Discord/Telegram, múltiplos terminais)
- Seu provider está rate-limited ou passando por incidente e retries amplificariam o burst

O default é ilimitado. Um cap de segurança típico é `2`:

```yaml
auxiliary:
  title_generation:
    max_concurrency: 2
  compression:
    max_concurrency: 2
```

O semáforo envolve toda a chamada incluindo retries e fallbacks, então uma chamada lenta conta apenas uma vez para o limite.

### Roteamento OpenRouter e Pareto Code para tarefas auxiliares {#openrouter-routing--pareto-code-for-auxiliary-tasks}

Quando uma tarefa auxiliar resolve para OpenRouter (explicitamente ou via `provider: "main"` enquanto seu agente principal está no OpenRouter), as configurações `provider_routing` e `openrouter.min_coding_score` do agente principal **não propagam** — por design, cada tarefa auxiliar é independente. Para definir preferências de provider OpenRouter ou usar o [Pareto Code router](/integrations/providers#openrouter-pareto-code-router) para uma task aux específica, defina-as por task via `extra_body`:

```yaml
auxiliary:
  compression:
    provider: openrouter
    model: openrouter/pareto-code         # usar o Pareto Code router para esta task
    extra_body:
      provider:                            # prefs de roteamento de provider OpenRouter
        order: [anthropic, google]         # tentar estes providers em ordem
        sort: throughput                   # ou "price" | "latency"
        # only: [anthropic]                # restringir a um provider específico
        # ignore: [deepinfra]              # excluir providers específicos
      plugins:                             # knob do OpenRouter Pareto Code router
        - id: pareto-router
          min_coding_score: 0.5            # 0.0–1.0; maior = coders mais fortes
```

A forma espelha o que o OpenRouter aceita no body de requisição chat completions. O Hermes encaminha todo o `extra_body` verbatim, então qualquer outro campo de request-body OpenRouter documentado em [openrouter.ai/docs](https://openrouter.ai/docs) funciona da mesma forma.

### Alterar o modelo de visão {#changing-the-vision-model}

Para usar GPT-4o em vez de Gemini Flash para análise de imagem:

```yaml
auxiliary:
  vision:
    model: "openai/gpt-4o"
```

Ou via variável de ambiente (em `~/.hermes/.env`):

```bash
AUXILIARY_VISION_MODEL=openai/gpt-4o
```

### Opções de provider {#provider-options}

Estas opções aplicam-se a **configs de tarefas auxiliares** (`auxiliary:`, `compression:`) e entradas de fallback primário (`fallback_providers:` ou `fallback_model:` legado), não à sua configuração principal `model.provider`.

| Provider | Descrição | Requisitos |
|----------|-------------|-------------|
| `"auto"` | Melhor disponível (default). Vision tenta OpenRouter → Nous → Codex. | — |
| `"openrouter"` | Força OpenRouter — roteia para qualquer modelo (Gemini, GPT-4o, Claude, etc.) | `OPENROUTER_API_KEY` |
| `"nous"` | Força Nous Portal | `hermes auth` |
| `"codex"` | Força Codex OAuth (conta ChatGPT). Suporta vision (gpt-5.3-codex). | `hermes model` → Codex |
| `"minimax-oauth"` | Força MiniMax OAuth (login browser, sem API key). Usa MiniMax-M2.7-highspeed para tarefas auxiliares. | `hermes model` → MiniMax (OAuth) |
| `"xai-oauth"` | Força xAI Grok OAuth (login browser para assinantes SuperGrok ou X Premium+, sem API key). Mesmo token OAuth cobre chat, TTS, image, video e transcription. | `hermes model` → xAI Grok OAuth (SuperGrok / Premium+) |
| `"main"` | Usa seu endpoint custom/main ativo. Pode vir de `OPENAI_BASE_URL` + `OPENAI_API_KEY` ou endpoint custom salvo via `hermes model` / `config.yaml`. Funciona com OpenAI, modelos locais ou qualquer API OpenAI-compatible. **Somente tarefas auxiliares — inválido para `model.provider`.** | Credenciais de endpoint custom + base URL |

Providers diretos com API key do catálogo principal também funcionam aqui quando você quer tarefas laterais bypassando seu router default. Por exemplo, `gmi` é válido quando `GMI_API_KEY` está configurada, e `fireworks` quando `FIREWORKS_API_KEY` está configurada:

```yaml
auxiliary:
  compression:
    provider: "gmi"
    model: "anthropic/claude-opus-4.6"
```

Para roteamento auxiliar GMI, use o ID exato de modelo retornado pelo endpoint `/v1/models` da GMI. IDs de modelo Fireworks usam a forma slash nativa do provider, por exemplo `accounts/fireworks/models/glm-5p2`.

### Configurações comuns {#common-setups}

**Usando endpoint customizado direto** (mais claro que `provider: "main"` para APIs locais/self-hosted):
```yaml
auxiliary:
  vision:
    base_url: "http://localhost:1234/v1"
    api_key: "local-key"
    model: "qwen2.5-vl"
```

`base_url` prevalece sobre `provider`, então esta é a forma mais explícita de rotear uma tarefa auxiliar para um endpoint específico. Para overrides de endpoint direto, o Hermes usa `api_key` configurada ou faz fallback para `OPENAI_API_KEY`; não reutiliza `OPENROUTER_API_KEY` para aquele endpoint customizado.

**Usando API key OpenAI para vision:**
```yaml
# Em ~/.hermes/.env:
# OPENAI_BASE_URL=https://api.openai.com/v1
# OPENAI_API_KEY=sk-...

auxiliary:
  vision:
    provider: "main"
    model: "gpt-4o"       # ou "gpt-4o-mini" para mais barato
```

**Usando OpenRouter para vision** (rotear para qualquer modelo):
```yaml
auxiliary:
  vision:
    provider: "openrouter"
    model: "openai/gpt-4o"      # ou "google/gemini-2.5-flash", etc.
```

**Usando Codex OAuth** (conta ChatGPT Pro/Plus — sem API key):
```yaml
auxiliary:
  vision:
    provider: "codex"     # usa seu token OAuth ChatGPT
    # model default gpt-5.3-codex (suporta vision)
```

**Usando MiniMax OAuth** (login browser, sem API key):
```yaml
model:
  default: MiniMax-M2.7
  provider: minimax-oauth
  base_url: https://api.minimax.io/anthropic
```
Execute `hermes model` e selecione **MiniMax (OAuth)** para login e config automática. Para região China, a base URL será `https://api.minimaxi.com/anthropic`. Veja o [guia MiniMax OAuth](../guides/minimax-oauth.md) para walkthrough completo.

**Usando modelo local/self-hosted:**
```yaml
auxiliary:
  vision:
    provider: "main"      # usa seu endpoint custom ativo
    model: "my-local-model"
```

`provider: "main"` usa o provider que o Hermes usa para chat normal — seja provider custom nomeado (ex.: `beans`), provider embutido como `openrouter` ou endpoint legado `OPENAI_BASE_URL`.

:::tip
Se você usa Codex OAuth como provider de modelo principal, vision funciona automaticamente — nenhuma config extra necessária. Codex está incluído na cadeia de auto-detecção para vision.
:::

:::warning
**Vision requer modelo multimodal.** Se definir `provider: "main"`, certifique-se de que seu endpoint suporta multimodal/vision — caso contrário análise de imagem falhará.
:::

### Variáveis de ambiente (legado) {#environment-variables-legacy}

Modelos auxiliares também podem ser configurados via variáveis de ambiente. Porém, `config.yaml` é o método preferido — é mais fácil de gerenciar e suporta todas as opções incluindo `base_url` e `api_key`.

| Configuração | Variável de ambiente |
|---------|---------------------|
| Provider de visão | `AUXILIARY_VISION_PROVIDER` |
| Modelo de visão | `AUXILIARY_VISION_MODEL` |
| Endpoint de visão | `AUXILIARY_VISION_BASE_URL` |
| API key de visão | `AUXILIARY_VISION_API_KEY` |
| Provider de web extract | `AUXILIARY_WEB_EXTRACT_PROVIDER` |
| Modelo de web extract | `AUXILIARY_WEB_EXTRACT_MODEL` |
| Endpoint de web extract | `AUXILIARY_WEB_EXTRACT_BASE_URL` |
| API key de web extract | `AUXILIARY_WEB_EXTRACT_API_KEY` |

Configurações de compression e fallback model são apenas config.yaml.

:::tip
Execute `hermes config` para ver suas configurações atuais de modelos auxiliares. Overrides aparecem apenas quando diferem dos defaults.
:::

## Esforço de raciocínio {#reasoning-effort}

Controla quanto "thinking" o modelo faz antes de responder:

```yaml
agent:
  reasoning_effort: ""   # vazio = medium. Opções: none, minimal, low, medium, high, xhigh, max, ultra
```

Quando indefinido (default), reasoning effort default é "medium" — nível equilibrado que funciona bem na maioria das tarefas. Definir um valor sobrescreve — reasoning effort maior dá melhores resultados em tarefas complexas ao custo de mais tokens e latência.

:::note Modelos adaptive-thinking (Claude 4.6+, classe Fable/Mythos) via OpenRouter
Estes modelos usam thinking *adaptativo* e não aceitam o campo usual `reasoning.effort`
— o OpenRouter o ignora para eles. O Hermes roteia transparentemente seu
`reasoning_effort` para o parâmetro `verbosity` do OpenRouter (que mapeia para
`output_config.effort` da Anthropic), então o mesmo knob de effort continua funcionando com
os níveis suportados pelo modelo selecionado. `none` (ou indefinido) deixa o modelo
no próprio default adaptativo. O
provider Anthropic nativo já controla effort diretamente e não é afetado.
:::

Você também pode alterar reasoning effort em runtime com o comando `/reasoning`:

```
/reasoning                # Mostrar nível de effort atual e state de exibição
/reasoning high           # Definir reasoning effort para high (somente esta sessão)
/reasoning high --global  # Definir effort e persistir em config.yaml
/reasoning none           # Desabilitar reasoning (somente esta sessão)
/reasoning show           # Mostrar thinking do modelo acima de cada resposta
/reasoning hide           # Ocultar thinking do modelo
```

Mudanças de effort são com escopo de sessão por padrão; adicione `--global` para salvar o
novo nível como seu default `agent.reasoning_effort`.

#### Overrides de raciocínio por modelo {#per-model-reasoning-overrides}

Você pode definir níveis diferentes de reasoning effort para modelos diferentes. Útil quando quer reasoning alto para modelos complexos mas medium para os mais rápidos:

```yaml
agent:
  reasoning_effort: "medium"       # default global
  reasoning_overrides:
    "openrouter/anthropic/claude-opus-4.5": "xhigh"
    "openai/gpt-5": "low"
    "claude-sonnet-4.6": "high"    # nome de modelo bare também funciona
```

A correspondência de chaves é **tolerante a grafia** — qualquer grafia razoável corresponde:
- `claude-opus-4.5`, `claude-opus-4-5`, `claude-opus.4.5` (pontos e hífens são intercambiáveis)
- `anthropic/claude-opus-4.5`, `openrouter/anthropic/claude-opus-4.5` (prefixo de provider opcional)
- Correspondências exatas têm precedência sobre variantes

:::note
Não há suporte `hermes config set` para chaves `reasoning_overrides` — edite o arquivo YAML diretamente. Isso porque nomes de modelo frequentemente contêm pontos (ex.: `claude-opus-4.5`), que conflitam com a sintaxe dotted-key da CLI.
:::

**Prioridade de resolução:**

1. Override `/reasoning --session` com escopo de sessão (somente gateway)
2. Override por modelo de `agent.reasoning_overrides` (tolerante a grafia)
3. Global `agent.reasoning_effort`
4. Default do provider

O override aplica-se automaticamente em todo lugar: startup CLI, gateway de messaging, Desktop/TUI, cron jobs, trocas mid-session `/model` e ativação de modelo de fallback.

## Enforcement de uso de ferramentas {#tool-use-enforcement}

Alguns modelos ocasionalmente descrevem ações pretendidas como texto em vez de fazer tool calls ("Eu rodaria os testes..." em vez de chamar o terminal de fato). Tool-use enforcement injeta guidance no system prompt que direciona o modelo de volta a chamar ferramentas de fato.

```yaml
agent:
  tool_use_enforcement: "auto"   # "auto" | true | false | ["model-substring", ...]
```

| Valor | Comportamento |
|-------|----------|
| `"auto"` (default) | Habilitado para modelos que correspondem a: `gpt`, `codex`, `gemini`, `gemma`, `grok`, `glm`, `qwen`, `deepseek`. Desabilitado para todos os outros (ex.: Claude). |
| `true` | Sempre habilitado, independentemente do modelo. Útil se notar seu modelo atual descrevendo ações em vez de executá-las. |
| `false` | Sempre desabilitado, independentemente do modelo. |
| `["gpt", "codex", "qwen", "llama"]` | Habilitado apenas quando o nome do modelo contém um dos substrings listados (case-insensitive). |

### O que injeta {#what-it-injects}

Quando habilitado, três camadas de guidance podem ser adicionadas ao system prompt:

1. **General tool-use enforcement** (todos os modelos correspondentes) — instrui o modelo a fazer tool calls imediatamente em vez de descrever intenções, continuar trabalhando até a tarefa completar e nunca encerrar um turno com promessa de ação futura.

2. **OpenAI execution discipline** (modelos GPT, Codex e Grok) — guidance adicional abordando modos de falha específicos do GPT: abandonar trabalho com resultados parciais, pular lookups de pré-requisitos, alucinar em vez de usar ferramentas e declarar "done" sem verificação.

3. **Google operational guidance** (somente modelos Gemini e Gemma) — concisão, paths absolutos, tool calls paralelas e padrões verify-before-edit.

São transparentes ao usuário e afetam apenas o system prompt. Modelos que já usam ferramentas de forma confiável (como Claude) não precisam desta guidance, por isso `"auto"` os exclui.

### Quando ativar {#when-to-turn-it-on}

Se você usa um modelo fora da lista auto default e nota que frequentemente descreve o que *faria* em vez de fazer, defina `tool_use_enforcement: true` ou adicione o substring do modelo à lista:

```yaml
agent:
  tool_use_enforcement: ["gpt", "codex", "gemini", "grok", "my-custom-model"]
```

## Guardrails de loop de ferramentas {#tool-loop-guardrails}

O Hermes detecta quando o agente está preso em loop improdutivo de tool-calling — a mesma tool call falhando repetidamente, a mesma ferramenta falhando sem parar, ou chamada idempotente retornando o mesmo resultado sem progresso. Por padrão injeta **warning** no resultado da ferramenta para o modelo se autocorrigir; não faz hard-stop, já que alguém observando CLI/TUI pode intervir.

Para deployments gateway/servidor unattended, habilite hard stops para um agente travado ser circuit-broken em vez de queimar o iteration budget:

```yaml
tool_loop_guardrails:
  warnings_enabled: true       # injetar warnings nos resultados (default: true)
  hard_stop_enabled: false     # também BLOQUEAR a chamada após threshold hard-stop (default: false)
  warn_after:
    exact_failure: 2           # chamada falha idêntica repetida N vezes
    same_tool_failure: 3       # mesma ferramenta falhando N vezes (args diferentes)
    idempotent_no_progress: 2  # mesmo resultado, sem progresso, N vezes
  hard_stop_after:
    exact_failure: 5
    same_tool_failure: 8
    idempotent_no_progress: 5
  loop_caps:
    max_web_searches: 50       # máx. web_search por turno (0 = ilimitado)
    max_subagents: 50          # máx. subagentes spawnados por turno (0 = ilimitado)
```

`hard_stop_enabled` default é `false` porque sessões interativas têm humano no loop. Em deployments unattended (gateway, cron, workers kanban) defina `true` para falhas repetidas serem bloqueadas em vez de apenas avisadas. Veja também [Docker / unattended deployments](docker.md).

### Limites de loop descontrolado por turno {#per-turn-runaway-loop-caps}

Separado dos thresholds baseados em falha acima, `loop_caps` define tetos rígidos de quantas chamadas `web_search` e spawns de subagente um único loop de agente (turno) pode fazer. Os contadores resetam no início de cada turno, então sessão multi-turno legítima nunca fica faminta — mas um turno que espirala em busca ou delegação ilimitada é parado. Estão sempre ligados e disparam independentemente de `hard_stop_enabled`. Um turno emitindo dezenas de web searches ou spawnando dezenas de subagentes já é patológico, então os defaults são baixos. Quando um cap é atingido, a tool call ofensora é bloqueada com mensagem explicativa e o turno para limpo em vez de queimar o resto do budget. Defina qualquer valor como `0` para desabilitar aquele cap.

Um batch `delegate_task` conta cada task em `max_subagents` (batch de 3 gasta 3), então o cap rastreia subagentes reais spawnados em vez de invocações `delegate_task`.

Espelha os caps por sessão de WebSearch e subagente do Claude Code (v2.1.212), que também default 200 e resetam em `/clear`.

## Configuração de TTS {#tts-configuration}

```yaml
tts:
  provider: "edge"              # "edge" | "elevenlabs" | "openai" | "minimax" | "mistral" | "gemini" | "xai" | "neutts" | "kittentts" | "piper" | "deepinfra"
  speed: 1.0                    # Multiplicador global de speed (fallback para todos os providers)
  edge:
    voice: "en-US-AriaNeural"   # 322 vozes, 74 idiomas
    speed: 1.0                  # Multiplicador de speed (convertido para percentual de rate, ex.: 1.5 → +50%)
  elevenlabs:
    voice_id: "pNInz6obpgDQGcFmaJgB"
    model_id: "eleven_multilingual_v2"
  openai:
    model: "gpt-4o-mini-tts"
    voice: "alloy"              # alloy, echo, fable, onyx, nova, shimmer
    speed: 1.0                  # Multiplicador de speed (limitado a 0.25–4.0 pela API)
    base_url: "https://api.openai.com/v1"  # Override para endpoints TTS OpenAI-compatible
  minimax:
    speed: 1.0                  # Multiplicador de speed de fala
    # base_url: ""              # Opcional: override para endpoints TTS OpenAI-compatible
  mistral:
    model: "voxtral-mini-tts-2603"
    voice_id: "c69964a6-ab8b-4f8a-9465-ec0925096ec8"  # Paul - Neutral (default)
  gemini:
    model: "gemini-2.5-flash-preview-tts"   # ou gemini-3.1-flash-tts-preview
    voice: "Kore"               # 30 vozes prebuilt: Zephyr, Puck, Kore, Enceladus, etc.
    audio_tags: false           # Inserção oculta de audio-tag TTS Gemini 3.1
    persona_prompt_file: ""      # Arquivo Markdown/texto opcional com direção de voz Gemini
  xai:
    voice_id: "eve"             # Voz TTS xAI
    language: "en"              # ISO 639-1
    sample_rate: 24000
    bit_rate: 128000            # Bitrate MP3
    # base_url: "https://api.x.ai/v1"
  neutts:
    ref_audio: ''
    ref_text: ''
    model: neuphonic/neutts-air-q4-gguf
    device: cpu
```

Isso controla tanto a ferramenta `text_to_speech` quanto respostas faladas no voice mode (`/voice tts` na CLI ou gateway de messaging).

**Hierarquia de fallback de speed:** speed específico do provider (ex.: `tts.edge.speed`) → global `tts.speed` → default `1.0`. Defina `tts.speed` global para speed uniforme em todos providers, ou override por provider para controle fino.

## Configurações de exibição {#display-settings}

```yaml
display:
  tool_progress: all      # off | new | all | verbose
  tool_progress_command: false  # Habilitar slash command /verbose no gateway de messaging
  focus_view: false       # CLI focus view (/focus) — saída reduzida, display-only
  platforms: {}           # Overrides de exibição por plataforma (veja abaixo)
  interim_assistant_messages: true  # Gateway: enviar updates naturais mid-turn do assistant como mensagens separadas
  show_commentary: true   # Modelos Codex: entregar narração de progresso do commentary channel como updates mid-turn visíveis
  skin: default           # Skin CLI built-in ou custom (veja user-guide/features/skins)
  personality: ""         # Campo cosmético legado ainda exibido em alguns resumos
  compact: false          # Modo de saída compacta (menos whitespace)
  resume_display: full    # full (mostrar mensagens anteriores ao retomar) | minimal (apenas one-liner)
  bell_on_complete: false # Tocar bell do terminal quando agente termina (ótimo para tarefas longas)
  show_reasoning: true    # Mostrar raciocínio/pensamento do modelo acima de cada resposta (default: true; alternar com /reasoning show|hide)
  streaming: false        # Fazer stream de tokens para terminal conforme chegam (saída em tempo real)
  show_cost: false        # Mostrar custo $ estimado na status bar da CLI
  timestamps: false       # Quando true, prefixa labels user e assistant com timestamps no transcript CLI / TUI
  timestamp_format: "%H:%M"  # formato strftime para esses timestamps (ex.: "%b-%d %H:%M" para mês-dia)
  tool_preview_length: 0  # Máx. chars para previews de tool call (0 = sem limite, mostrar paths/comandos completos)
  turn_summary: true      # Somente CLI: imprimir rodapé contábil one-line após cada turno interativo
  spinner_token_flow: true # Somente CLI: anexar tokens cumulativos do turno ao timer do spinner
  runtime_footer:         # Gateway: anexar rodapé de runtime-context a respostas finais
    enabled: false
    fields: ["model", "context_pct", "cwd"]
  file_mutation_verifier: true    # Anexar rodapé advisory quando write_file/patch falharam neste turno
  credits_notices: true   # Avisos de créditos Nous na status bar (faixas de uso, grant-spent, depleted). false = silenciar; /usage ainda funciona
  language: en            # UI language for static messages
``` (approval prompts, some gateway replies). en | zh | zh-hant | ja | de | es | fr | tr | uk | af | ko | it | ga | pt | ru | hu
```

### Resumo por turno e fluxo de tokens no spinner {#per-turn-summary-and-spinner-token-flow}

`display.turn_summary` (default `true`) imprime uma linha contábil dim após cada turno **interativo da CLI**, resumindo o que aquele turno realmente fez:

```
⋯ 12.4s · edited 2 files +18 -3 · read 4 files · ran 3 commands
```

A contagem é observada do feed tool-progress que a CLI já recebe, então não custa nada extra. Detalhes:

- Wall time é a duração real do turno (`2m05s` após um minuto).
- Tool calls são agrupadas por verbo (`edited`, `read`, `ran`, `searched`, …) com pluralização correta; ferramentas plugin/MCP sem verbo curado colapsam em `called N tools`.
- Deltas de linha `+X -Y` aparecem apenas quando o resultado da ferramenta já reporta diff (atualmente `patch`). O Hermes nunca executa git para calculá-los, então edição `write_file` é contada sem delta.
- **Tool calls falhas não são contadas** — write negado nunca renderiza como edição bem-sucedida (veja o [file-mutation verifier](#file-mutation-verifier) para o aviso complementar).
- Turnos longos limitam a quatro segmentos de verbo mais cauda `+N more` para a linha nunca quebrar.
- Turno rápido sem tool calls não imprime nada.

`display.spinner_token_flow` (default `true`) anexa tokens de output cumulativos do turno em execução ao timer live do spinner da CLI:

```
  ⚡ Reading cli.py  (  2.3s · ↓ 1.2k tok)
```

A contagem é por turno (totais de sessão são baselined no início do turno) e atualiza conforme cada chamada de API no turno reporta usage. Nada renderiza antes do primeiro usage report chegar, então você nunca vê `↓ 0 tok` enganoso.

Ambas as chaves são display-only e CLI-only: são suprimidas em quiet mode, quando `display.tool_progress` é `off`, em runs batch single-query/`-Q` e em superfícies gateway/messaging (essas usam `display.runtime_footer`). Defina qualquer chave como `false` para desligar.

### Verificador de mutação de arquivos {#file-mutation-verifier}

Quando `display.file_mutation_verifier` é `true` (default), o Hermes anexa um aviso de uma linha à resposta final do assistant sempre que uma chamada `write_file` ou `patch` falhou durante o turno e nunca foi substituída por write bem-sucedido no mesmo path. Isso captura a classe de over-claim "batch de patches paralelos, metade falha silenciosamente, modelo resume sucesso" sem exigir `git status` manual após cada edição.

Exemplo de rodapé:

```
⚠️ File-mutation verifier: 3 file(s) NÃO foram modificados neste turno apesar de qualquer texto acima que possa sugerir o contrário. Execute `git status` ou `read_file` para confirmar.
  • concepts/automatic-organization.md — [patch] Não foi possível encontrar correspondência para old_string
  • concepts/lora.md — [patch] Não foi possível encontrar correspondência para old_string
  • concepts/rag-pipeline.md — [patch] Não foi possível encontrar correspondência para old_string
```

Defina `file_mutation_verifier: false` (ou `HERMES_FILE_MUTATION_VERIFIER=0`) para suprimir o rodapé. O verifier dispara apenas quando falhas reais estão pendentes no fim do turno — um modelo que retenta patch falho e tem sucesso no mesmo turno não o dispara para aquele arquivo.

**Confie no verifier em vez do resumo do modelo.** O rodapé significa que os arquivos listados **não** foram modificados em disco, mesmo se a mensagem final do assistant diz que a tarefa terminou. Causas comuns:

- **Write denied** — path está na denylist de credenciais ou fora de `HERMES_WRITE_SAFE_ROOT` (veja [File write safety](./security.md#file-write-safety))
- **Patch mismatch** — `old_string` não correspondeu ao arquivo em disco
- **Syntax gate** — conteúdo candidato falhou validação JSON/YAML/TOML antes do write

Exemplo de rodapé quando writes são bloqueados:

```
⚠️ File-mutation verifier: 2 file(s) NÃO foram modificados neste turno apesar de qualquer texto acima que possa sugerir o contrário. Execute `git status` ou `read_file` para confirmar.
  • ~/.hermes/cron/jobs.json — [patch] Write negado: '…' está fora de HERMES_WRITE_SAFE_ROOT (/path/to/project)
  • ~/.hermes/scripts/monitor.py — [write_file] Write negado: '…' is outside HERMES_WRITE_SAFE_ROOT (/path/to/project)
```

Se writes para state do Hermes (cron jobs, skills, scripts em `~/.hermes/`) estão falhando, verifique se `HERMES_WRITE_SAFE_ROOT` está definido no seu ambiente. Para mudanças de cron, use a ferramenta `cronjob` ou `hermes cron edit` em vez de fazer patch direto em `jobs.json`.

### Idioma da UI para mensagens estáticas {#ui-language-for-static-messages}

A configuração `display.language` traduz um pequeno conjunto de mensagens estáticas ao usuário — prompt de aprovação da CLI, algumas respostas de slash commands do gateway (ex.: avisos restart-drain, "approval expired", "goal cleared"). **Não** traduz respostas do agente, linhas de log, saída de ferramentas, tracebacks de erro ou descrições de slash commands — esses permanecem em inglês. Se quer que o agente responda em outro idioma, diga no prompt ou system message.

Valores suportados: `en` (default), `zh` (Chinês Simplificado), `zh-hant` (Chinês Tradicional), `ja` (Japonês), `de` (Alemão), `es` (Espanhol), `fr` (Francês), `tr` (Turco), `uk` (Ucraniano), `af` (Afrikaans), `ko` (Coreano), `it` (Italiano), `ga` (Irlandês), `pt` (Português), `ru` (Russo), `hu` (Húngaro). Valores desconhecidos fazem fallback para inglês.

Você também pode definir por sessão com env var `HERMES_LANGUAGE`, que sobrescreve o valor de config.

```yaml
display:
  language: zh   # Prompts de aprovação da CLI aparecem em chinês
```

| Modo | O que você vê |
|------|-------------|
| `off` | Silencioso — apenas a resposta final |
| `new` | Indicador de ferramenta apenas quando a ferramenta muda |
| `all` | Toda tool call com preview curto (default) |
| `verbose` | Args completos, resultados e logs de debug |

Na CLI, alterne entre estes modos com `/verbose`. Para usar `/verbose` em plataformas de messaging (Telegram, Discord, Slack, etc.), defina `tool_progress_command: true` na seção `display` acima. O comando então alterna o modo e salva na config.

Tool progress requer adapter de gateway que possa exibir updates de progresso com segurança. Plataformas sem suporte a edição de mensagem, incluindo Signal, suprimem bubbles de tool-progress mesmo se `/verbose` salvar modo não-`off`.

### Focus view (`/focus`, CLI + TUI) {#focus-view-focus-cli--tui}

`display.focus_view: true` habilita **focus view** — modo de exibição com saída reduzida quando você quer a resposta, não o play-by-play. É camada fina sobre a mesma maquinaria `tool_progress` em vez de segundo caminho de supressão:

- ligar fixa `tool_progress` em `off` e guarda seu modo anterior em `display.focus_saved_tool_progress`;
- `/focus off` restaura aquele modo exatamente, então setup `/verbose verbose` sobrevive ida e volta;
- cada turno completo termina com linha dim de recuperação — `⋯ 7 tool lines hidden · /focus off to show` — contada contra seu modo *pré-focus*, então nunca afirma ter ocultado linhas que você já tinha desligado;
- badge persistente `◉ focus` fica na status bar (CLI prompt_toolkit e TUI Ink) para o modo reduzido nunca ser invisível;
- alternar `/verbose` com focus ligado devolve o modo a `/verbose` e limpa o badge.

Focus view é **display-only**. Nunca edita histórico de conversa, system prompt, schemas de ferramentas ou payload de requisição — detalhe oculto é suprimido na tela, nunca descartado, e prompt caching não é afetado.

### Rodapé de metadados de runtime (somente gateway) {#runtime-metadata-footer-gateway-only}

Quando `display.runtime_footer.enabled: true`, o Hermes anexa rodapé pequeno de runtime-context à mensagem **final** de cada turno gateway. O rodapé atual pode mostrar modelo, percentual da janela de contexto e diretório de trabalho atual. Off por padrão; opt-in por gateway se sua equipe quer toda resposta com esta proveniência.

```yaml
display:
  runtime_footer:
    enabled: true
    fields: ["model", "context_pct", "cwd"]   # ordem exibida; remova qualquer para ocultar
```

Campos suportados:

| Campo | Renderiza | Exemplo |
| --- | --- | --- |
| `model` | ID bare do modelo, prefixo vendor removido | `gpt-5.4` |
| `context_pct` | Ocupação de contexto da última chamada em percentual | `5%` |
| `latency` | Duração wall-clock do turno | `22s`, `1m05s` |
| `cwd` | Diretório de trabalho relativo ao home | `~` |

O conjunto default é `["model", "context_pct", "cwd"]`. `latency` é opt-in — adicione a `fields` para usar. Campos com dados indisponíveis são pulados silenciosamente em vez de slot vazio.

O slash command `/footer` alterna isso em runtime em qualquer sessão.

Exemplo de rodapé anexado a resposta Telegram/Discord/Slack:

```
— claude-opus-4.7 · 12 tool calls · 2m 14s · $0.042
```

Apenas a mensagem **final** de um turno recebe o rodapé; updates interim permanecem limpos.

### Overrides de progresso por plataforma {#per-platform-progress-overrides}

Plataformas diferentes têm necessidades de verbosidade diferentes. Use `display.platforms` para modos por plataforma:

```yaml
display:
  tool_progress: all          # default global
  platforms:
    signal:
      tool_progress: 'off'    # Signal atualmente não exibe bubbles de tool-progress
    telegram:
      tool_progress: verbose  # progresso detalhado no Telegram
    slack:
      tool_progress: 'off'    # silencioso em workspace Slack compartilhado
```

Plataformas sem override fazem fallback ao valor global `tool_progress`. Chaves válidas: `telegram`, `discord`, `slack`, `signal`, `whatsapp`, `matrix`, `mattermost`, `email`, `sms`, `homeassistant`, `dingtalk`, `feishu`, `wecom`, `weixin`, `bluebubbles`, `qqbot`. A chave legada `display.tool_progress_overrides` ainda carrega por compatibilidade mas está depreciada e migrada para `display.platforms` no primeiro load.

Signal está listado como chave válida porque a config pode ser salva por plataforma, mas o adapter Signal atual não edita mensagens enviadas nem renderiza bubbles de tool-progress. Mantenha Signal `tool_progress` em `off`; use CLI ou plataforma de messaging com edição se precisa ver cada tool call ao vivo.

`interim_assistant_messages` é somente gateway. Quando habilitado, o Hermes envia updates mid-turn completos do assistant como mensagens separadas. Independente de `tool_progress` e não requer gateway streaming.

`show_commentary` (default `true`) controla o commentary channel dos modelos Codex Responses — narração polida de progresso que esses modelos produzem junto ao reasoning privado. Quando habilitado, cada mensagem de commentary completa é entregue como update mid-turn visível (no gateway também requer `interim_assistant_messages`). Defina `false` se a narração extra incomoda: commentary faz fallback ao reasoning channel e só aparece com `show_reasoning` habilitado.

## Privacidade {#privacy}

```yaml
privacy:
  redact_pii: false  # Remover PII do contexto LLM (somente gateway)
```

Quando `redact_pii` é `true`, o gateway redige informações pessoalmente identificáveis do system prompt antes de enviá-lo ao LLM em plataformas suportadas:

| Campo | Tratamento |
|-------|-----------|
| Números de telefone (user ID no WhatsApp/Signal) | Hashed para `user_<12-char-sha256>` |
| User IDs | Hashed para `user_<12-char-sha256>` |
| Chat IDs | Porção numérica hashed, prefixo de plataforma preservado (`telegram:<hash>`) |
| Home channel IDs | Porção numérica hashed |
| User names / usernames | **Não afetados** (escolha do usuário, visíveis publicamente) |

**Suporte de plataforma:** Redação aplica-se a WhatsApp, Signal e Telegram. Discord e Slack são excluídos porque sistemas de mention (`<@user_id>`) exigem ID real no contexto LLM.

Hashes são determinísticos — o mesmo usuário sempre mapeia ao mesmo hash, então o modelo ainda distingue usuários em group chats. Roteamento e entrega usam valores originais internamente.

## Speech-to-Text (STT) {#speech-to-text-stt}

```yaml
stt:
  enabled: true                # Auto-transcrever voice messages inbound (default: true)
  echo_transcripts: true       # Postar transcripts brutos de volta no chat como 🎙️ "..." (default: true)
  provider: "local"            # "local" | "groq" | "openai" | "mistral" | "xai" | "elevenlabs" | "deepinfra" | ...
  language: "en"               # Dica GLOBAL de idioma para todo provider (idioma por provider prevalece); defina "" para auto-detect
  local:
    model: "base"              # tiny, base, small, medium, large-v3
    language: ""               # override por provider de stt.language
    initial_prompt: ""         # prompt whisper opcional para enviesar vocabulário/script (ex.: Chinês Simplificado)
    vad: true                  # Filtro Silero VAD (default on) — silêncio nunca chega ao whisper; false = comportamento raw (música/ambiente)
    vad_min_silence_ms: 500    # silêncio mín. (ms) que separa chunks de fala quando vad está on
    no_speech_prob_threshold: 0.6  # descartar segmento só quando no_speech_prob > isto...
    logprob_threshold: -1.0        # ...E avg_logprob < this (ambos devem bater — fala real silenciosa sobrevive)
  groq:
    language: ""               # override por provider de stt.language
  openai:
    model: "whisper-1"         # whisper-1 | gpt-4o-mini-transcribe | gpt-4o-transcribe | gpt-transcribe
    language: ""               # override por provider de stt.language
  # model: "whisper-1"         # Chave fallback legada ainda respeitada
```

Resolução de idioma é a mesma para **todo** provider STT (local, groq, openai, mistral, xai, elevenlabs, deepinfra, command providers e plugins): `stt.<provider>.language` → `stt.language` → env var `HERMES_LOCAL_STT_LANGUAGE` → auto-detect do provider. **O default é `stt.language: "en"`** — auto-detecção Whisper frequentemente identifica mal clips curtos ou com sotaque, aparecendo como voice notes transcritos no idioma errado. Falantes não ingleses devem definir `stt.language` para seu código de idioma uma vez (ex.: `"es"`, `"zh"`, `"uk"`); defina `""` para restaurar auto-detecção em uso multilíngue.

Defina `stt.echo_transcripts: false` quando o gateway deve transcrever voice notes para o agente mas não deve postar o transcript bruto de volta no chat (ex.: bots WhatsApp customer-facing).

Comportamento por provider:

- `local` usa `faster-whisper` na sua máquina. Instale separadamente com `pip install faster-whisper`. Hardening contra alucinação de silêncio ligado por padrão: filtro Silero VAD impede silêncio/ruído de chegar ao Whisper, conditioning cross-window desabilitado, e segmentos que o modelo marca como provavelmente-não-fala *e* baixa confiança são descartados. Defina `stt.local.vad: false` para transcrever áudio não-fala (música, ambiente) com comportamento raw.
- `groq` usa endpoint Whisper-compatible da Groq e lê `GROQ_API_KEY`. Passe `stt.groq.language` (ou env var global `HERMES_LOCAL_STT_LANGUAGE`) para pular auto-detecção e reduzir latência.
- `openai` usa API speech da OpenAI e lê `VOICE_TOOLS_OPENAI_KEY`.

Se o provider solicitado estiver indisponível, o Hermes faz fallback automaticamente nesta ordem: `local` → `groq` → `openai`.

Overrides de modelo Groq e OpenAI são controlados por variáveis de ambiente:

```bash
STT_GROQ_MODEL=whisper-large-v3-turbo
STT_OPENAI_MODEL=whisper-1
GROQ_BASE_URL=https://api.groq.com/openai/v1
STT_OPENAI_BASE_URL=https://api.openai.com/v1
```

## Modo de voz (CLI) {#voice-mode-cli}

```yaml
voice:
  record_key: "ctrl+b"         # Tecla push-to-talk dentro da CLI
  max_recording_seconds: 120    # Parada forçada para gravações longas
  auto_tts: false               # Habilitar respostas faladas automaticamente com /voice on
  beep_enabled: true            # Tocar beeps de início/fim de gravação no voice mode da CLI
  beep_volume: 0.3              # Amplitude do beep (0.0-1.0); aumente em sistemas/fones silenciosos
  silence_threshold: 200        # Threshold RMS para detecção de fala
  silence_duration: 3.0         # Segundos de silêncio antes de auto-stop
```

Use `/voice on` na CLI para habilitar modo microfone, `record_key` para iniciar/parar gravação, e `/voice tts` para alternar respostas faladas. Veja [Voice Mode](/user-guide/features/voice-mode) para setup end-to-end e comportamento por plataforma.

## Streaming {#streaming}

Faz stream de tokens para o terminal ou plataformas de messaging conforme chegam, em vez de esperar a resposta completa.

### Streaming na CLI {#cli-streaming}

```yaml
display:
  streaming: true         # Fazer stream de tokens para terminal em tempo real
  show_reasoning: true    # Também fazer stream de tokens de raciocínio/pensamento (opcional)
```

Quando habilitado, respostas aparecem token a token dentro de uma caixa de streaming. Tool calls ainda são capturadas silenciosamente. Se o provider não suporta streaming, faz fallback automático para exibição normal.

### Streaming no gateway (Telegram, Discord, Slack) {#gateway-streaming-telegram-discord-slack}

```yaml
streaming:
  enabled: true           # Habilitar edição progressiva de mensagem (default: false)
  transport: auto         # "auto" (default) | "edit" (edição progressiva de mensagem) | "off"
  edit_interval: 0.8      # Segundos entre edições de mensagem (default: 0.8)
  buffer_threshold: 24    # Caracteres antes de forçar flush de edição (default: 24)
  cursor: " ▉"            # Cursor exibido durante streaming
  fresh_final_after_seconds: 0    # Opt-in para fresh final (Telegram) quando preview tiver esta idade
```

Quando habilitado, o bot envia mensagem no primeiro token, depois edita progressivamente conforme mais tokens chegam. Plataformas sem suporte a edição de mensagem (Signal, Email, Home Assistant) são auto-detectadas na primeira tentativa — streaming é desabilitado graciosamente para aquela sessão sem flood de mensagens.

Para updates naturais mid-turn do assistant separados sem edição progressiva de tokens, defina `display.interim_assistant_messages: true`.

**Overflow handling:** Se o texto streamed excede o limite de tamanho da plataforma (~4096 chars), a mensagem atual é finalizada e uma nova começa automaticamente.

**Fresh final (Telegram):** `editMessageText` do Telegram preserva o timestamp original da mensagem, então resposta streamed longa manteria timestamp do primeiro token mesmo após conclusão. Defina `fresh_final_after_seconds > 0` para opt-in entregando previews antigos como mensagens finais novas com deleção best-effort do preview. O default é `0`, que sempre finaliza respostas streamed no lugar e evita breve sequência duplicate-message/delete em clientes que mostram ambas operações.

:::note Defaults de streaming por plataforma
O switch mestre `streaming.enabled` é `false` por padrão — nada faz stream até você ligar. Uma vez habilitado, streaming é decidido **por plataforma**: Telegram vem com `display.platforms.telegram.streaming: true` (faz stream) e Discord com `display.platforms.discord.streaming: false` (não faz). Então após habilitar streaming, Telegram faz stream out of the box e Discord permanece em respostas de mensagem inteira até você mudar seu toggle. Você pode ajustar esses switches por plataforma nos toggles **Channels** do dashboard ou diretamente em `~/.hermes/config.yaml`.
:::

## Isolamento de sessão em group chat {#group-chat-session-isolation}

Limite quantas sessões de chat podem estar ativamente abertas na CLI, TUI/dashboard
e gateway de messaging:

```yaml
max_concurrent_sessions: null  # null/0 = ilimitado; inteiro positivo = cap de sessões ativas
```

Um slot é ocupado quando uma sessão executa seu **primeiro turno**, não quando uma janela de chat
é aberta. Abrir, retomar ou reconectar a um chat não custa nada até você
enviar uma mensagem, então abas desktop idle (e resumes em background que um websocket instável
dispara) não podem famintar o gateway de messaging que compartilha este cap.

Quando o cap é atingido, o Hermes retorna mensagem direta de limite nomeando quais
superfícies ocupam os slots. Sessões ativas existentes mantêm comportamento normal.
Execute `hermes status` para ver uso atual de slots e cada holder.

A chave canônica é top-level `max_concurrent_sessions`. O Hermes também aceita
`gateway.max_concurrent_sessions` como fallback, mas a chave top-level prevalece quando
ambas estão definidas.

O cap é aplicado com arquivo de lease runtime local e é best-effort: o Hermes
falha open se o registry não puder ser lido ou locked para usuários não ficarem presos.
Destinado a runtime single host/profile, não `$HERMES_HOME` compartilhado
montado em múltiplas máquinas.

Controle se chats compartilhados mantêm uma conversa por sala ou uma conversa por participante:

```yaml
group_sessions_per_user: true  # true = isolamento por usuário em groups/channels, false = uma sessão compartilhada por chat
```

- `true` é default e recomendado. Em canais Discord, groups Telegram, canais Slack e contextos compartilhados similares, cada remetente recebe sua própria sessão quando a plataforma fornece user ID.
- `false` reverte ao comportamento antigo de sala compartilhada. Pode ser útil se quer explicitamente que o Hermes trate um canal como conversa colaborativa única, mas também significa que usuários compartilham contexto, custos de token e state de interrupt.
- DMs não são afetadas. O Hermes ainda chaveia DMs por chat/DM ID como usual.
- Threads permanecem isoladas do canal pai de qualquer forma; com `true`, cada participante também recebe sessão própria dentro da thread.

Para detalhes de comportamento e exemplos, veja [Sessions](/user-guide/sessions) e o [guia Discord](/user-guide/messaging/discord).

## Comportamento de DM não autorizada {#unauthorized-dm-behavior}

Controle o que o Hermes faz quando usuário desconhecido envia mensagem direta:

```yaml
unauthorized_dm_behavior: pair

whatsapp:
  unauthorized_dm_behavior: ignore
```

- `pair` é default para plataformas DM estilo chat. O Hermes nega acesso, mas responde com código de pairing one-time em DMs.
- `ignore` descarta silenciosamente DMs não autorizadas.
- Email default é `ignore` a menos que `platforms.email.unauthorized_dm_behavior: pair` esteja definido, porque inboxes podem conter mail não relacionado não lido.
- Seções de plataforma sobrescrevem default global, então você pode manter pairing habilitado amplamente enquanto deixa uma plataforma mais silenciosa.

## Quick commands {#quick-commands}

Defina comandos customizados que rodam comandos shell sem invocar o LLM, ou fazem alias de um slash command para outro. Quick commands exec são zero-token e úteis em plataformas de messaging (Telegram, Discord, etc.) para checagens rápidas de servidor ou scripts utilitários.

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

Uso: digite `/status`, `/disk`, `/update`, `/gpu` ou `/restart` na CLI ou qualquer plataforma de messaging. Comandos `exec` rodam localmente no host e retornam saída diretamente — sem chamada LLM, sem tokens consumidos. Comandos `alias` reescrevem para o target slash command configurado.

- **Timeout de 30 segundos** — comandos longos são mortos com mensagem de erro
- **Prioridade** — quick commands são verificados antes de skill commands, então você pode sobrescrever nomes de skills
- **Autocomplete** — quick commands são resolvidos no dispatch e não aparecem nas tabelas de autocomplete de slash commands embutidas
- **Type** — tipos suportados são `exec` e `alias`; outros tipos mostram erro
- **Funciona em todo lugar** — CLI, Telegram, Discord, Slack, WhatsApp, Signal, Email, Home Assistant

Atalhos de prompt somente string não são quick commands válidos. Para workflows de prompt reutilizáveis, crie uma skill ou alias para slash command existente.

## Human delay {#human-delay}

Simule ritmo de resposta humano em plataformas de messaging:

```yaml
human_delay:
  mode: "off"                  # off | natural | custom
  min_ms: 800                  # Delay mínimo (modo custom)
  max_ms: 2500                 # Delay máximo (modo custom)
```

## Execução de código {#code-execution}

Configure a ferramenta `execute_code`:

```yaml
code_execution:
  mode: project                # project (default) | strict
  timeout: 300                 # Tempo máximo de execução em segundos
  max_tool_calls: 50           # Máx. tool calls dentro de code execution
```

**`mode`** controla diretório de trabalho e interpretador Python para scripts:

- **`project`** (default) — scripts rodam no diretório de trabalho da sessão com python do virtualenv/conda env ativo. Deps de projeto (`pandas`, `torch`, pacotes do projeto) e paths relativos (`.env`, `./data.csv`) resolvem naturalmente, igual ao que `terminal()` vê.
- **`strict`** — scripts rodam em diretório staging temp com `sys.executable` (python próprio do Hermes). Máxima reprodutibilidade, mas deps de projeto e paths relativos não resolvem.

Scrubbing de ambiente (remove `*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_CREDENTIAL`, `*_PASSWD`, `*_AUTH`) e whitelist de ferramentas aplicam-se igualmente em ambos modos — trocar modo não muda postura de segurança.

## Backends de web search {#web-search-backends}

As ferramentas `web_search` e `web_extract` suportam cinco providers de backend. Configure o backend em `config.yaml` ou via `hermes tools`:

```yaml
web:
  backend: firecrawl    # firecrawl | searxng | parallel | tavily | exa

  # Ou use chaves por capability para misturar providers (ex.: search grátis + extract pago):
  search_backend: "searxng"
  extract_backend: "firecrawl"
```

| Backend | Env var | Busca | Extract |
|---------|---------|--------|---------|
| **Firecrawl** (default) | `FIRECRAWL_API_KEY` | ✔ | ✔ |
| **SearXNG** | `SEARXNG_URL` | ✔ | — |
| **Parallel** | `PARALLEL_API_KEY` | ✔ | ✔ |
| **Tavily** | `TAVILY_API_KEY` | ✔ | ✔ |
| **Exa** | `EXA_API_KEY` | ✔ | ✔ |

**Seleção de backend:** Se `web.backend` não estiver definido, o backend é auto-detectado das API keys disponíveis. Se só `SEARXNG_URL` estiver definida, SearXNG é usado. Se só `EXA_API_KEY`, Exa. Se só `TAVILY_API_KEY`, Tavily. Se só `PARALLEL_API_KEY`, Parallel. Caso contrário Firecrawl é default.

**SearXNG** é metasearch engine grátis, self-hosted e respeitoso à privacidade que consulta 70+ search engines. Sem API key — apenas defina `SEARXNG_URL` para sua instância (ex.: `http://localhost:8080`). SearXNG é search-only; `web_extract` requer provider de extract separado (defina `web.extract_backend`). Veja o [guia Web Search setup](/user-guide/features/web-search) para instruções Docker.

**Firecrawl self-hosted:** Defina `FIRECRAWL_API_URL` para sua instância. Com URL custom, API key torna-se opcional (defina `USE_DB_AUTHENTICATION=*** no servidor para desabilitar auth).

**Modos Parallel search:** Defina `PARALLEL_SEARCH_MODE` para controlar comportamento — `fast`, `one-shot` ou `agentic` (default: `agentic`).

**Exa:** Defina `EXA_API_KEY` em `~/.hermes/.env`. Suporta filtro `category` (`company`, `research paper`, `news`, `people`, `personal site`, `pdf`) e filtros de domínio/data.

## Browser {#browser}

Configure comportamento de automação do browser:

```yaml
browser:
  inactivity_timeout: 120        # Segundos antes de fechar sessões idle automaticamente
  command_timeout: 30             # Timeout em segundos para comandos browser (screenshot, navigate, etc.)
  record_sessions: false         # Auto-gravar sessões browser como vídeos WebM em ~/.hermes/browser_recordings/
  # Override CDP opcional — quando definido, Hermes anexa diretamente ao seu
  # browser Chromium-family (via /browser connect) em vez de iniciar browser headless.
  cdp_url: ""
  # Dialog supervisor — controla como dialogs JS nativos (alert / confirm / prompt)
  # são tratados quando backend CDP está anexado (Browserbase, browser Chromium-family local
  # via /browser connect). Ignorado em Camofox e modo agent-browser local default.
  dialog_policy: must_respond    # must_respond | auto_dismiss | auto_accept
  dialog_timeout_s: 300          # Auto-dismiss de segurança sob must_respond (segundos)
  camofox:
    managed_persistence: false   # Quando true, sessões Camofox persistem cookies/logins entre restarts
    user_id: ""                  # userId Camofox gerenciado externamente opcional
    session_key: ""              # session key opcional enviada quando Hermes cria tab
    adopt_existing_tab: false    # Reutilizar tab existente para esta identidade antes de criar uma
```

**Políticas de dialog:**

- `must_respond` (default) — captura o dialog, expõe em `browser_snapshot.pending_dialogs` e espera o agente chamar `browser_dialog(action=...)`. Após `dialog_timeout_s` segundos sem resposta, o dialog é auto-dismissed para evitar que thread JS da página trave para sempre.
- `auto_dismiss` — captura, dismiss imediato. O agente ainda vê registro do dialog em `browser_snapshot.recent_dialogs` com `closed_by="auto_policy"` depois.
- `auto_accept` — captura, accept imediato. Útil para páginas com prompts `beforeunload` agressivos.

Veja a [página browser feature](./features/browser.md#browser_dialog) para workflow completo de dialog.

O toolset browser suporta múltiplos providers. Veja a [página Browser feature](/user-guide/features/browser) para detalhes de Browserbase, Browser Use e setup CDP Chromium-family local.

## Fuso horário {#timezone}

Sobrescreva o fuso horário local do servidor com string IANA timezone. Afeta timestamps em logs, agendamento cron e injeção de hora no system prompt.

```yaml
timezone: "America/New_York"   # IANA timezone (default: "" = hora local do servidor)
```

Valores suportados: qualquer identificador IANA timezone (ex.: `America/New_York`, `Europe/London`, `Asia/Kolkata`, `UTC`). Deixe vazio ou omita para hora local do servidor.

## Discord {#discord}

Configure comportamento específico do Discord para o gateway de messaging:

```yaml
discord:
  require_mention: true          # Exigir @mention para responder em canais de servidor
  free_response_channels: ""     # IDs de canal separados por vírgula onde bot responde sem @mention
  auto_thread: true              # Auto-criar threads em @mention em canais
```

- `require_mention` — quando `true` (default), o bot só responde em canais de servidor quando mencionado com `@BotName`. DMs sempre funcionam sem mention.
- `free_response_channels` — lista separada por vírgula de IDs de canal onde o bot responde a toda mensagem sem exigir mention.
- `auto_thread` — quando `true` (default), mentions em canais criam automaticamente thread para a conversa, mantendo canais limpos (similar a threading Slack).

## Segurança {#security}

Scanning de segurança pré-execução e redação de segredos:

```yaml
security:
  redact_secrets: true           # Redigir padrões de API key em saída de ferramentas e logs (ligado por padrão)
  tirith_enabled: true           # Habilitar scanning Tirith para comandos de terminal
  tirith_path: "tirith"          # Caminho para binário tirith (default: "tirith" em $PATH)
  tirith_timeout: 5              # Segundos para esperar scan tirith antes de timeout
  tirith_fail_open: true         # Permitir execução se tirith indisponível
  website_blocklist:             # Veja seção Website Blocklist abaixo
    enabled: false
    domains: []
    shared_files: []
```

- `redact_secrets` — quando `true`, detecta e redige automaticamente padrões que parecem chaves de API, tokens e senhas em saída de ferramentas antes de entrar no contexto da conversa e logs. **Ligado por padrão**. Defina `false` explicitamente apenas quando precisa de strings tipo credencial brutas para debug ou desenvolvimento do redactor.
- `tirith_enabled` — quando `true`, comandos de terminal são escaneados por [Tirith](https://github.com/sheeki03/tirith) antes da execução para detectar operações potencialmente perigosas.
- `tirith_path` — caminho para o binário tirith. Defina se tirith está instalado em local não padrão.
- `tirith_timeout` — máximo de segundos para esperar scan tirith. Comandos prosseguem se o scan der timeout.
- `tirith_fail_open` — quando `true` (default), comandos são permitidos se tirith estiver indisponível ou falhar. Defina `false` para bloquear comandos quando tirith não puder verificá-los.

## Blocklist de sites {#website-blocklist}

Bloqueie domínios específicos de serem acessados pelas ferramentas web e browser do agente:

```yaml
security:
  website_blocklist:
    enabled: false               # Habilitar bloqueio de URL (default: false)
    domains:                     # Lista de padrões de domínio bloqueados
      - "*.internal.company.com"
      - "admin.example.com"
      - "*.local"
    shared_files:                # Carregar regras adicionais de arquivos externos
      - "/etc/hermes/blocked-sites.txt"
```

Quando habilitado, qualquer URL correspondendo a padrão bloqueado é rejeitada antes da ferramenta web ou browser executar. Aplica-se a `web_search`, `web_extract`, `browser_navigate` e qualquer ferramenta que acessa URLs.

Regras de domínio suportam:
- Domínios exatos: `admin.example.com`
- Subdomínios wildcard: `*.internal.company.com` (bloqueia todos subdomínios)
- Wildcards TLD: `*.local`

Arquivos compartilhados contêm uma regra de domínio por linha (linhas em branco e comentários `#` ignorados). Arquivos ausentes ou ilegíveis logam warning mas não desabilitam outras ferramentas web.

A política é cacheada por 30 segundos, então mudanças de config entram em vigor rapidamente sem restart.

## Aprovações inteligentes {#smart-approvals}

Controle como o Hermes lida com comandos potencialmente perigosos:

```yaml
approvals:
  mode: smart   # smart | manual | off
```

| Modo | Comportamento |
|------|----------|
| `smart` (default) | Usa LLM auxiliar para avaliar se comando sinalizado é realmente perigoso. Comandos de baixo risco são auto-aprovados só para aquele comando. Comandos genuinamente arriscados são negados; decisões incertas escalam ao usuário. |
| `manual` | Pergunta ao usuário antes de executar qualquer comando sinalizado. Na CLI, mostra diálogo interativo de aprovação. Em messaging, enfileira pedido de aprovação pendente. |
| `off` | Pula todas verificações de aprovação. Equivalente a `HERMES_YOLO_MODE=true`. **Use com cautela.** |

Modo smart é particularmente útil para reduzir fadiga de aprovação — deixa o agente trabalhar mais autonomamente em operações seguras enquanto ainda pega comandos genuinamente destrutivos.

:::warning
Definir `approvals.mode: off` desabilita todas verificações de segurança para comandos de terminal. Use apenas em ambientes confiáveis e sandboxed.
:::

### Circuit breaker de negação {#denial-circuit-breaker}

`approvals.denial_breaker_threshold` (default `3`) protege contra o agente retentar variações de comando que o revisor smart-approval continua negando — cada retry queima outra chamada LLM guardian. Após tantas negações consecutivas em uma sessão, a mensagem de deny escala para instrução hard-stop dizendo ao agente para parar, reportar a operação bloqueada e pedir que você execute manualmente ou `/approve`. Qualquer aprovação reseta a contagem; defina `0` para desabilitar:

```yaml
approvals:
  denial_breaker_threshold: 3   # 0 desabilita o breaker
```

### Regras de deny {#deny-rules}

`approvals.deny` é lista de padrões glob que bloqueiam comandos de terminal correspondentes incondicionalmente — mesmo sob `--yolo`, `/yolo` ou `mode: off`. É o counterpart editável pelo usuário da blocklist hardline embutida:

```yaml
approvals:
  deny:
    - "git push --force*"
    - "*curl*|*sh*"
```

Padrões são globs fnmatch case-insensitive e devem ser quoted em YAML (`*` bare no início é parse error). Veja [Security — User-Defined Deny Rules](/user-guide/security#user-defined-deny-rules-approvalsdeny) para detalhes.

### Política customizada de smart approval {#custom-smart-approval-policy}

`approvals.smart_policy` permite anexar suas próprias regras às instruções do revisor smart-approval. Quando definido, o texto é adicionado ao system prompt do LLM guardian (canal confiável — nunca junto ao texto não confiável do comando), para apertar ou relaxar julgamento no seu ambiente sem editar código:

```yaml
approvals:
  smart_policy: |
    Sempre ESCALATE comandos que modificam qualquer coisa sob /etc.
    APPROVE docker compose restarts em ~/deploys — são rotina aqui.
```


## Checkpoints {#checkpoints}

Snapshots automáticos de filesystem antes de operações destrutivas de arquivo. Veja [Checkpoints & Rollback](/user-guide/checkpoints-and-rollback) para detalhes.

```yaml
checkpoints:
  enabled: false                 # Habilitar checkpoints automáticos (também: hermes chat --checkpoints). Default: false (opt-in).
  max_snapshots: 20              # Máx. checkpoints a manter por diretório (default: 20)
```


## Delegação {#delegation}

Configure comportamento de subagentes para a ferramenta delegate:

```yaml
delegation:
  # model: "google/gemini-3-flash-preview"  # Override de modelo (vazio = herdar do pai)
  # provider: "openrouter"                  # Override de provider (vazio = herdar do pai)
  # base_url: "http://localhost:1234/v1"    # Endpoint OpenAI-compatible direto (prevalece sobre provider)
  # api_key: "local-key"                    # API key para base_url (fallback OPENAI_API_KEY)
  # api_mode: ""                            # Protocolo wire para base_url: "chat_completions", "codex_responses" ou "anthropic_messages". Vazio = auto-detect da URL (ex.: sufixo /anthropic → anthropic_messages). Defina explicitamente para endpoints não padrão que a heurística não detecta.
  max_concurrent_children: 3                # Filhos paralelos por batch (piso 1, sem teto). Também via env var DELEGATION_MAX_CONCURRENT_CHILDREN.
  max_spawn_depth: 1                        # Cap de profundidade da árvore de delegação (1-3, limitado). 1 = flat (default): pai spawna leaves que não podem delegar. 2 = filhos orchestrator podem spawnar netos leaf. 3 = três níveis.
  orchestrator_enabled: true                # Kill switch global. Quando false, role="orchestrator" é ignorado e todo filho é forçado a leaf independentemente de max_spawn_depth.
```

**Override provider:modelo de subagente:** Por padrão, subagentes herdam provider e modelo do agente pai. Defina `delegation.provider` e `delegation.model` para rotear subagentes para par provider:modelo diferente — ex.: usar modelo barato/rápido para subtarefas estreitas enquanto o agente principal roda modelo de raciocínio caro.

**Override de endpoint direto:** Se quer o caminho óbvio de endpoint customizado, defina `delegation.base_url`, `delegation.api_key` e `delegation.model`. Isso envia subagentes diretamente àquele endpoint OpenAI-compatible e prevalece sobre `delegation.provider`. Se `delegation.api_key` for omitida, o Hermes faz fallback apenas para `OPENAI_API_KEY`.

**Protocolo wire (`api_mode`):** O Hermes auto-detecta o protocolo wire de `delegation.base_url` (ex.: paths terminando em `/anthropic` → `anthropic_messages`; hostnames Codex / Anthropic nativo / Kimi-coding mantêm detecção existente). Para endpoints que a heurística não classifica — por exemplo Azure AI Foundry, MiniMax, Zhipu GLM ou proxies LiteLLM fronteando backend Anthropic-shaped — defina `delegation.api_mode` explicitamente como um de `chat_completions`, `codex_responses` ou `anthropic_messages`. Deixe vazio (default) para manter auto-detecção.

O provider de delegação usa a mesma resolução de credenciais do startup CLI/gateway. Todos os providers configurados são suportados: `openrouter`, `nous`, `copilot`, `zai`, `kimi-coding`, `minimax`, `minimax-cn`. Quando um provider é definido, o sistema resolve automaticamente base URL, API key e API mode corretos — sem wiring manual de credenciais.

**Precedência:** `delegation.base_url` na config → `delegation.provider` na config → provider pai (herdado). `delegation.model` na config → modelo pai (herdado). Definir apenas `model` sem `provider` muda apenas o nome do modelo mantendo credenciais do pai (útil para trocar modelos no mesmo provider como OpenRouter).

**Largura e profundidade:** `max_concurrent_children` limita quantos subagentes rodam em paralelo por batch (default `3`, piso 1, sem teto). Também via env var `DELEGATION_MAX_CONCURRENT_CHILDREN`. Quando o modelo submete array `tasks` maior que o cap, `delegate_task` retorna erro de ferramenta explicando o limite em vez de truncar silenciosamente. `max_spawn_depth` controla profundidade da árvore de delegação (limitado a 1-3). No default `1`, delegação é flat: filhos não podem spawnar netos, e passar `role="orchestrator"` degrada silenciosamente para `leaf`. Aumente para `2` para filhos orchestrator spawnarem netos leaf; `3` para árvores de três níveis. O agente opta por orquestração por chamada via `role="orchestrator"`; `orchestrator_enabled: false` força todo filho de volta a leaf. Custo escala multiplicativamente — em `max_spawn_depth: 3` com `max_concurrent_children: 3`, a árvore pode atingir 3×3×3 = 27 agentes leaf concorrentes. Veja [Subagent Delegation → Depth Limit and Nested Orchestration](features/delegation.md#depth-limit-and-nested-orchestration) para padrões de uso.

## Clarify {#clarify}

Configure quanto tempo o gateway espera resposta a pergunta esclarecedora. A chave canônica é `agent.clarify_timeout` (default `3600` segundos); chave legada top-level `clarify.timeout` ainda é respeitada se definida explicitamente:

```yaml
agent:
  clarify_timeout: 3600        # Segundos para esperar resposta de esclarecimento do usuário (0 ou menos = ilimitado)
```

## Arquivos de contexto (SOUL.md, AGENTS.md) {#context-files-soulmd-agentsmd}

O Hermes usa dois escopos de contexto diferentes:

| File | Purpose | Scope |
|------|---------|-------|
| `SOUL.md` | **Identidade principal do agente** — define quem o agente é (slot #1 no system prompt) | `~/.hermes/SOUL.md` ou `$HERMES_HOME/SOUL.md` |
| `.hermes.md` / `HERMES.md` | Instruções específicas do projeto (maior prioridade) | Sobe até git root |
| `AGENTS.md` | Instruções específicas do projeto, convenções de código | Walk recursivo de diretórios |
| `CLAUDE.md` | Arquivos de contexto Claude Code (também detectados) | Somente diretório de trabalho |
| `.cursorrules` | Regras Cursor IDE (também detectadas) | Somente diretório de trabalho |
| `.cursor/rules/*.mdc` | Arquivos de regra Cursor (também detectados) | Somente diretório de trabalho |

- **SOUL.md** é a identidade principal do agente. Ocupa slot #1 no system prompt, substituindo completamente a identidade default embutida. Edite para customizar totalmente quem o agente é.
- Se SOUL.md estiver ausente, vazio ou não puder ser carregado, o Hermes faz fallback para identidade default embutida.
- **Arquivos de contexto de projeto usam sistema de prioridade** — apenas UM tipo é carregado (first match wins): `.hermes.md` → `AGENTS.md` → `CLAUDE.md` → `.cursorrules`. SOUL.md é sempre carregado independentemente.
- **AGENTS.md** é hierárquico: se subdiretórios também têm AGENTS.md, todos são combinados.
- O Hermes faz seed automático de `SOUL.md` default se ainda não existir.
- Todos os arquivos de contexto carregados são limitados a `context_file_max_chars` caracteres (default 20.000) com truncamento inteligente.

Veja também:
- [Personality & SOUL.md](/user-guide/features/personality)
- [Context Files](/user-guide/features/context-files)

## Diretório de trabalho {#working-directory}

| Contexto | Default |
|---------|---------|
| **CLI (`hermes`)** | Diretório atual onde você executa o comando |
| **Gateway de messaging** | `terminal.cwd` de `~/.hermes/config.yaml`; se indefinido, diretório home `~` |
| **Docker / Singularity / Modal / SSH** | Diretório home do usuário dentro do container ou máquina remota |

Sobrescreva o diretório de trabalho:
```yaml
# Em ~/.hermes/config.yaml:
terminal:
  cwd: /home/myuser/projects
```

`MESSAGING_CWD` e entradas diretas `TERMINAL_CWD` em `~/.hermes/.env` são fallbacks de compatibilidade legados. Novas configurações devem usar `terminal.cwd`.

## Rede {#network}

Workarounds de conectividade para HTTP de saída:

```yaml
network:
  force_ipv4: false   # Forçar IPv4 para conexões de saída (default: false)
```

`force_ipv4` — em servidores com IPv6 quebrado ou inacessível, Python resolve registros AAAA primeiro e pode travar pelo timeout TCP completo antes de fallback para IPv4. Defina `true` para pular IPv6 totalmente e conectar diretamente via IPv4.

## Onboarding {#onboarding}

Dicas de onboarding de first-touch e oferta estruturada de profile-build:

```yaml
onboarding:
  profile_build: "ask"   # "ask" (default) | "off"
  seen: {}               # latch interno — deixe vazio
```

- `profile_build` — controla o caminho de construção de profile oferecido na primeira mensagem do gateway. `"ask"` (default) oferece construir um profile de usuário; a oferta é **opt-in e protegida por consentimento** — o agente pergunta antes de qualquer lookup e nunca lê contas conectadas silenciosamente. `"off"` mostra apenas uma intro simples. A oferta dispara no máximo uma vez.
- `seen` — state interno. O Hermes trava cada dica mostrada aqui para nunca disparar de novo; a oferta de profile-build também é registrada aqui uma vez mostrada. Não edite manualmente — apague toda a seção `onboarding` se quiser rever todas as dicas.

## Dashboard {#dashboard}

Configuração para o [web dashboard](/user-guide/features/web-dashboard) — tema visual, URL pública e providers de autenticação. Os providers de auth (OAuth, senha básica, drain) estão documentados em detalhe na página web-dashboard; esta é a forma em `config.yaml`.

```yaml
dashboard:
  theme: "default"            # "default" | "midnight" | "ember" | "mono" | "cyberpunk" | "rose"
  show_token_analytics: false # Reabilitar superfícies de analytics token/custo (somente estimativa local)
  public_url: ""              # Authority pública completa para redirect_uri OAuth (env: HERMES_DASHBOARD_PUBLIC_URL)
  oauth:                      # Gate OAuth Portal (ativado com --host e sem --insecure)
    client_id: ""             # agent:{instance_id} — Portal provisiona isto
    portal_url: ""            # vazio → default do plugin (Portal produção)
  basic_auth:                 # Gate username/senha self-hosted (plugin dashboard_auth/basic)
    username: ""              # vazio → plugin no-op
    password_hash: ""         # scrypt$... (preferido — sem plaintext at rest)
    password: ""              # fallback plaintext (hashed in-memory no load)
    secret: ""                # chave de assinatura de token; vazio → random por processo
    session_ttl_seconds: 0    # 0 → default do plugin (12h)
  drain_auth:                 # Gate service-credential de drain-control (plugin dashboard_auth/drain)
    scope: "drain"            # label de capability no principal verificado
    min_secret_chars: 43      # barra de entropia (chars url-safe-b64; 43 ≈ 256 bits)
```

- `theme` — tema visual do dashboard.
- `show_token_analytics` — off por padrão. A página Analytics e figuras token/custo são **estimativa local lower-bound** (excluem chamadas auxiliares, retries, fallbacks e cache writes), então podem ficar bem abaixo da fatura do provider. Defina `true` apenas se entender que não são billing.
- `public_url` — quando definida, é a authority completa (scheme + host + prefixo de path opcional) de onde o `redirect_uri` OAuth é construído. Defina para deploys atrás de reverse proxies que não encaminham headers `X-Forwarded-*` de forma confiável. Deixe vazio para usar reconstrução por proxy-header.
- `oauth` / `basic_auth` / `drain_auth` — config de auth provider lida pelos plugins dashboard-auth incluídos. O drain secret em si **não** é definido aqui; é provisionado via env var `HERMES_DASHBOARD_DRAIN_SECRET`. Veja [Web Dashboard](/user-guide/features/web-dashboard) para setup completo de auth.
