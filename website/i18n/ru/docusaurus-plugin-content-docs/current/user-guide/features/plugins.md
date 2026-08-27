---
sidebar_position: 11
sidebar_label: Plugins
title: Плагины
description: Расширьте Hermes с помощью специальных инструментов, крючков и интеграций
  через систему плагинов.
---

# Плагины

У Hermes есть система плагинов для добавления пользовательских инструментов, хуков и интеграций без изменения основного кода.

Если вы хотите создать собственный инструмент для себя, своей команды или одного проекта,
обычно это правильный путь. Руководство для разработчиков
Страница [Добавление инструментов](/developer-guide/adding-tools) предназначена для встроенного Hermes.
основные инструменты, находящиеся в `tools/` и `toolsets.py`.

**→ [Создать плагин Hermes](/developer-guide/plugins)** — пошаговое руководство с полным рабочим примером.

## Краткий обзор

Перетащите каталог в `~/.hermes/plugins/` с помощью `plugin.yaml` и кода Python:

```
~/.hermes/plugins/my-plugin/
├── plugin.yaml      # manifest
├── __init__.py      # register() — wires schemas to handlers
├── schemas.py       # tool schemas (what the LLM sees)
└── tools.py         # tool handlers (what runs when called)
```

Запустите Hermes — ваши инструменты появятся рядом со встроенными. Модель может позвонить им немедленно.

### Минимальный рабочий пример

Вот полный плагин, который добавляет инструмент `hello_world` и регистрирует каждый вызов инструмента через перехватчик.

**`~/.hermes/plugins/hello-world/plugin.yaml`**

```yaml
name: hello-world
version: "1.0"
description: A minimal example plugin
```

**`~/.hermes/plugins/hello-world/__init__.py`**

```python
"""Minimal Hermes plugin — registers a tool and a hook."""

import json


def register(ctx):
    # --- Tool: hello_world ---
    schema = {
        "name": "hello_world",
        "description": "Returns a friendly greeting for the given name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name to greet",
                }
            },
            "required": ["name"],
        },
    }

    def handle_hello(params, **kwargs):
        del kwargs
        name = params.get("name", "World")
        return json.dumps({"success": True, "greeting": f"Hello, {name}!"})

    ctx.register_tool(
        name="hello_world",
        toolset="hello_world",
        schema=schema,
        handler=handle_hello,
    )

    # --- Hook: log every tool call ---
    def on_tool_call(tool_name, params, result):
        print(f"[hello-world] tool called: {tool_name}")

    ctx.register_hook("post_tool_call", on_tool_call)
```

Поместите оба файла в `~/.hermes/plugins/hello-world/`, перезапустите Hermes, и модель сможет немедленно вызвать `hello_world`. Хук печатает строку журнала после каждого вызова инструмента.

Описание инструмента для работы с моделью находится в `schema["description"]`. Необязательное значение `ctx.register_tool(description=...)` представляет собой отдельные метаданные реестра `ToolEntry`: если оно опущено, по умолчанию используется описание схемы, но Hermes не копирует его обратно в схему, в которой отсутствует `description`. Предпочитайте определять текст один раз в схеме. Если вы предоставляете оба значения, синхронизируйте их; модель видит значение схемы.

Локальные плагины проекта под `./.hermes/plugins/` по умолчанию отключены. Включите их только для доверенных репозиториев, установив `HERMES_ENABLE_PROJECT_PLUGINS=true` перед запуском Hermes.

## Что могут плагины

Каждый API `ctx.*`, указанный ниже, доступен внутри функции `register(ctx)` плагина.

