---
sidebar_position: 1
title: Архитектура
description: Внутреннее устройство агента Hermes — основные подсистемы, пути выполнения,
  поток данных и где читать дальше
---

# Архитектура

Эта страница представляет собой карту верхнего уровня внутреннего устройства агента Гермес. Используйте его, чтобы сориентироваться в кодовой базе, а затем погрузитесь в документацию по конкретной подсистеме, чтобы узнать подробности реализации.

## Обзор системы

```text
┌─────────────────────────────────────────────────────────────────────┐
│                        Entry Points                                  │
│                                                                      │
│  CLI (cli.py)    Gateway (gateway/run.py)    ACP (acp_adapter/)     │
│  Batch Runner    API Server                  Python Library          │
└──────────┬──────────────┬───────────────────────┬───────────────────┘
           │              │                       │
           ▼              ▼                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     AIAgent (run_agent.py)                          │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │ Prompt       │  │ Provider     │  │ Tool         │               │
│  │ Builder      │  │ Resolution   │  │ Dispatch     │               │
│  │ (prompt_     │  │ (runtime_    │  │ (model_      │               │
│  │  builder.py) │  │  provider.py)│  │  tools.py)   │               │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘               │
│         │                 │                 │                       │
│  ┌──────┴───────┐  ┌──────┴───────┐  ┌──────┴───────┐               │
│  │ Compression  │  │ 3 API Modes  │  │ Tool Registry│               │
│  │ & Caching    │  │ chat_compl.  │  │ (registry.py)│               │
│  │              │  │ codex_resp.  │  │ 70+ tools    │               │
│  │              │  │ anthropic    │  │ 28 toolsets  │               │
│  └──────────────┘  └──────────────┘  └──────────────┘               │
└─────────┴─────────────────┴─────────────────┴───────────────────────┘
           │                                    │
           ▼                                    ▼
┌───────────────────┐              ┌──────────────────────┐
│ Session Storage   │              │ Tool Backends         │
│ (SQLite + FTS5)   │              │ Terminal (6 backends) │
│ hermes_state.py   │              │ Browser (5 backends)  │
│ gateway/session.py│              │ Web (4 backends)      │
└───────────────────┘              │ MCP (dynamic)         │
                                   │ File, Vision, etc.    │
                                   └──────────────────────┘
```

## Структура каталогов

```text
hermes-agent/
├── run_agent.py              # AIAgent — core conversation loop (large file)
├── cli.py                    # HermesCLI — interactive terminal UI (large file)
├── model_tools.py            # Tool discovery, schema collection, dispatch
├── toolsets.py               # Tool groupings and platform presets
├── hermes_state.py           # SQLite session/state database with FTS5
├── hermes_constants.py       # HERMES_HOME, profile-aware paths
├── batch_runner.py           # Batch trajectory generation
│
├── agent/                    # Agent internals
│   ├── prompt_builder.py     # System prompt assembly
│   ├── context_engine.py     # ContextEngine ABC (pluggable)
│   ├── context_compressor.py # Default engine — lossy summarization
│   ├── prompt_caching.py     # Anthropic prompt caching
│   ├── auxiliary_client.py   # Auxiliary LLM for side tasks (vision, summarization)
│   ├── model_metadata.py     # Model context lengths, token estimation
│   ├── models_dev.py         # models.dev registry integration
│   ├── anthropic_adapter.py  # Anthropic Messages API format conversion
│   ├── display.py            # KawaiiSpinner, tool preview formatting
│   ├── skill_commands.py     # Skill slash commands
│   ├── memory_manager.py    # Memory manager orchestration
│   ├── memory_provider.py   # Memory provider ABC
│   └── trajectory.py         # Trajectory saving helpers
│
├── hermes_cli/               # CLI subcommands and setup
│   ├── main.py               # Entry point — all `hermes` subcommands (large file)
│   ├── config.py             # DEFAULT_CONFIG, OPTIONAL_ENV_VARS, migration
│   ├── commands.py           # COMMAND_REGISTRY — central slash command definitions
│   ├── auth.py               # PROVIDER_REGISTRY, credential resolution
│   ├── runtime_provider.py   # Provider → api_mode + credentials
│   ├── models.py             # Model catalog, provider model lists
│   ├── model_switch.py       # /model command logic (CLI + gateway shared)
│   ├── setup.py              # Interactive setup wizard (large file)
│   ├── skin_engine.py        # CLI theming engine
│   ├── skills_config.py      # hermes skills — enable/disable per platform
│   ├── skills_hub.py         # /skills slash command
│   ├── tools_config.py       # hermes tools — enable/disable per platform
│   ├── plugins.py            # PluginManager — discovery, loading, hooks
│   ├── callbacks.py          # Terminal callbacks (clarify, sudo, approval)
│   └── gateway.py            # hermes gateway start/stop
│
├── tools/                    # Tool implementations (one file per tool)
│   ├── registry.py           # Central tool registry
│   ├── approval.py           # Dangerous command detection
│   ├── terminal_tool.py      # Terminal orchestration
│   ├── process_registry.py   # Background process management
│   ├── file_tools.py         # read_file, write_file, patch, search_files
│   ├── web_tools.py          # web_search, web_extract
│   ├── browser_tool.py       # 10 browser automation tools
│   ├── code_execution_tool.py # execute_code sandbox
│   ├── delegate_tool.py      # Subagent delegation
│   ├── mcp_tool.py           # MCP client (large file)
│   ├── credential_files.py   # File-based credential passthrough
│   ├── env_passthrough.py    # Env var passthrough for sandboxes
│   ├── ansi_strip.py         # ANSI escape stripping
│   └── environments/         # Terminal backends (local, docker, ssh, modal, daytona, singularity)
│
├── gateway/                  # Messaging platform gateway
│   ├── run.py                # GatewayRunner — message dispatch (large file)
│   ├── session.py            # SessionStore — conversation persistence
│   ├── delivery.py           # Outbound message delivery
│   ├── pairing.py            # DM pairing authorization
│   ├── hooks.py              # Hook discovery and lifecycle events
│   ├── mirror.py             # Cross-session message mirroring
│   ├── status.py             # Token locks, profile-scoped process tracking
│   ├── builtin_hooks/        # Extension point for always-registered hooks (none shipped)
│   └── platforms/            # Built-in adapters: signal, weixin, bluebubbles,
│                             #   qqbot, whatsapp_cloud, yuanbao, webhook, api_server
│
├── plugins/platforms/        # Bundled platform plugins: telegram, discord, slack,
│                             #   whatsapp, matrix, mattermost, email, sms, dingtalk,
│                             #   feishu, wecom, homeassistant, irc, line, teams,
│                             #   google_chat, buzz, ntfy, photon, raft, simplex
│
├── acp_adapter/              # ACP server (VS Code / Zed / JetBrains)
├── cron/                     # Scheduler (jobs.py, scheduler.py)
├── plugins/memory/           # Memory provider plugins
├── plugins/context_engine/   # Context engine plugins
├── skills/                   # Bundled skills (always available)
├── optional-skills/          # Official optional skills (install explicitly)
├── website/                  # Docusaurus documentation site
└── tests/                    # Pytest suite (~25,000 tests across ~1,250 files)
```

## Поток данных

### Сеанс CLI

```text
User input → HermesCLI.process_input()
  → AIAgent.run_conversation()
    → prompt_builder.build_system_prompt()
    → runtime_provider.resolve_runtime_provider()
    → API call (chat_completions / codex_responses / anthropic_messages)
    → tool_calls? → model_tools.handle_function_call() → loop
    → final response → display → save to SessionDB
```

### Сообщение шлюза

```text
Platform event → Adapter.on_message() → MessageEvent
  → GatewayRunner._handle_message()
    → authorize user
    → resolve session key
    → create AIAgent with session history
    → AIAgent.run_conversation()
    → deliver response back through adapter
```

### Задание Крон

```text
Scheduler tick → load due jobs from jobs.json
  → create fresh AIAgent (no history)
  → inject attached skills as context
  → run job prompt
  → deliver response to target platform
  → update job state and next_run
```

## Рекомендуемый порядок чтения

Если вы новичок в кодовой базе:

1. **Эта страница** — сориентируйтесь
2. **[Внутреннее устройство цикла агента](./agent-loop.md)** — как работает AIAgent
3. **[Prompt Assembly](./prompt-assembly.md)** — построение системного приглашения
4. **[Разрешение во время выполнения поставщика](./provider-runtime.md)** — как выбираются поставщики
5. **[Добавление провайдеров](./adding-providers.md)** — практическое руководство по добавлению нового провайдера.
6. **[Tools Runtime](./tools-runtime.md)** — реестр инструментов, диспетчеризация, среды
7. **[Хранилище сеансов](./session-storage.md)** — схема SQLite, FTS5, происхождение сеанса.
8. **[Gateway Internals](./gateway-internals.md)** — шлюз платформы обмена сообщениями.
9. **[Сжатие контекста и кэширование подсказок](./context-compression-and-caching.md)** — сжатие и кэширование.
10. **[ACP Internals](./acp-internals.md)** — интеграция с IDE.

## Основные подсистемы

### Цикл агента

Механизм синхронной оркестровки (`AIAgent` в `run_agent.py`). Управляет выбором поставщика, созданием подсказок, выполнением инструмента, повторными попытками, откатом, обратными вызовами, сжатием и сохранением. Поддерживает три режима API для серверов разных поставщиков.

→ [Внутреннее устройство цикла агента](./agent-loop.md)

### Система подсказок

Быстрое создание и обслуживание на протяжении всего жизненного цикла разговора:

- **`system_prompt.py` + `prompt_builder.py`** — собирает упорядоченные уровни системных подсказок (`stable` → `context` → `volatile`): идентификационные данные/инструменты/навыки, файлы контекста, затем блоки памяти/профиля/метки времени.
- **`prompt_caching.py`** — применяет точки останова кэша Anthropic для кэширования префиксов.
- **`context_compressor.py`** — суммирует средние повороты разговора, когда контекст превышает пороговые значения.