| Возможность | Как |
|-----------|-----|
| Добавить инструменты | `ctx.register_tool(name=..., toolset=..., schema=..., handler=...)` |
| Добавить крючки | `ctx.register_hook("post_tool_call", callback)` |
| Добавить команды слэша | `ctx.register_command(name, handler, description)` — добавляет `/name` в сеансы CLI и шлюза |
| Инструменты диспетчеризации из команд | `ctx.dispatch_tool(name, args)` — вызывает зарегистрированный инструмент с автоматическим подключением контекста родительского агента |
| Добавить команды CLI | `ctx.register_cli_command(name, help, setup_fn, handler_fn)` — добавляет `hermes <plugin> <subcommand>` |
| Внедрить сообщения | `ctx.inject_message(content, role="user", session_key=...)` — см. [Внедрение сообщений](#injecting-messages) |
| Файлы данных корабля | `Path(__file__).parent / "data" / "file.yaml"` |
| Пакет навыков | `ctx.register_skill(name, path)` — пространство имен `plugin:skill`, загружено через `skill_view("plugin:skill")` |
| Ворота в переменных окружения | `requires_env: [API_KEY]` в плагине.yaml — запрос во время `hermes plugins install` |
| Распространять через пип | `[project.entry-points."hermes_agent.plugins"]` |
| Зарегистрируйте шлюзовую платформу (Discord, Telegram, IRC,…) | `ctx.register_platform(name, label, adapter_factory, check_fn, ...)` — см. [Добавление адаптеров платформы](/developer-guide/adding-platform-adapters) |
| Зарегистрируйте серверную часть для создания изображений | `ctx.register_image_gen_provider(provider)` — см. [Плагины поставщика создания изображений](/developer-guide/image-gen-provider-plugin) |
| Зарегистрируйте серверную часть для создания видео | `ctx.register_video_gen_provider(provider)` — см. [Плагины поставщика создания видео](/developer-guide/video-gen-provider-plugin) |
| Зарегистрировать механизм сжатия контекста | `ctx.register_context_engine(engine)` — см. [Плагины контекстного движка](/developer-guide/context-engine-plugin) |
| Зарегистрируйте серверную часть выполнения терминала (облачную песочницу) | `ctx.register_terminal_environment_provider(provider)` — см. [Плагины среды терминала](/developer-guide/terminal-environment-plugin) |
| Направление запросов на одобрение человека | `ctx.register_approval_transport(name, present_fn)` — см. [Утверждающие перевозки](#approval-transports) |
| Зарегистрируйте серверную часть памяти | Подкласс `MemoryProvider` в `plugins/memory/<name>/__init__.py` — см. [Плагины поставщика памяти](/developer-guide/memory-provider-plugin) (использует отдельную систему обнаружения) |
| Выполнить вызов LLM, принадлежащий хосту | `ctx.llm.complete(...)` / `ctx.llm.complete_structured(...)` — заимствуйте активную модель пользователя + аутентификацию для однократного завершения с дополнительной проверкой схемы JSON. См. [Доступ к плагину LLM](/developer-guide/plugin-llm-access) |
| Вызов инструмента MCP (с ограничением возможностей) | `ctx.call_mcp(server, tool, arguments, timeout=30)` — см. [Вызов серверов MCP из плагинов](#calling-mcp-servers-from-plugins) |
| Зарегистрируйте серверную часть вывода (поставщик LLM) | `register_provider(ProviderProfile(...))` в `plugins/model-providers/<name>/__init__.py` — см. [Плагины поставщика моделей](/developer-guide/model-provider-plugin) (использует отдельную систему обнаружения) |

## Открытие плагина

| Источник | Путь | Вариант использования |
|--------|------|----------|
| В комплекте | `<repo>/plugins/` | Поставляется с Hermes — см. [Встроенные плагины](/user-guide/features/built-in-plugins) |
| Пользователь | `~/.hermes/plugins/` | Персональные плагины |
| Проект | `.hermes/plugins/` | Плагины для конкретных проектов (требуется `HERMES_ENABLE_PROJECT_PLUGINS=true`) |
| пип | `hermes_agent.plugins` точки входа | Распределенные пакеты |
| Никс | `services.hermes-agent.extraPlugins` / `extraPythonPackages` | Декларативная установка NixOS — см. [Установка Nix](/getting-started/nix-setup#plugins) |

Более поздние источники переопределяют более ранние при столкновении имен, поэтому его заменяет пользовательский плагин с тем же именем, что и у входящего в комплект плагина.

### Подкатегории плагинов

В каждом источнике Hermes также распознает каталоги подкатегорий, которые направляют плагины в специализированные системы обнаружения:

| Подкаталог | Что он содержит | Система обнаружения |
|---|---|---|
| `plugins/` (корневой) | Общие плагины — инструменты, перехватчики, команды слэша, команды CLI, встроенные навыки | `PluginManager` (вид: `standalone` или `backend`) |
| `plugins/platforms/<name>/` | Адаптеры канала шлюза (`ctx.register_platform()`) | `PluginManager` (вид: `platform`, на уровень глубже) |
| `plugins/image_gen/<name>/` | Серверная часть создания изображений (`ctx.register_image_gen_provider()`) | `PluginManager` (вид: `backend`, на уровень глубже) |
| `plugins/memory/<name>/` | Поставщики памяти (подкласс `MemoryProvider`) | **Собственный загрузчик** в `plugins/memory/__init__.py` (вид: `exclusive` — активен по одному) |
| `plugins/context_engine/<name>/` | Механизмы сжатия контекста (`ctx.register_context_engine()`) | **Собственный загрузчик** в `plugins/context_engine/__init__.py` (по одному активен за раз) |
| `plugins/model-providers/<name>/` | Профили поставщиков LLM (`register_provider(ProviderProfile(...))`) | **Собственный загрузчик** в `providers/__init__.py` (ленивое сканирование при первом вызове `get_provider_profile()`) |

Пользовательские плагины в `~/.hermes/plugins/model-providers/<name>/` и `~/.hermes/plugins/memory/<name>/` переопределяют объединенные плагины с тем же именем — побеждает последний автор в `register_provider()` / `register_memory_provider()`. Добавьте каталог, и он заменит встроенный без каких-либо изменений репо.

## Плагины доступны по вашему желанию (за некоторыми исключениями).

**Общие плагины и установленные пользователем серверные части по умолчанию отключены** — обнаружение находит их (поэтому они отображаются в `hermes plugins` и `/plugins`), но ничего с перехватчиками или инструментами не загружается, пока вы не добавите имя плагина в `plugins.enabled` в `~/.hermes/config.yaml`. Это предотвратит запуск стороннего кода без вашего явного согласия.

```yaml
plugins:
  enabled:
    - my-tool-plugin
    - disk-cleanup
  disabled:       # optional deny-list — always wins if a name appears in both
    - noisy-plugin
```

Три способа перевернуть состояние:

```bash
hermes plugins                    # interactive toggle (space to check/uncheck)
hermes plugins enable <name>      # add to allow-list
hermes plugins disable <name>     # remove from allow-list + add to disabled
```

После `hermes plugins install owner/repo` вас спросят `Enable 'name' now? [y/N]` — по умолчанию нет. Пропустите запрос при установке по сценарию с помощью `--enable` или `--no-enable`.

Для воспроизводимой установки закрепите полный неизменяемый коммит (теги, ветки и
сокращенные SHA не принимаются):

```bash
hermes plugins install owner/repo --ref 0123456789abcdef0123456789abcdef01234567
```

Гермес извлекает отсоединенный коммит и проверяет, что `HEAD` точно соответствует
запрашивает SHA и записывает канонический источник, установленную версию и PIN-код.
статус в текущем профиле. `hermes plugins update` отказывается перемещать закрепленный
плагин; выберите новый точный коммит явно с помощью
`hermes plugins install <source> --force --ref <new-commit>`.
Метаданные установки Profile-local не содержат значений конфигурации, значений среды,
секреты или гранты возможностей.

### Что НЕ блокируется в списке разрешенных

Несколько категорий плагинов обходят `plugins.enabled` — они являются частью встроенной поверхности Hermes и нарушат базовую функциональность, если отключены по умолчанию:

| Вид плагина | Как вместо этого активируется |
|---|---|
| **Входящие в комплект плагины платформы** (IRC, Teams и т. д. под `plugins/platforms/`) | Загружается автоматически, поэтому доступен каждый поставленный канал шлюза. Фактический канал включается через `gateway.platforms.<name>.enabled` в `config.yaml`. |
| **Комплексные серверные части** (поставщики изображений под `plugins/image_gen/` и т. д.) | Загружен автоматически, поэтому серверная часть по умолчанию «просто работает». Выбор происходит через `<category>.provider` в `config.yaml` (например, `image_gen.provider: openai`). |
| **Поставщики памяти** (`plugins/memory/`) | Все обнаружено; активен ровно один, выбранный `memory.provider` в `config.yaml`. |
| **Контекстные механизмы** (`plugins/context_engine/`) | Все обнаружено; один активен, выбран `context.engine` в `config.yaml`. |
| **Поставщики моделей** (`plugins/model-providers/`) | Все объединенные поставщики под `plugins/model-providers/` обнаруживают и регистрируются при первом вызове `get_provider_profile()`. Пользователь выбирает по одному через `--provider` или `config.yaml`. |
| **Плагины `backend`, установленные в Pip** | Подтвердите свое согласие через `plugins.enabled` (так же, как и для обычных плагинов). |
| **Платформы, устанавливаемые пользователем** (под `~/.hermes/plugins/platforms/`) | Подтвердите свое согласие через `plugins.enabled` — для сторонних адаптеров шлюзов требуется явное согласие. |

Вкратце: **комплексная «всегда работающая» инфраструктура загружается автоматически; Общие плагины сторонних производителей включены по своему усмотрению.** Список разрешений `plugins.enabled` — это шлюз, предназначенный специально для произвольного кода, который пользователь помещает в `~/.hermes/plugins/`.

### Утверждение транспорта

Транспорт утверждения изменяет **где человек видит и отвечает** на существующий
Запрос на одобрение инструмента Hermes. Он не решает, нужна ли команде
утверждение, и это не API политики авторизации.

```python
def present(request):
    # Deliver request.command and request.description to your UI, wait for
    # its authenticated human response, then return a request-bound decision.
    choice = send_to_my_ui_and_wait(request)  # once/session/always/deny
    return request.respond(choice)


def register(ctx):
    ctx.register_approval_transport("my-ui", present)
```

`present` может быть синхронным или асинхронным. Гермес запускает его на ограниченном работнике и
применяет канонический `approvals.timeout`, даже если плагин этого не делает.
запрос является неизменяемым и содержит отредактированный отображаемый текст, его представление хоста
класс (`cli` или `gateway`), тайм-аут хоста, разрешенные варианты выбора и непрозрачный
идентификатор запроса/дайджест.
Вернуть результат
`request.respond(choice)`; несвязанные словари и устаревший или измененный запрос
Идентификаторы/дайджесты отклоняются. Плагин не может вернуть область, которую не вернул хост.
предложение (например, `always` при однократном запросе).

Сама по себе регистрация ничего не дает. Включение плагина и явный выбор
его транспортировка представляет собой отдельные этапы согласия:

```yaml
plugins:
  enabled: [my-approval-plugin]

security:
  approval:
    transport: my-ui
    transport_fallback: deny     # default
```

Исключения транспорта, тайм-ауты, недоступные регистрации, неверный выбор,
а устаревшие ответы по умолчанию отклоняются. Чтобы намеренно отобразить подсказку на
обычная поверхность CLI/TUI/шлюза/ACP при сбое выбранного транспорта, установите
`transport_fallback: builtin`. Без этого точного согласия Гермес никогда
материализует подсказку на другой поверхности.

Hermes по-прежнему владеет жесткой блокировкой, защитой sudo-stdin, правилами запрета пользователей,
привязка запроса, разрешенные области действия, постоянство, перехватчики и окончательная авторизация.
Жесткие команды блокируются перед любым обратным вызовом транспорта. Есть
намеренно ** политика одобрения плагинов, автоматическое разрешение обратного вызова или требование отсутствуют.
`pre_tool_call` политика** в этом интерфейсе. Будущая возможность политики одобрения
может использовать модель согласия возможностей плагина, но выбор транспорта не
даруй это.

### Миграция для существующих пользователей

При обновлении до версии Hermes, в которой есть дополнительные плагины (схема конфигурации v21+), любые пользовательские плагины, уже установленные в `~/.hermes/plugins/`, которые еще не были в `plugins.disabled`, **автоматически переходят** в `plugins.enabled`. Ваша существующая установка продолжает работать. Автономные плагины в комплекте НЕ являются устаревшими — даже существующие пользователи должны явно дать свое согласие. (Связанные плагины платформы/бэкенда никогда не нуждались в дедушке, потому что они никогда не были закрытыми.)

## Доступные крючки

Плагины могут регистрировать 26 событий жизненного цикла, которые в настоящее время принимаются `hermes_cli.plugins.VALID_HOOKS`. **[Каталог перехватчиков событий](/user-guide/features/hooks#shipped-plugin-hook-catalog)** является каноническим для точного времени, обработки возврата, полей полезной нагрузки и примечаний о конфиденциальности.

| Описательная категория | Поставляются крючки |
|---|---|
| **Директива/контроль** | `pre_tool_call`, `pre_llm_call`, `pre_verify`, `pre_gateway_dispatch` |
| **Трансформировать** | `transform_tool_result`, `transform_terminal_output`, `transform_llm_output`, `pre_transcription` |
| **Наблюдатель** | `post_tool_call`, `post_llm_call`, `pre_api_request`, `post_api_request`, `api_request_error`, `on_stream_start`, `on_stream_delta`, `on_stream_end`, `on_interim_message`, `on_session_start`, `on_session_end`, `on_session_finalize`, `on_session_reset`, `on_skill_lifecycle`, `subagent_start`, `subagent_stop`, `pre_approval_request`, `post_approval_response`, `pre_command`, `kanban_task_claimed`, `kanban_task_completed`, `kanban_task_blocked` |

Эти категории описывают текущее поведение, а не определяют будущие правила именования. Промежуточное программное обеспечение плагина остается отдельным реестром/поверхностью.
## Типы плагинов

У Hermes есть четыре типа плагинов:

| Тип | Что он делает | Выбор | Расположение |
|------|-------------|-----------|----------|
| **Общие плагины** | Добавляйте инструменты, перехватчики, команды с косой чертой, команды CLI | Множественный выбор (включить/выключить) | `~/.hermes/plugins/` |
| **Поставщики памяти** | Заменить или увеличить встроенную память | Одиночный выбор (один активный) | `plugins/memory/` |
| **Контекстные механизмы** | Заменить встроенный компрессор контекста | Одиночный выбор (один активный) | `plugins/context_engine/` |
| **Поставщики моделей** | Объявить серверную часть вывода (OpenRouter, Anthropic, …) | Мультирегистр, выбранный `--provider` / `config.yaml` | `plugins/model-providers/` |

Поставщики памяти и механизмы контекста являются **плагинами провайдеров** — одновременно может быть активен только один из каждого типа. Поставщики моделей также являются плагинами, но многие из них загружаются одновременно; пользователь выбирает по одному через `--provider` или `config.yaml`. Общие плагины можно включать в любой комбинации.

## Подключаемые интерфейсы — куда обращаться за каждым

В таблице выше показаны четыре категории плагинов, но в разделе «Общие плагины» `PluginContext` предоставляет несколько отдельных точек расширения — и Hermes также принимает расширения вне системы плагинов Python (бэкэнды, управляемые конфигурацией, команды, подключенные к оболочке, внешние серверы и т. д.). Используйте эту таблицу, чтобы найти подходящую документацию для того, что вы хотите создать:

| Хочу добавить… | Как | Авторское руководство |
|---|---|---|
| **инструмент**, которым может воспользоваться LLM | Плагин Python — `ctx.register_tool()` | [Создать плагин Hermes](/developer-guide/plugins) · [Добавление инструментов](/developer-guide/adding-tools) |
| **привязка жизненного цикла** (до/после LLM, начало/окончание сеанса, фильтр инструментов) | Плагин Python — `ctx.register_hook()` | [Справочник по крючкам](/user-guide/features/hooks) · [Создание плагина Hermes](/developer-guide/plugins) |
| **команда косой черты** для CLI/шлюза | Плагин Python — `ctx.register_command()` | [Создать плагин Hermes](/developer-guide/plugins) · [Расширение CLI](/developer-guide/extending-the-cli) |
| **подкоманда** для `hermes <thing>` | Плагин Python — `ctx.register_cli_command()` | [Расширение CLI](/developer-guide/extending-the-cli) |
| Включенный **навык**, который поставляется с вашим плагином | Плагин Python — `ctx.register_skill()` | [Создание навыков](/developer-guide/creating-skills) |
| **Бэкэнд вывода** (поставщик LLM: OpenAI-compat, Codex, Anthropic-Messages, Bedrock) | Плагин провайдера — `register_provider(ProviderProfile(...))` в `plugins/model-providers/<name>/` | **[Плагины поставщиков моделей](/developer-guide/model-provider-plugin)** · [Добавление поставщиков](/developer-guide/adding-providers) |
| **канал шлюза** (Discord/Telegram/IRC/Teams/и т. д.) | Плагин платформы — `ctx.register_platform()` в `plugins/platforms/<name>/` | [Добавление адаптеров платформы](/developer-guide/добавление-платформы-адаптеров) |
| **Бэкэнд памяти** (Honcho, Mem0, Supermemory, …) | Плагин памяти — подкласс `MemoryProvider` в `plugins/memory/<name>/` | [Плагины поставщика памяти](/developer-guide/memory-provider-plugin) |
| **Стратегия сжатия контекста** | Плагин контекстного движка — `ctx.register_context_engine()` | [Плагины контекстного движка](/developer-guide/context-engine-plugin) |
| **Сервис создания изображений** (DALL·E, SDXL, …) | Бэкенд-плагин — `ctx.register_image_gen_provider()` | [Плагины поставщика изображений](/developer-guide/image-gen-provider-plugin) |
| **Бэкэнд для создания видео** (Veo, Kling, Pixverse, Grok-Imagine, Runway, …) | Бэкенд-плагин — `ctx.register_video_gen_provider()` | [Плагины поставщиков генерации видео](/developer-guide/video-gen-provider-plugin) |
| **Бэкэнд TTS** (любой интерфейс командной строки — Piper, VoxCPM, Kokoro, xtts, сценарии клонирования голоса и т. д.) | На основе конфигурации (рекомендуется) — объявите в `tts.providers.<name>` с `type: command` в `config.yaml`. ИЛИ Бэкэнд-плагин Python — `ctx.register_tts_provider()` для Python-SDK/движков потоковой передачи, которым требуется нечто большее, чем просто шаблон оболочки. | [Настройка TTS](/user-guide/features/tts#custom-command-providers) · [Руководство по плагинам Python](/user-guide/features/tts#python-plugin-providers) |
| **Бэкэнд STT** (любой интерфейс командной строки – шепот.cpp, собственный двоичный файл шепота, локальный интерфейс командной строки ASR) | На основе конфигурации (рекомендуется) — объявите в `stt.providers.<name>` с `type: command` в `config.yaml` или установите `HERMES_LOCAL_STT_COMMAND` для устаревшего аварийного люка с одной командой. ИЛИ Внутренний плагин Python — `ctx.register_transcription_provider()` для движков Python-SDK (OpenRouter, SenseAudio, Gemini-STT и т. д.). | [Настройка STT](/user-guide/features/tts#stt-custom-command-providers) · [Руководство по плагинам Python](/user-guide/features/tts#python-plugin-providers-stt) |
| **Внешние инструменты через MCP** (файловая система, GitHub, Linear, Notion, любой сервер MCP) | На основе конфигурации — объявите `mcp_servers.<name>` с помощью `command:`/`url:` в `config.yaml`. Hermes автоматически обнаруживает инструменты сервера и регистрирует их вместе со встроенными модулями. | [MCP](/руководство пользователя/функции/MCP) |
| **Дополнительные источники навыков** (пользовательские репозитории GitHub, частные индексы навыков) | CLI — `hermes skills tap add <repo>` | [Skills Hub](/user-guide/features/skills#skills-hub) · [Публикация пользовательского крана](/user-guide/features/skills#publishing-a-custom-skill-tap) |
| **Перехватчики событий шлюза** (активируются `gateway:startup`, `session:start`, `agent:end`, `command:*`) | Перетащите `HOOK.yaml` + `handler.py` в `~/.hermes/hooks/<name>/` | [Перехватчики событий](/user-guide/features/hooks#gateway-event-hooks) |
| **Перехватчики оболочки** (запуск команды оболочки для событий — уведомлений, журналов аудита, оповещений на рабочем столе) | На основе конфигурации — объявите под `hooks:` в `config.yaml` | [Shell Hooks](/user-guide/features/hooks#shell-hooks) |

:::примечание
Не все является плагином Python. Некоторые поверхности расширения намеренно используют **команды оболочки, управляемые конфигурацией** (TTS, STT, перехватчики оболочки), поэтому любой CLI, который у вас уже есть, становится плагином без написания Python. Другие представляют собой **внешние серверы** (MCP), к которым агент подключается и автоматически регистрирует инструменты. Некоторые из них представляют собой **каталоги** (перехватчики шлюза) со своим собственным форматом манифеста. Выберите правильную поверхность для стиля интеграции, который соответствует вашему сценарию использования; Руководства по созданию в таблице выше охватывают заполнители, открытия и примеры.
:::

## Декларативные плагины для NixOS

В NixOS плагины можно устанавливать декларативно через параметры модуля — `hermes plugins install` не требуется. Подробную информацию см. в **[Руководстве по настройке Nix](/getting-started/nix-setup#plugins)**.

```nix
services.hermes-agent = {
  # Directory plugin (source tree with plugin.yaml)
  extraPlugins = [ (pkgs.fetchFromGitHub { ... }) ];
  # Entry-point plugin (pip package)
  extraPythonPackages = [ (pkgs.python312Packages.buildPythonPackage { ... }) ];
  # Enable in config
  settings.plugins.enabled = [ "my-plugin" ];
};
```

Декларативные плагины имеют символическую ссылку с префиксом `nix-managed-` — они сосуществуют с плагинами, установленными вручную, и автоматически очищаются при удалении из конфигурации Nix.

## Управление плагинами

```bash
hermes plugins                               # unified interactive UI
hermes plugins list                          # table: enabled / disabled / not enabled
hermes plugins search <term>                 # search the community plugin index
hermes plugins install <name>                # install by index name (resolved to repo @ pinned ref)
hermes plugins install user/repo             # install from Git, then prompt Enable? [y/N]
hermes plugins install user/repo --enable    # install AND enable (no prompt)
hermes plugins install user/repo --no-enable # install but leave disabled (no prompt)
hermes plugins update my-plugin              # pull latest (local edits are autostashed and re-applied)
hermes plugins remove my-plugin              # uninstall
hermes plugins enable my-plugin              # add to allow-list
hermes plugins disable my-plugin             # remove from allow-list + add to disabled
hermes plugins capabilities [my-plugin]      # declared vs granted capabilities
```

### Ссылки для установки в один клик (рабочий стол)

Hermes Desktop регистрирует схему URL-адресов `hermes://`, поэтому веб-сайт, README или
Сообщение чата может ссылаться прямо на установку плагина:

```
hermes://plugin/install?repo=owner/repo            # main install link
hermes://plugin/install?repo=owner/repo&enable=1   # enable the agent plugin after install
hermes://plugin/install?repo=owner/repo&force=1    # replace an existing install
```

Нажатие на одну из них открывает Hermes и отображает **диалоговое окно подтверждения** — идентификатор репо,
примечание «Перед установкой» и ссылки на GitHub и клонирование — затем
мелко клонирует репозиторий, чтобы определить, что он отправляет (**плагин агента** —
серверная часть Python, **плагин рабочего стола** — пользовательский интерфейс приложения или и то, и другое). Вы выбираете
компоненты с флажками и подтвердите. Пока вы этого не сделаете, ничего не будет установлено;
глубокие ссылки никогда не устанавливаются автоматически, и установка плагина агента происходит одинаково
[сканирование безопасности во время установки](#install-time-security-scanning) как
`hermes plugins install`.

Гибридные репозитории (половинки агента и рабочего стола в одном репозитории) используют одну ссылку и одну
диалог. Тот же модальный модуль доступен без ссылки через **Настройки → Плагины →
Установите из Git**. Наследие `hermes://plugin-agent/…` и
`hermes://plugin-desktop/…` URL-адреса направляются в одно и то же диалоговое окно. В девелоперских сборках
(`npm run dev`) схема `hermes-dev://`.

Веб-сайтам не нужен SDK — работает обычный якорь:

```html
<a href="hermes://plugin/install?repo=owner/repo&enable=1">Install in Hermes</a>
```

Серверы MCP имеют эквивалентную форму ссылки — см.
[Добавить ссылку на Hermes](/reference/mcp-config-reference#add-to-hermes-link).

### Возможности и согласие плагина

Плагины могут объявлять привилегированные поверхности хоста, которые они хотят, в своих
`plugin.yaml`:

```yaml
name: my-plugin
capabilities:
  - tools.override        # replace built-in tools
  - llm.model_override    # pick the model for host-owned LLM calls
```

Когда плагин объявляет возможности, `hermes plugins install` (и
`hermes plugins enable`) показывает список с однострочными описаниями рисков и
спрашивает один раз. Согласие регистрирует грант под
`plugins.entries.<id>.granted_capabilities` вместе с хешем согласия и
временная метка. Отказ оставляет плагин включенным с отключенными этими возможностями —
плагин с хорошим поведением проверяет `ctx.has_capability()` и ухудшает работу
изящно.

**Повторное согласие на обновление:** если в обновлении плагина заявлены возможности, которых у вас нет.
Конечно, `hermes plugins update` показывает дополнения и спрашивает еще раз. Новый
возможности остаются отключенными до тех пор, пока вы не согласитесь — обновление плагина никогда не может быть выполнено автоматически.
расширить свой доступ.

**Неинтерактивные сеансы не закрываются:** установка или обновление без
TTY завершает установку, но заявленные возможности *не* предоставляются. Беги
`hermes plugins enable <id>` в интерактивном режиме, чтобы предоставить их позже.

Осмотрите состояние в любое время:

```bash
hermes plugins capabilities             # all plugins with declared/granted capabilities
hermes plugins capabilities my-plugin   # one plugin, declared vs granted
```

Идентификаторы возможностей сопоставляются 1:1 со старыми шлюзами конфигурации для каждой функции, которые сохраняют
работают, но **устарели** в пользу потока согласия:

| Возможность | Устаревший ключ (`plugins.entries.<id>.…`) |
|---|---|
| `tools.override` | `allow_tool_override` |
| `llm.provider_override` | `llm.allow_provider_override` |
| `llm.model_override` | `llm.allow_model_override` |
| `llm.agent_id_override` | `llm.allow_agent_id_override` |
| `llm.profile_override` | `llm.allow_profile_override` |
| `llm.task_override` | `llm.allow_task_override` |
| `gateway.platform_actions` | `allow_platform_actions` |

Ворота открываются, когда *либо* предоставляется возможность, *или* используется устаревший ключ.
set — существующие конфиги продолжают работать без изменений.

:::предупреждение Не песочница
Возможности — это **уровень согласия и аудита**, а не изоляция. Плагины работают как
обычный внутрипроцессный Python: вредоносный плагин может игнорировать здесь каждый шлюз.
Предоставление возможности является заявлением о доверии автору плагина.
это не аудит кода, и компания Hermes не проверяла код плагина. Устанавливать только
плагины из источников, которым вы доверяете.
:::

### Действия платформы

`ctx.platform_actions` предоставляет плагину минимальный набор команд с ограниченными возможностями для
действие на подключенных платформах чата через реестр адаптера живого шлюза —
разрешенная альтернатива исправлению адаптера. **Это выключено
по умолчанию**: каждый вызов повторно проверяет возможность `gateway.platform_actions`
(устаревший ключ `plugins.entries.<id>.allow_platform_actions`) и непредоставленный
вызов вместо действия возвращает структурированную ошибку.

глаголы v1 (оба `async`, оба возвращают простой текст, и ни один из них никогда не превращается в
отправка крючка):

```python
result = await ctx.platform_actions.add_reaction(
    platform="telegram", chat_id="-100123", message_id="456", emoji="👍",
)
result = await ctx.platform_actions.set_thread_title(
    platform="discord", chat_id="123", thread_id="456", title="New title",
)
if not result["ok"]:
    print(result["error"], result.get("detail"))
```

Успех — `{"ok": True, "action": <verb>}`. Неудачи
`{"ok": False, "error": <code>, "detail": <str>}` со стабильными кодами ошибок:
`capability_not_granted`, `invalid_argument`, `gateway_unavailable`,
`unknown_platform`, `adapter_not_registered`, `adapter_disconnected`,
`unsupported_platform_action`, `action_failed`. Действия подтверждают, что
целевой адаптер существует и подключен перед действием; отключенный или
отсутствие адаптера превращается в структурированную ошибку, а не в исключение.

Платформы, поддерживаемые в версии 1: Telegram и Discord. Телеграмма `add_reaction`
*устанавливает* реакцию бота (API бота скорее заменяет предыдущую реакцию бота).
чем штабелирование). Каждое действие — разрешенное или запрещенное — записывается в журнал с помощью
идентификатор плагина, глагол, платформа и результат.

:::предупреждение Примечание по безопасности
Действия платформы — это **возможность обмена сообщениями как бота**: предоставленный плагин может
реагировать и переименовывать темы в любом чате, к которому может обратиться бот-шлюз, а не только
чат, который вызвал крючок. Предоставьте `gateway.platform_actions` только плагинам
вы доверяете и предпочитаете плагины, которые точно документируют, какие действия они предпринимают.
Доступ к полезной нагрузке и дескрипторам SDK необработанной платформы намеренно **не** является частью этого
поверхность — согласно исправлению конструкции #64176 round-2, требуется своя собственная
возможность (`gateway.raw_events`) с меткой «нет гарантии стабильности» и
отдельный дизайн и не поставляется.
:::

### Обнаружение плагинов сообщества

`hermes plugins search <term>` выполняет поиск в **индексе плагинов сообщества** —
статический машиночитаемый каталог плагинов сообщества в формате JSON. Соответствие нечеткое
по имени, описанию и тегам:

```bash
hermes plugins search telegram               # fuzzy search
hermes plugins search                        # browse the whole index
hermes plugins search --capability platform  # filter by declared capability
hermes plugins search media --json           # machine-readable output
hermes plugins search --refresh              # bypass the 24h local cache
```

Найдя плагин, установите его под открытым именем — имя разрешается.
через индекс к его `owner/repo` плюс закрепленный индексом коммит:

```bash
hermes plugins install hermes-media-studio
```

Если имя соответствует более чем одной записи, кандидаты перечисляются и ничего не
установлен. Явные идентификаторы `owner/repo` или Git-URL никогда не затрагивают
index и продолжайте работать так же, как и раньше. Явный `--ref <sha>` всегда
переопределяет индексный контакт.

**Как извлекается индекс.** Индекс размещается по каноническому URL-адресу.
(`https://raw.githubusercontent.com/NousResearch/hermes-plugin-index/main/index.json`,
можно переопределить через `hermes config set plugins.index_url <url>`). Выборки
кэшируется под `~/.hermes/cache/plugin_index.json` на 24 часа; когда
удаленный доступ недоступен, используется устаревший кеш, а когда кеш отсутствует на
Все исходные копии в комплекте поставляются вместе с Hermes, поэтому поиск работает полностью в автономном режиме.

**Формат индексной записи.** Каждая запись представляет собой объект JSON:

```json
{
  "name": "hermes-media-studio",
  "description": "Generative media workspace plugin.",
  "author": "NousResearch",
  "tags": ["media", "image-gen"],
  "repo": "NousResearch/hermes-media-studio",
  "ref": "<40-char commit SHA>",
  "subdir": null,
  "homepage": "https://github.com/NousResearch/hermes-media-studio",
  "capabilities": ["tools", "dashboard"],
  "api_version": 1,
  "added_at": "2026-08-12"
}
```

`repo` — это идентификатор `owner/name` GitHub, `ref` закрепляет неизменяемый коммит.
SHA и дополнительный `subdir` поддерживают монорепозитории. В комплекте исходный файл
(`hermes_cli/data/plugin_index.json` в репозитории) — это ссылка на формат.

**Отправка плагина.** Индекс сохраняется в виде простого файла JSON —
отправить запрос на вытягивание в
[hermes-plugin-index](https://github.com/NousResearch/hermes-plugin-index)
репозиторий, добавляющий вашу запись (имя, описание, автор, теги, `owner/repo`,
и закрепленный коммит SHA). Проверка охватывает только *метаданные* записи.

:::предупреждение Проиндексировано ≠ проверено
Включение в индекс сообщества означает, что метаданные записи были проверены.
**это не аудит кода**. Установка по-прежнему проходит нормально
поток согласия/проверки (установка плагинов отключена по умолчанию, включение является
явный шаг, а права на переопределение инструментов требуют отдельного предоставления). Обзор
исходный код плагина перед его включением.
:::

### Пакеты плагинов

**Пакет плагинов** – это декларативный общий файл YAML (`hermes-pack.yaml`).
который закрепляет набор плагинов — например, общий доступ к модпаку. Установка пакетных вентиляторов
переход к обычным закрепленным установкам; во время выполнения не существует ничего нового.

```yaml
name: voice-assistant-pack
description: STT + streaming TTS + approval relay
author: hyper
version: 1.0.0
plugins:
  - name: hermes-media-studio            # bare community-index name…
    ref: e8d59971d2b7901405b39dac7b03bdd616272d0d
  - repo: owner/approval-relay           # …or explicit owner/repo (or git URL)
    ref: 8f3c2d1a9b4e5f6071829304a5b6c7d8e9f00112
    subdir: plugins/relay                # optional monorepo path
config:                                  # optional, non-secret seeds only
  hermes-media-studio:
    default_model: flux-3
skills: []                               # declared list only (not auto-installed yet)
```

```bash
hermes plugins pack show ./hermes-pack.yaml     # dry-run review
hermes plugins pack install ./hermes-pack.yaml  # review → confirm → install
hermes plugins pack export > hermes-pack.yaml   # snapshot the current install
hermes plugins pack export --enabled-only       # only plugins.enabled
```

**Состояние цепочки поставок.** `ref` каждой записи должно содержать ровно 40 символов.
commit SHA — теги и имена ветвей отклоняются с ошибкой при именовании
запись, то же правило, что и индекс сообщества. Пакет установки ездит точно
тот же закрепленный путь установки, что и `hermes plugins install --ref <sha>`, и запись
то же происхождение в `plugins/.install-metadata.json`, поэтому две установки
один и тот же пакет разрешается одинаково. Пакеты основаны на
[поля манифеста v2](/developer-guide/plugins) (`manifest_version`,
`api_version`, `requires_plugins`) — собственный манифест каждого плагина по-прежнему
проверяется по обычному пути установки.

**Согласие никогда не предоставляется массово.** `pack install` показывает обязательную проверку.
экран (каждый плагин, источник, закрепленная ссылка и заявленные в нем возможности),
затем запрашивает **одно** подтверждение содержимого упаковки. После этого каждый
заявленные возможности плагина проходят стандартную процедуру для каждого плагина.
Запрос согласия на возможность — идентичен одному `hermes plugins install`.
Нет `--yes`, и неинтерактивные сеансы не могут устанавливать пакеты.

**Секреты никогда не передаются пакетами.** Количество семян `config:` ограничено.
несекретные ключи `plugins.entries.<id>` — имена ключей в форме секрета
(`*token*`, `*key*`, `*password*`, …), предоставление возможностей и устаревшие
`allow_*` шлюзы доверия отклоняются при установке и удаляются при экспорте.
Плагины, которым нужны секреты, объявляют их в своем собственном `requires_env`, который
подсказки во время установки, как обычно. Существующие пользовательские значения в
`plugins.entries.<id>` всегда выигрывает упаковку семян.

**Частичный сбой.** Каждый плагин устанавливается независимо; неудачи
сообщается для каждого плагина, остальные продолжаются, и команда завершает работу с ненулевым значением, если
какой-либо плагин не сработал.

**Предупреждения об экспорте.** `pack export` включает только плагины с известным Git.
происхождение (установлено через `hermes plugins install`). Плагины, предназначенные только для локального использования,
перечислены как предупреждающие комментарии в создаваемом YAML, а не как устанавливаемые записи.

Список `skills:` анализируется и отображается во время установки, но еще не
установлены автоматически — пока установите их вручную (`hermes skills`). Электропроводка
idskill-hub в пакетную установку — это задокументированный последующий этап.

### Сканирование безопасности во время установки

Каждые `hermes plugins install` и `hermes plugins update` запускают статический
сканирование безопасности дерева плагинов перед его активацией (по мотивам
Навыки Клода Коворка и сканирование безопасности плагинов). Сканер повторно использует
тот же механизм шаблонов угроз, что и у [охранника Центра навыков](/user-guide/features/skills)
— эксфильтрация хранилищ учетных данных, обратные оболочки, деструктивные команды,
механизмы сохранения, запутанное выполнение и быстрое внедрение
файлы документации — с исключениями, связанными с плагинами: плагин провайдера
чтение своего **собственного** ключа API из среды (документированный
`requires_env` шаблон) не помечен.

Три вердикта, соответствующие «годен/предупрежден/не годен» Cowork:

| Вердикт | Поведение |
|---|---|
| **безопасно** | Устанавливается нормально, без дополнительного вывода |
| **осторожно** | Результаты показаны; вы подтверждаете `Install anyway? [y/N]` (или передаете `--force`) |
| **опасно** | Заблокировано. `--force` **не** переопределяет |

`hermes plugins update` опасный вердикт об обновленном дереве.
отключает плагин до тех пор, пока вы не проверите результаты и не включите его повторно.

Сканирование включено по умолчанию; отключите его в `config.yaml`:

```yaml
plugins:
  scan_on_install: false
```

### Интерактивный интерфейс

Запуск `hermes plugins` без аргументов открывает составной интерактивный экран:

```
Plugins
  ↑↓ navigate  SPACE toggle  ENTER configure/confirm  ESC done

  General Plugins
 → [✓] my-tool-plugin — Custom search tool
   [ ] webhook-notifier — Event hooks
   [ ] disk-cleanup — Auto-cleanup of ephemeral files [bundled]

  Provider Plugins
     Memory Provider          ▸ honcho
     Context Engine           ▸ compressor
```

- **Раздел «Общие плагины»** — флажки, переключаются ПРОБЕЛОМ. Установлено = в `plugins.enabled`, снято = в `plugins.disabled` (явно отключено).
- **Раздел «Плагины поставщика»** — показывает текущий выбор. Нажмите ВВОД, чтобы перейти к средству выбора радио, в котором вы выбираете одного активного провайдера.
- Плагины в комплекте отображаются в том же списке с тегом `[bundled]`.

Выбор плагина поставщика сохраняется в `config.yaml`:

```yaml
memory:
  provider: "honcho"      # empty string = built-in only

context:
  engine: "compressor"    # default built-in compressor
```

### Включено, отключено или нет

Плагины занимают одно из трёх состояний:

| Государство | Значение | В `plugins.enabled`? | В `plugins.disabled`? |
|---|---|---|---|
| `enabled` | Загружено на следующем сеансе | Да | Нет |
| `disabled` | Явно выключен — не загружается, даже если также в `enabled` | (не имеет значения) | Да |
| `not enabled` | Обнаружен, но никогда не подписывался | Нет | Нет |

По умолчанию для вновь установленного или включенного в комплект плагина — `not enabled`. `hermes plugins list` показывает все три различных состояния, чтобы вы могли отличить то, что было явно отключено, а что просто ожидает включения.

В работающем сеансе `/plugins` показывает, какие плагины загружены в данный момент.

## Внедрение сообщений

Плагины могут вставлять сообщения в разговор CLI или известный сеанс шлюза, используя `ctx.inject_message()`:

```python
# Active CLI conversation
ctx.inject_message("New data arrived from the webhook", role="user")

# Existing gateway conversation
ctx.inject_message(
    "New data arrived from the webhook",
    role="user",
    session_key="agent:main:telegram:dm:123456789",
)
```

**Подпись:** `ctx.inject_message(content: str, role: str = "user", *, session_key: str | None = None) -> bool`

В режиме CLI:

- Если агент **бездействует** (ожидает ввода пользователя), сообщение ставится в очередь в качестве следующего ввода и начинает новый ход.
- Если агент находится **в середине хода** (активно работает), сообщение прерывает текущую операцию — так же, как если бы пользователь вводил новое сообщение и нажимал Enter.
– Для ролей, отличных от `"user"`, контент имеет префикс `[role]` (например, `[system] ...`).
- Возвращает `True`, если сообщение успешно поставлено в очередь.

В режиме шлюза:

- `session_key` является обязательным и должен идентифицировать существующий сеанс шлюза. Это стабильный ключ маршрутизации, а не идентификатор сеанса CLI.
- Hermes повторно использует сохраненную платформу, чат, тред, профиль и историю разговоров этого сеанса. Плагины не могут предоставить новый маршрут чата через этот API.
- Перед отправкой компания Hermes повторно проверяет сохраненный маршрут на соответствие текущим правилам авторизации шлюза.
- Маршруты, которые основывались только на решении об авторизации во время адаптера или восходящем направлении, отклоняются, если Hermes не сможет повторно проверить их на основе текущих основных списков разрешений, сопряжения или явной конфигурации разрешения всех.
- Введенный текст всегда представляет собой диалоговый ввод. Он не может вызывать команды с косой чертой, утверждать инструменты или разрешать ожидающие подтверждения и поясняющие запросы.
- Маршрут и разговор закрепляются, пока ожидается отправка. Hermes отбрасывает запрос, если восстановление темы меняет маршрут или сеанс меняется до начала обработки.
- Запрос поступает по обычному пути сообщения адаптера платформы. Активные сеансы используют существующую очередь занятых сеансов, а не начинают конкурирующий ход.
- Возвращает `True`, когда активный шлюз принимает запрос на асинхронную отправку. Это не подтверждает завершение поворота агента или доставки платформы.
- Возвращает `False`, если `session_key` опущен, разрешение не предоставлено или ни один активный шлюз не может принять запрос. Неизвестные или немаршрутизируемые сеансовые ключи, обнаруженные после асинхронного принятия, записываются в журнал шлюза.

Это позволяет плагинам, таким как средства просмотра удаленного управления, мосты обмена сообщениями или приемники веб-перехватчиков, передавать сообщения в разговор из внешних источников.

Внедрение шлюза может отправить ответ агента на внешнюю платформу обмена сообщениями. По умолчанию он отключен для каждого плагина. Предоставьте это для каждого плагина в `config.yaml`:

```yaml
plugins:
  entries:
    my-plugin:
      allow_gateway_injection: true
```

:::предупреждение
Разрешайте внедрение шлюза только тем плагинам, которым вы доверяете. Hermes проверяет это разрешение API хоста и ограничивает его существующими маршрутами сеанса, но плагины Python выполняются внутри процесса, и этот параметр не является песочницей.
:::

:::примечание
Этот API плагина не предоставляет общедоступную конечную точку HTTP или команду CLI для внешних процессов. Плагин уже должен знать целевой шлюз `session_key`, например, из своей доверенной конфигурации или ранее сохраненного состояния сеанса.
:::

## Вызов MCP-серверов из плагинов

`ctx.call_mcp()` позволяет плагину вызывать инструмент на одном из настроенных пользователем серверов MCP — синхронно, из любого обработчика или обработчика инструмента — маршрутизируя через существующий собственный клиент MCP Hermes (те же соединения, шлюзы уровня доверия, автоматический выключатель и логика повторного подключения, что и у инструментов MCP, вызываемых моделью; никогда не параллельный клиент).

```python
result = ctx.call_mcp(
    "knowledge_rag",            # server name from mcp.servers
    "query_knowledge",          # tool on that server
    {"query": "deploy runbook"},
    timeout=30,                 # seconds; clamped to 1–600
)
if result["ok"]:
    print(result["result"])
else:
    print("MCP error:", result["error"])
```

**Подпись:** `ctx.call_mcp(server: str, tool: str, arguments: dict | None = None, timeout: float = 30) -> dict`

Возвращает стабильный конверт: `{"ok": True, "result": ...}` (плюс `structuredContent`, если его предоставляет сервер) или `{"ok": False, "error": "..."}`. Результаты размером более ~64 КБ обрезаются и помечаются `"truncated": True`.

### Безопасность: отключено по умолчанию, список разрешений для каждого сервера

Плагин не имеет доступа к MCP по умолчанию**. Оператор должен предоставить каждому серверу явное разрешение в `config.yaml`:

```yaml
plugins:
  entries:
    my-plugin:
      mcp_allowlist: ["knowledge_rag", "github"]
```

- При вызове сервера, которого нет в списке, возникает `PermissionError` с указанием точного ключа конфигурации, который нужно установить.
- Предоставление предоставляется для каждого сервера и каждого плагина — внешние полномочия не предоставляются для каждого настроенного сервера, а подстановочные знаки `"*"` не учитываются.
— Каждый вызов имеет принудительное время ожидания (по умолчанию 30 секунд), поэтому зависший сервер MCP не может остановить работу перехватчика или конвейера инструментов, вызвавшего его.
- Серверы MCP возвращают ненадежный контент. Относитесь к `result` как к данным, а не к инструкциям — не используйте их в привилегированных решениях (утверждениях, выполнении команд) без проверки.

:::предупреждение
Предоставление `mcp_allowlist` дает плагину тот же доступ к этому серверу MCP, что и модель, включая любые инструменты с возможностью записи, предоставляемые сервером (с учетом шлюзов уровня `trust` сервера). Предоставляйте только те серверы, которые действительно нужны плагину.
:::

См. **[полное руководство](/developer-guide/plugins)** для получения информации о контрактах обработчиков, формате схемы, поведении перехватчиков, обработке ошибок и распространенных ошибках.