→ [Сборка подсказок](./prompt-assembly.md), [Сжатие контекста и кэширование подсказок](./context-compression-and-caching.md)

### Разрешение поставщика

Общий преобразователь времени выполнения, используемый CLI, шлюзом, cron, ACP и вспомогательными вызовами. Сопоставляет кортежи `(provider, model)` с `(api_mode, api_key, base_url)`. Обрабатывает более 18 поставщиков, потоки OAuth, пулы учетных данных и разрешение псевдонимов.

→ [Разрешение времени выполнения поставщика](./provider-runtime.md)

### Система инструментов

Центральный реестр инструментов (`tools/registry.py`), содержащий более 70 зарегистрированных инструментов из примерно 28 наборов инструментов. Каждый файл инструмента самостоятельно регистрируется во время импорта. Реестр управляет сбором, отправкой, проверкой доступности и переносом ошибок схемы. Инструменты терминала поддерживают 7 бэкэндов (локальный, Docker, SSH, Daytona, Modal, Singularity, Vercel Sandbox).

→ [Среда выполнения инструментов](./tools-runtime.md)

### Сохранение сеанса

Хранилище сеансов на базе SQLite с полнотекстовым поиском FTS5. Сеансы имеют отслеживание происхождения (родительский/дочерний элемент при сжатии), изоляцию для каждой платформы и атомарные записи с обработкой конфликтов.

→ [Хранилище сеансов](./session-storage.md)

### Шлюз обмена сообщениями

Длительный процесс с более чем 25 адаптерами платформы (встроенные + встроенные плагины), унифицированной маршрутизацией сеансов, авторизацией пользователей (списки разрешений + соединение DM), отправкой косых команд, системой перехвата, тиканием cron и фоновым обслуживанием.

→ [Внутреннее устройство шлюза](./gateway-internals.md)

### Система плагинов

Три источника обнаружения: `~/.hermes/plugins/` (пользователь), `.hermes/plugins/` (проект) и точки входа в пункты. Плагины регистрируют инструменты, перехватчики и команды CLI через контекстный API. Существует два специализированных типа плагинов: поставщики памяти (`plugins/memory/`) и механизмы контекста (`plugins/context_engine/`). Оба имеют одиночный выбор — одновременно может быть активным только один из них, настроенный через `hermes plugins` или `config.yaml`.

→ [Руководство по плагинам](/developer-guide/plugins), [Плагин Memory Provider](./memory-provider-plugin.md)

### Крон

Первоклассные задачи агента (не задачи оболочки). Вакансии хранятся в формате JSON, поддерживают несколько форматов расписаний, могут прикреплять навыки и сценарии и доставляться на любую платформу.

→ [Внутренние элементы Cron](./cron-internals.md)

### Интеграция ACP

Предоставляет Hermes как собственный агент редактора через stdio/JSON-RPC для VS Code, Zed и JetBrains.

→ [Внутренние элементы ACP](./acp-internals.md)

### Траектории

Генерирует траектории в формате ShareGPT на основе сеансов агента для создания обучающих данных.

→ [Траектории и формат обучения](./trajectory-format.md)

## Принципы проектирования

| Принцип | Что это означает на практике |
|-----------|--------------------------|
| **Быстрая стабильность** | Системное приглашение не меняется во время разговора. Никаких мутаций, нарушающих кэш, за исключением явных действий пользователя (`/model`). |
| **Наблюдаемое исполнение** | Каждый вызов инструмента виден пользователю через обратные вызовы. Обновления хода выполнения в CLI (счётчик) и шлюзе (сообщения чата). |
| **Прерываемый** | Вызовы API и выполнение инструмента могут быть отменены в процессе выполнения с помощью пользовательского ввода или сигналов. |
| **Независимое от платформы ядро** | Один класс AIAgent обслуживает интерфейс командной строки, шлюз, ACP, пакетную обработку и сервер API. Различия платформ заключаются в точке входа, а не в агенте. |
| **Слабая связь** | Дополнительные подсистемы (MCP, плагины, поставщики памяти, среды RL) используют шаблоны реестра и шлюзование check_fn, а не жесткие зависимости. |
| **Изоляция профиля** | Каждый профиль (`hermes -p <name>`) получает свой собственный HERMES_HOME, конфигурацию, память, сеансы и PID шлюза. Несколько профилей работают одновременно. |

## Цепочка зависимостей файлов

```text
tools/registry.py  (no deps — imported by all tool files)
       ↑
tools/*.py  (each calls registry.register() at import time)
       ↑
model_tools.py  (imports tools/registry + triggers tool discovery)
       ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

Эта цепочка означает, что регистрация инструмента происходит во время импорта, до создания экземпляра агента. Любой файл `tools/*.py` с вызовом `registry.register()` верхнего уровня обнаруживается автоматически — список импорта вручную не требуется.