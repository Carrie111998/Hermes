---
sidebar_position: 6
title: Перехватчики событий
description: 'Запускайте собственный код в ключевых точках жизненного цикла: регистрируйте
  активность, отправляйте оповещения, публикуйте сообщения в веб-перехватчиках.'
---

# Перехватчики событий

У Hermes есть четыре системы перехватчиков, которые запускают собственный код в ключевых точках жизненного цикла:

| Система | Зарегистрировано через | Вбегает | Вариант использования |
|--------|---------------|---------|----------|
| **[Перехватчики шлюза](#gateway-event-hooks)** | `HOOK.yaml` + `handler.py` в `~/.hermes/hooks/` | Только шлюз | Ведение журнала, оповещения, веб-перехватчики |
| **[Хуки плагинов](#хуки плагинов)** | `ctx.register_hook()` в [плагине](/user-guide/features/plugins) | CLI + шлюз | Инструмент перехвата, метрики, ограждения |
| **[Крючки-ракушки](#крючки-ракушки)** | Блок `hooks:` в `~/.hermes/config.yaml`, указывающий на сценарии оболочки | CLI + шлюз | Встраиваемые скрипты для блокировки, автоформатирования, внедрения контекста |
| **[Исходящие веб-перехватчики](#outbound-webhooks)** | список `hooks.outbound:` в `~/.hermes/config.yaml` | CLI + шлюз | Отправка подписанных событий жизненного цикла на внешние конечные точки HTTP — CI, информационные панели, другие агенты |

Ошибки обратного вызова перехватчика изолируются и протоколируются, а не приводят к сбою агента. Не все хуки пассивны: хуки директив/управления могут изменять поток, преобразования могут заменять содержимое, а крючок оболочки `pre_tool_call` может блокировать или не закрываться.

## Перехватчики событий шлюза

Перехватчики шлюза срабатывают автоматически во время работы шлюза (Telegram, Discord, Slack, WhatsApp, Teams), не блокируя основной конвейер агента.

### Создание хука

Каждый хук представляет собой каталог под `~/.hermes/hooks/`, содержащий два файла:

```text
~/.hermes/hooks/
└── my-hook/
    ├── HOOK.yaml      # Declares which events to listen for
    └── handler.py     # Python handler function
```

#### КРЮК.yaml

```yaml
name: my-hook
description: Log all agent activity to a file
events:
  - agent:start
  - agent:end
  - agent:step
```

Список `events` определяет, какие события запускают ваш обработчик. Вы можете подписаться на любую комбинацию событий, включая подстановочные знаки, такие как `command:*`.

#### обработчик.py

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

**Правила обработчика:**
- Должно быть имя `handle`
- Получает `event_type` (строку) и `context` (дикт).
- Может быть `async def` или обычный `def` — оба работают
- Ошибки фиксируются и протоколируются, что не приводит к сбою агента.

### Доступные события

| Событие | Когда он срабатывает | Контекстные клавиши |
|-------|---------------|--------------|
| `gateway:startup` | Запускается процесс шлюза | `platforms` (список названий активных платформ) |
| `session:start` | Создан новый сеанс обмена сообщениями | `platform`, `user_id`, `session_id`, `session_key` |
| `session:end` | Сеанс завершен (до сброса) | `platform`, `user_id`, `session_key` |
| `session:reset` | Пользователь запустил `/new` или `/reset` | `platform`, `user_id`, `session_key` |
| `session:compress` | Сжатие контекста для сеанса завершено | `platform`, `session_id`, `old_session_id` (пусто при сжатии на месте), `in_place` (bool — `true` = транскрипт, сжатый с тем же идентификатором, `false` = повернутый из `old_session_id`), `compression_count` |
| `agent:start` | Агент начинает обработку сообщения | `platform`, `user_id`, `chat_id`, `thread_id` (идентификатор темы форума/корня темы; пусто, если не в теме), `chat_type` (`"dm"` \| `"group"` \| `"forum"`; пусто, если неизвестно), `session_id`, `message` (усечено) до 500 символов) |
| `agent:step` | Каждая итерация цикла вызова инструмента | `platform`, `user_id`, `session_id`, `iteration`, `tool_names` |
| `agent:end` | Агент завершает обработку | те же ключи, что и `agent:start`, плюс `response` (обрезано до 500 символов) |
| `reaction:added` | К сообщению, которое видит бот, была добавлена ​​реакция в виде смайлика (в настоящее время адаптер Slack). Требуется область `reactions:read` + подписка на события бота `reaction_added`; бот должен быть участником канала. | `platform`, `reaction`, `user_id`, `item_user_id`, `item_type`, `channel_id`, `message_ts`, `team_id`, `event_ts`, `raw_event` |
| `reaction:removed` | Реакция смайлика была удалена из сообщения, которое видит бот. Требуется подписка на события бота `reaction_removed`. | той же формы, что и `reaction:added` |
| `command:*` | Любая выполненная косая черта | `platform`, `user_id`, `command`, `args` |

#### Соответствие подстановочным знакам

Обработчики, зарегистрированные для `command:*`, активируют любое событие `command:` (`command:model`, `command:reset` и т. д.). Отслеживайте все слэш-команды с помощью одной подписки.

:::tip Вложенные ответы
Обработчик, публикующий последующее сообщение в той же теме форума Telegram, должен включать `message_thread_id=int(thread_id)`, если `chat_type == "forum"` и `thread_id` не пусты.
:::

### Примеры

#### Оповещение Telegram о длинных задачах

Отправьте себе сообщение, когда агент сделает более 10 шагов:

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

#### Регистратор использования команд

Отслеживайте, какие команды слэша используются:

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

#### Вебхук запуска сеанса

POST во внешнюю службу в новых сеансах:

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

### Учебное пособие: BOOT.md — запуск контрольного списка при каждой загрузке шлюза

Популярный шаблон сообщества: разместите контрольный список Markdown по адресу `~/.hermes/BOOT.md` и попросите агента запускать его один раз при каждом запуске шлюза. Полезно для «при каждой загрузке проверять сбои cron в ночное время и пинговать меня в Discord, если что-то не удалось» или «подводить итоги развертывания.log за последние 24 часа и публиковать его в Slack #ops».

В этом туториале показано, как создать его самостоятельно в виде определяемого пользователем хука. Hermes не поставляет встроенный крючок BOOT.md — вы подключаете именно то поведение, которое хотите.

#### Что мы строим

1. Файл по адресу `~/.hermes/BOOT.md` с инструкциями по запуску на естественном языке.
2. Перехватчик шлюза, который срабатывает на `gateway:startup`, порождает одноразовый агент с разрешенной моделью/учетными данными вашего шлюза и запускает инструкции BOOT.md.
3. Соглашение `[SILENT]`, позволяющее агенту отказаться от отправки сообщения, когда не о чем сообщить.

#### Шаг 1. Напишите свой контрольный список

Создайте `~/.hermes/BOOT.md`. Напишите это так, как если бы вы давали инструкции помощнику-человеку:

```markdown
# Startup Checklist

1. Run `hermes cron list` and check if any scheduled jobs failed overnight.
2. If any failed, summarize them for Discord #ops (the hook delivers your final response to its configured target).
3. Check if `/opt/app/deploy.log` has any ERROR lines from the last 24 hours. If yes, summarize them and include in the same report.
4. If nothing went wrong, reply with only `[SILENT]` so no message is sent.
```

Агент видит это как часть своего приглашения, поэтому все, что вы можете описать простым языком, работает — вызовы инструментов, команды оболочки, отправка сообщений, суммирование файлов.

#### Шаг 2: Создайте крючок

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

Две ключевые линии:

- `_resolve_gateway_model()` считывает текущую настроенную модель шлюза.
- `_resolve_runtime_agent_kwargs()` разрешает учетные данные поставщика так же, как это делает обычный шлюз, включая ключи API, базовые URL-адреса, токены OAuth и пулы учетных данных.

Без них голый `AIAgent()` возвращается к встроенным настройкам по умолчанию и выдает ошибку 401 для любой конечной точки, отличной от умолчанию.

#### Шаг 3: Проверьте это

Перезапустите шлюз:

```bash
hermes gateway restart
```

Смотрите логи:

```bash
hermes logs --follow --level INFO | grep boot-md
```

Вы должны увидеть `Running BOOT.md (N chars)`, за которым следует либо `boot-md completed: ...` (сводка действий агента), либо `boot-md completed (nothing to report)`, когда агент отвечает точным токеном молчания, например `[SILENT]`.

Удалите `~/.hermes/BOOT.md`, чтобы отключить контрольный список — хук остается загруженным, но автоматически пропускает, когда файла нет.

#### Расширение шаблона

- **Контрольные списки с учетом расписания:** выключите `datetime.now().weekday()` в инструкциях BOOT.md («если сегодня понедельник, проверьте также еженедельный журнал развертывания»). Инструкции представляют собой текст в свободной форме, поэтому все, о чем агент может рассуждать, является честной игрой.
– **Несколько контрольных списков**: укажите перехватчик на разные файлы (`STARTUP.md`, `MORNING.md` и т. д.) и зарегистрируйте для каждого из них отдельные каталоги перехватчиков.
- **Вариант без агента:** если вам не нужен полный цикл агента, полностью пропустите `AIAgent` и попросите обработчик отправить фиксированное уведомление непосредственно через `httpx`. Дешевле, быстрее и не зависит от поставщика.

#### Почему это не встроенный

Более ранняя версия Hermes поставляла это как встроенный перехватчик и автоматически создавала агент с пустыми настройками по умолчанию при каждой загрузке шлюза. Это удивило пользователей с настраиваемыми конечными точками и сделало эту функцию невидимой для пользователей, которые не знали, что она работает. Сохранение его в виде документированного шаблона, созданного вами в каталоге хуков, означает, что вы точно видите, что он делает, и соглашаетесь на него, записывая файлы.

### Как это работает

1. При запуске шлюза `HookRegistry.discover_and_load()` сканирует `~/.hermes/hooks/`.
2. Каждый подкаталог с `HOOK.yaml` + `handler.py` загружается динамически.
3. Обработчики регистрируются на заявленные ими события.
4. В каждой точке жизненного цикла `hooks.emit()` запускает все соответствующие обработчики.
5. Ошибки в любом обработчике фиксируются и протоколируются — сломанный хук никогда не приводит к сбою агента.

:::информация
Перехватчики шлюза срабатывают только на **шлюзе** (Telegram, Discord, Slack, WhatsApp, Teams). CLI не загружает перехватчики шлюза. Для хуков, которые работают везде, используйте [хуки плагинов](#plugin-hooks).
:::

## Хуки плагинов

[Плагины](/user-guide/features/plugins) могут регистрировать перехватчики, которые срабатывают в сеансах **как CLI, так и шлюза**. Они регистрируются программно через `ctx.register_hook()` в функции `register()` вашего плагина.

Подробности об упаковке и регистрации плагина см.
[Руководство по плагинам](/docs/user-guide/features/plugins).

```python
def register(ctx):
    ctx.register_hook("pre_tool_call", my_tool_observer)
    ctx.register_hook("post_tool_call", my_tool_logger)
    ctx.register_hook("pre_llm_call", my_memory_callback)
    ctx.register_hook("post_llm_call", my_sync_callback)
    ctx.register_hook("on_session_start", my_init_callback)
    ctx.register_hook("on_session_end", my_cleanup_callback)
    # Kanban board lifecycle (dependency-wait blocking may fire inside its transaction):
    ctx.register_hook("kanban_task_claimed", my_claim_callback)     # dispatcher process
    ctx.register_hook("kanban_task_completed", my_done_callback)    # worker process
    ctx.register_hook("kanban_task_blocked", my_blocked_callback)   # worker process
```

**Общие правила для всех крючков:**

– Обратные вызовы получают **аргументы ключевых слов**. Всегда принимайте `**kwargs` для обеспечения совместимости.
- Исключения обратного вызова протоколируются и пропускаются; более поздние обратные вызовы продолжаются.
- Приведенный ниже каталог носит описательный характер: **наблюдатели** игнорируют возвраты, **преобразования** принимают первую допустимую замену строки, а **хуки директив/управления** используют документированные формы возврата. Промежуточное программное обеспечение плагина — это отдельный реестр и поверхность, а не еще одна категория перехватчиков.
- Поля корреляции, такие как `turn_id`, `api_request_id`, `task_id`, `session_id` и `api_call_count`, зависят от крюка и могут отсутствовать. Считайте идентификаторы непрозрачными.
- Допустимость имени события во время выполнения происходит от `hermes_cli.plugins.VALID_HOOKS`. `hermes hooks list` перечисляет настроенные перехватчики оболочки/исходящие события, а не все доступные события; `hermes hooks test <event>` сообщает о допустимом наборе только в том случае, если указано недопустимое событие.

### Разделы системных подсказок, безопасные для кэша

Плагины, которым требуется надежное и постоянное руководство, могут зарегистрировать ограниченную систему.
раздел подсказки вместо того, чтобы вводить тот же текст через `pre_llm_call` в
каждый поворот:

```python
def board_rules(session_info):
    return f"Apply the worker rules for profile {session_info['profile_name']}."

def register(ctx):
    ctx.register_system_prompt_section(
        "kanban-advanced.worker-rules",
        board_rules,                       # a string is also accepted
        position="after_memory",
        max_chars=4000,
    )
```

Контракт намеренно узок:

- Идентификаторы – это глобальные, стабильные идентификаторы длиной от 1 до 128 символов в нижнем регистре, в которых используются только
  буквы, цифры, `.`, `_` и `-`. Дубликаты идентификаторов отклоняются.
– `after_memory` — единственная привязка места размещения. Разделы сортируются по идентификатору,
  визуализируется после контекста памяти/профиля и перед метаданными сеанса; плагины
  не может изменить порядок или заменить основное содержимое подсказки.
- Вызываемый объект получает сопоставление только для чтения с `session_id`, `model`,
  `provider`, `platform`, `profile_name` и `cwd`. Он запускается ** один раз для нового
  сеанс**. Его визуализированные байты замораживаются при сжатии и восстанавливаются из
  уже сохраненное полное системное приглашение после перезапуска/возобновления процесса;
  состояние плагина не перечитывается для существующего сеанса.
- `max_chars` ограничен 4000 символами. Все разделы плагина вместе,
  включая заголовки аудита, ограничены 8000 символами и 32
  разделы. Пустое, нестроковое, слишком большое, совокупное превышение бюджета или повышение
  разделы пропускаются с предупреждением; Стремительное строительство продолжается.
- Каждый принятый раздел имеет имя в командной строке и регистрируется в начале сеанса.
  с его плагином, позицией и количеством символов.

Используйте `pre_llm_call` для действительно динамического контекста каждого хода. Есть намеренно
в этом контракте нет привязки подсказок среды плагина: изменение cwd, ветки или
другие данные среды не должны незаметно изменять кэшированное приглашение сеанса.
Для такого крючка нужен конкретный потребитель и та же семантика, безопасная для заморозки и возобновления.
прежде чем его можно будет добавить.

### Поставлен каталог плагинов-хуков

Поля полезной нагрузки ниже представляют собой точные поля для конкретных событий, предоставляемые каждым местом вызова. Для обратной совместимости `PluginManager` также добавляет `telemetry_schema_version="hermes.observer.v1"` к каждому обратному вызову плагина. Этот устаревший маркер конверта не означает, что все полезные данные перехватчика имеют одну семантическую схему; новые версии контрактов принадлежат их конкретному семейству событий или возможностей.

| Крюк | Категория | Точные сроки и поведение возврата | Явные поля полезной нагрузки | Конфиденциальность/чувствительность |
|---|---|---|---|---|
| [`pre_tool_call`](#pre_tool_call) | Директива/контроль | Один раз перед выполнением; побеждает первая допустимая директива `block` или `approve`, а результаты `modify` частично объединяются с аргументами инструмента. | `tool_name`, `args`, `task_id`, `session_id`, `tool_call_id`, `turn_id`, `api_request_id`, `middleware_trace` | Необработанные аргументы могут содержать пользовательский контент, пути, команды или секреты. |
| `post_tool_call` | наблюдатель | После блокировки, ошибки или успешного результата; возврат игнорируется. | `tool_name`, `args`, `result`, `task_id`, `session_id`, `tool_call_id`, `turn_id`, `api_request_id`, `duration_ms`, `status`, `error_type`, `error_message`, `middleware_trace` | Текст результата/ошибки может содержать произвольный инструментальный или пользовательский контент и секреты. |
| `transform_tool_result` | Трансформировать | После `post_tool_call`, перед добавлением диалога; первая строка заменяет результат. | `tool_name`, `args`, `result`, `task_id`, `session_id`, `tool_call_id`, `turn_id`, `api_request_id`, `duration_ms`, `status`, `error_type`, `error_message` | Предоставляет полный результат и аргументы, привязанные к модели. |
| `transform_terminal_output` | Трансформировать | После захвата ограниченного процесса переднего плана, перед окончательным ограничением вывода; первая строка заменяет вывод. | `command`, `output`, `returncode`, `task_id`, `env_type` | Команда/выход могут содержать учетные данные. |
| `pre_transcription` | Трансформировать | Запускается диспетчером STT после разрешения поставщика и до вызова какой-либо серверной части (встроенной, командной или зарегистрированной в плагине); Результаты dict применяются в порядке регистрации, число побед последнего автора для каждого поля (`prompt`, `language`, `model`; `file_path` доступно только для чтения). | `file_path`, `provider`, `model`, `language`, `prompt`, `source` | Окончательное приглашение загружается настроенному поставщику STT вместе со звуком — не допускайте возврата секретов. |
| `pre_llm_call` | Директива/контроль | Один раз за ход перед циклом; все действительные возвращаемые значения string/`{"context": ...}` объединяются и вставляются в сообщение пользователя. | `session_id`, `task_id`, `turn_id`, `user_message`, `conversation_history`, `is_first_turn`, `model`, `platform`, `parent_session_id`, `sender_id` | Полная история сообщений и разговоров пользователя. |
| `post_llm_call` | наблюдатель | Успешное, непрерывное завершение поворота; возврат игнорируется. | `session_id`, `task_id`, `turn_id`, `user_message`, `assistant_response`, `conversation_history`, `model`, `platform` | Полное приглашение, ответ и история. |
| `transform_llm_output` | Трансформировать | До `post_llm_call` и окончательной доставки; первая непустая строка заменяет ответ. | `response_text`, `session_id`, `model`, `platform` | Полный окончательный текст помощника. |
| `pre_verify` | Директива/контроль | На ограниченном шлюзе проверки отредактированного кода; первая действующая директива continue/block-stop поддерживает ход. | `session_id`, `platform`, `model`, `coding`, `attempt`, `final_response`, `changed_paths` | Проект ответа и измененные пути. |
| `pre_api_request` | наблюдатель | За попытку провайдера, непосредственно перед запросом; возврат игнорируется. | `task_id`, `turn_id`, `api_request_id`, `session_id`, `user_message`, `conversation_history`, `platform`, `model`, `provider`, `base_url`, `api_mode`, `api_call_count`, `retry_count`, `request_messages`, `message_count`, `tool_count`, `approx_input_tokens`, `request_char_count`, `max_tokens`, `started_at`, `middleware_trace`, `request` | Высокая чувствительность: устаревшие `user_message`, `conversation_history` и `request_messages` намеренно являются необработанными; предпочитайте очищенный `request`. |
| `post_api_request` | наблюдатель | После нормализованного успеха поставщика; возврат игнорируется. | `task_id`, `turn_id`, `api_request_id`, `session_id`, `platform`, `model`, `provider`, `base_url`, `api_mode`, `api_call_count`, `api_duration`, `started_at`, `ended_at`, `finish_reason`, `message_count`, `response_model`, `response`, `usage`, `assistant_message`, `assistant_content_chars`, `assistant_tool_call_count` | Доступен очищенный `response`, но необработанный нормализованный `assistant_message` может содержать контент модели/пользователя; `usage` — данные бухгалтерского учета. |
| `api_request_error` | наблюдатель | При каждой неудачной попытке поставщика; возврат игнорируется. | `task_id`, `turn_id`, `api_request_id`, `session_id`, `platform`, `model`, `provider`, `base_url`, `api_mode`, `api_call_count`, `api_duration`, __PH_38

0__, `ended_at`, `status_code`, `retry_count`, `max_retries`, `retryable`, `reason`, `error`, `request` | Текст ошибки может содержать данные поставщика/пользователя; `request` предназначен для дезинфекции. |
| `on_stream_start` | наблюдатель | Отправляется, когда начинается потоковый ответ LLM; доставляется по пути токена через ограниченную очередь, принадлежащую хосту, с одним работником на обратный вызов; возврат игнорируется. | `turn_id`, `iteration`, `session_id`, `model`, `provider`, `surface` | Только идентификаторы и метаданные маршрутизации. |
| `on_stream_delta` | наблюдатель | Отправляется для каждой нормализованной разницы потокового текста через ограниченную очередь наблюдателей; при остановленном обратном вызове удаляются только самые старые события; возврат игнорируется. | `delta`, `kind` (`text` или `reasoning`), `turn_id`, `iteration`, `session_id`, `model`, `provider`, `surface` | Дельта-текст — это необработанные выходные данные модели; рассуждения о дельтах требуют согласия `plugins.stream_reasoning_deltas`. |
| `on_stream_end` | наблюдатель | Отправляется при завершении ответа потоковой передачи или ошибке после закрытия потока; возврат игнорируется. | `final_text`, `finished`, `error`, `turn_id`, `iteration`, `session_id`, `model`, `provider`, `surface` | Полный собранный текст ответа; текст ошибки может включать данные провайдера. |
| `on_interim_message` | наблюдатель | Отправляется, когда сообщение помощника в середине цикла отображается перед окончательным ответом (потоковое или непотоковое); возврат игнорируется. | `text`, `already_streamed`, `turn_id`, `iteration`, `session_id`, `model`, `provider`, `surface` | Полный текст временного помощника. |
| `transform_api_error_classification` | Трансформировать | При каждой неудачной попытке поставщика вверху встроенного классификатора; все обратные вызовы выполняются, затем побеждает первый словарь с действительным `reason` (сначала выполнить все, затем выбрать), а пропущенные действительные результаты регистрируют предупреждение во время выполнения. Только плагины Python. | `provider`, `model`, `status_code`, `error_type`, `error_code`, `error_message`, `error_body`, `error`, `approx_tokens`, `context_length`, `num_messages` | `error_message` и `error_body` могут содержать необработанные данные поставщика/пользователя. |
| `on_session_start` | наблюдатель | Первый ход новой сессии; возврат игнорируется. | `session_id`, `model`, `platform` | Только идентификаторы и метаданные маршрутизации. |
| `on_session_end` | наблюдатель | Канонически на каждом ходу финализация; Выходы CLI/TUI имеют дополнительные уменьшенные устаревшие формы. Возврат игнорируется. | Канонический: `session_id`, `task_id`, `turn_id`, `completed`, `failed`, `interrupted`, `turn_exit_reason`, `model`, `platform`; пути выхода могут добавлять `reason`/`api_request_id` и опускать поля. | Идентификаторы, модель/платформа и результат; каноническая полезная нагрузка не имеет тела сообщения. |
| `on_session_finalize` | наблюдатель | Демонтаж CLI/TUI/шлюза через `finalize_session`; Отключение шлюза или истечение срока его действия могут завершиться без перезагрузки. Возврат игнорируется. | Зависит от поверхности `session_id`, `platform`, дополнительно `reason`, `old_session_id`, `new_session_id` | Идентификаторы сеанса и маршрутизации. |
| `on_session_reset` | наблюдатель | Граница и шлюз сеанса CLI/TUI после существования замещающего сеанса; возврат игнорируется. | CLI: `session_id`, `platform`, `reason`; ТУИ: `session_id`, `platform`; шлюз: те плюс `reason`, `old_session_id`, `new_session_id` | Идентификаторы сеанса и маршрутизации. |
| `on_skill_lifecycle` | наблюдатель | После авторитетного изменения состояния использования навыков; возврат игнорируется. | `action`, `skill_name`, `provenance`, `task_id`, `session_id`, `use_count`, `reused`, `reuse_after_patch` | Раскрывает название и происхождение местного навыка. |
| `subagent_start` | наблюдатель | Ребенок построился и собирается бежать; возврат игнорируется. | `parent_session_id`, `parent_turn_id`, `parent_subagent_id`, `child_session_id`, `child_subagent_id`, `child_role`, `child_goal` | Дочерняя цель может содержать контент пользователя/проекта. |
| `subagent_stop` | наблюдатель | Детский выход; возврат игнорируется. | `parent_session_id`, `parent_turn_id`, `child_session_id`, `child_role`, `child_summary`, `child_status`, `tool_call_history`, `duration_ms` | Сводные и отредактированные метаданные истории инструмента могут раскрыть структуру проекта. |
| `pre_gateway_dispatch` | Директива/контроль | Входящее невнутреннее сообщение перед аутентификацией/сопряжением/отправкой; первый действительный `skip`, `rewrite` или `allow` управляет потоком. | `event`, `gateway`, `session_store` | Внутрипроцессные объекты с чрезвычайно привилегированными правами предоставляют входящие данные пользователя/маршрутизации и дескрипторы хоста. |
| `gateway_platform_event` | наблюдать

эээ | После успешной авторизации на уровне профиля шлюза, когда поддерживаемое событие платформы нормализуется на границе шлюза (Telegram: реакции, редактирование сообщений; Discord: редактирование/удаление сообщений, создание/переименование потока); возврат игнорируется. | `platform`, `event_type`, `payload` (диктофон для конкретного типа события — см. контракты для каждого события ниже) | Только нормализованный конверт обычного текста; необработанные объекты SDK, дескрипторы адаптеров и клиенты-боты никогда не предоставляются. |
| `pre_command` | наблюдатель | Распознанная косая черта, которая будет отправлена ​​до запуска обработчика, при отправке через CLI и шлюзе по холодному пути; return игнорируется в v1 (диктанты в форме директив регистрируются при отладке). Команды перехвата агента запуска шлюза (`/stop`, `/approve` во время активного запуска) намеренно исключены — аварийные люки плоскости управления должны оставаться за пределами досягаемости плагина. | `surface` (`"cli"` \| `"gateway"`), `command` (каноническое имя), `alias_used`, `args_raw`, `session_key`, `platform` | `args_raw` может содержать пользовательский контент или секреты, введенные после команды. |
| `pre_approval_request` | наблюдатель | Перед запросом или интеллектуальным одобрением; возврат игнорируется. | `command`, `description`, `pattern_key`, `pattern_keys`, `session_key`, `surface`, `turn_id`, `tool_call_id` | Команда может содержать секреты; умная подготовка наблюдателя принудительно редактирует, но не все поверхности имеют идентичное редактирование. |
| `post_approval_response` | наблюдатель | После принятия решения, тайм-аута или сбоя уведомления шлюза; возврат игнорируется. | `command`, `description`, `pattern_key`, `pattern_keys`, `session_key`, `surface`, `turn_id`, `tool_call_id`, `choice`; умный путь может добавить `decided_by` | Та же чувствительность команд плюс метаданные решения. |
| `kanban_task_claimed` | наблюдатель | После фиксации заявки, в процессе диспетчера перед появлением работника; возврат игнорируется. | `task_id`, `profile_name`, `board`, `assignee`, `run_id` | Идентификаторы доски/задачи/профиля/правопреемника. |
| `kanban_task_completed` | наблюдатель | После завершения и очистки, обычно в рабочем процессе; возврат игнорируется. | `task_id`, `profile_name`, `board`, `assignee`, `run_id`, `summary` | Сводка может содержать контент проекта/пользователя. |
| `kanban_task_blocked` | наблюдатель | После заблокированного перехода; путь ожидания зависимости срабатывает до завершения транзакции. Возврат игнорируется. | `task_id`, `profile_name`, `board`, `assignee`, `run_id`, `reason` | Причина может содержать контент проекта/пользователя. |
| `on_kanban_worker_spawned` | наблюдатель | После того, как `spawn_fn` возвращается и рабочий PID сохраняется; работает внутри блокировки диспетчеризации, обеспечивает быстроту обратных вызовов. Возврат игнорируется. | `task_id`, `profile_name`, `board`, `assignee`, `run_id`, `worker_pid`, `workspace_path` | `workspace_path` — это путь к файловой системе, который может раскрывать макет проекта или имена пользователей. |
| `on_kanban_worker_exited` | наблюдатель | На основе такта: после того, как `detect_crashed_workers` восстанавливает задачу с мертвым PID и восстановление фиксируется. Возврат игнорируется. | `task_id`, `profile_name`, `board`, `assignee`, `run_id`, `worker_pid`, `exit_kind`, `exit_code`, `outcome`, `retry_status` ​​| Только идентификаторы и выходные метаданные. |
| `on_kanban_worker_stale_claim` | наблюдатель | После возврата претензии с истекшим сроком жизни; Расширения live-PID не запускаются. Возврат игнорируется. | `task_id`, `profile_name`, `board`, `assignee`, `run_id`, `worker_pid`, `heartbeat_stale`, `retry_status` | Только идентификаторы и метаданные утверждений. |
| `on_kanban_task_updated` | наблюдатель | После фиксации поля задачи запись вне жизненного цикла заявки/завершения/блокировки (назначение, переопределение, редакторы информационной панели). Возврат игнорируется. | `task_id`, `profile_name`, `board`, `assignee`, `run_id`, `changed_fields` | `changed_fields` содержит только имена полей, но не значения; названные значения заголовка/тела в базе данных доски могут содержать контент пользователя/проекта. |
| `on_kanban_dispatch_tick` | наблюдатель | Один раз за тик диспетчера, строго после снятия блокировки диспетчеризации; Простой и спорный тик тоже срабатывает. Возврат игнорируется. | `board`, `profile_name`, `dry_run`, `outcome`, `result` | `result` — это `DispatchResult` галочки, который содержит идентификаторы задач, исполнителей и пути к рабочей области. |

---

### Перехватчики потокового вывода

Эти перехватчики, доступные только для наблюдателей, позволяют плагинам использовать потоковые выходные данные LLM для телеметрии, интерактивных информационных панелей или конвейеров TTS без изменения ответа. Они доставляются через ограниченные очереди, принадлежащие хосту, с одним фоновым рабочим процессом на каждый зарегистрированный обратный вызов, поэтому обратные вызовы плагина никогда не выполняются встроенными в путь токена. Если один обратный вызов останавливается, только очередь этого обратного вызова может заполнить и удалить самое старое ожидающее событие-наблюдатель; другие наблюдатели продолжают получать информацию о событиях самостоятельно.

Зарегистрируйте их, как и любой другой хук плагина:

```python
def on_delta(delta, kind, model, provider, **kwargs):
    if kind == "text":
        print(delta, end="", flush=True)

def register(ctx):
    ctx.register_hook("on_stream_delta", on_delta)
```

Общие поля для всех четырех хуков:

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `turn_id` | `str` | Непрозрачный идентификатор поворота, если доступен |
| `iteration` | `int` | Текущая итерация API-вызова/инструментального цикла |
| `session_id` | `str` | Текущий идентификатор сеанса Hermes |
| `model` | `str` | Идентификатор активной модели |
| `provider` | `str` | Имя активного провайдера |
| `surface` | `str` | Вызывающая поверхность, например `cli`, `discord`, `telegram` |

Дополнительные поля:

| Крюк | Дополнительные поля |
|------|--------------|
| `on_stream_start` | нет |
| `on_stream_delta` | `delta: str`, `kind: "text" | "reasoning"` |
| `on_stream_end` | `final_text: str`, `finished: bool`, `error: str | None` |
| `on_interim_message` | `text: str`, `already_streamed: bool` |

`on_interim_message` также может срабатывать после ответа, не связанного с потоковой передачей, поэтому регистрация только этого хука не приводит к принудительному вызову провайдера на потоковый транспорт.

По умолчанию дельты рассуждений не доступны плагинам. Подтвердите свое согласие:

```yaml
plugins:
  stream_reasoning_deltas: true
```

Возвращаемые значения игнорируются. Чтобы поток оставался быстрым, обратные вызовы должны ставить свою работу в очередь и быстро возвращаться. Исключения регистрируются и не останавливают поток.

---

### `pre_tool_call`

Запускается **непосредственно перед** выполнением каждого инструмента — как встроенного, так и подключаемого.

**Подпись обратного вызова:**

```python
def my_callback(tool_name: str, args: dict, task_id: str, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `tool_name` | `str` | Имя инструмента, который будет запущен (например, `"terminal"`, `"web_search"`, `"read_file"`) |
| `args` | `dict` | Аргументы, переданные моделью инструменту |
| `task_id` | `str` | Идентификатор сеанса/задачи. Пустая строка, если не установлена. |

**Срабатывает:** В `model_tools.py`, внутри `handle_function_call()`, перед запуском обработчика инструмента. Срабатывает один раз за вызов инструмента — если модель вызывает 3 инструмента параллельно, это срабатывает 3 раза.

**Возвращаемое значение — заблокировать или потребовать одобрения:**

```python
return {"action": "block", "message": "Reason the tool call was blocked"}
# or
return {"action": "approve", "message": "Why approval is required", "rule_key": "optional:scope"}
```

Выигрывает первая действующая директива (сначала регистрируются плагины Python, затем перехватчики оболочки). `block` требует непустой `message` и замыкает инструмент с этим текстом, поскольку ошибка возвращается в модель. `approve` передает вызов существующим воротам одобрения человеком; `message` и `rule_key` являются необязательными, а отказ, тайм-аут или ошибка шлюза завершаются неудачно. Другие возвращаемые значения игнорируются, поэтому существующие обратные вызовы только для наблюдателей продолжают работать без изменений.

**Возвращаемое значение — перепишите аргументы инструмента:**

```python
return {"action": "modify", "args": {"new_string": "fixed content"}}
```

Возвращенный словарь `args` частично объединяется с исходными аргументами инструмента перед его выполнением. Накапливается несколько перехватчиков `modify` — ключи каждого перехватчика объединяются в один накопленный словарь, построенный на основе исходных аргументов, поэтому перехват A, меняющий `path`, и перехват B, изменяющий `content`, оба сохраняются. Если два хука изменяют один и тот же ключ, побеждает более поздний хук.

Хуки оболочки также принимают формат, совместимый с Claude Code:

```json
{"decision": "modify", "tool_input": {"new_string": "fixed content"}}
```

Оба формата внутренне нормализованы до `{"action": "modify", "args": {...}}`.

**Случаи использования**: ведение журналов, контрольные журналы, счетчики вызовов инструментов, блокировка опасных операций, ограничение скорости, применение политик для каждого пользователя, очистка аргументов, перезапись пути, внедрение параметров по умолчанию.

**Пример — журнал аудита вызовов инструментов:**

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

**Пример — предупреждение об опасных инструментах:**

```python
DANGEROUS = {"terminal", "write_file", "patch"}

def warn_dangerous(tool_name, **kwargs):
    if tool_name in DANGEROUS:
        print(f"⚠ Executing potentially dangerous tool: {tool_name}")

def register(ctx):
    ctx.register_hook("pre_tool_call", warn_dangerous)
```

---

### `post_tool_call`

Срабатывает **сразу после** возобновления выполнения каждого инструмента.

**Подпись обратного вызова:**

```python
def my_callback(tool_name: str, args: dict, result: str, task_id: str,
                duration_ms: int, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `tool_name` | `str` | Имя только что запущенного инструмента |
| `args` | `dict` | Аргументы, переданные моделью инструменту |
| `result` | `str` | Возвращаемое значение инструмента (всегда строка JSON) |
| `task_id` | `str` | Идентификатор сеанса/задачи. Пустая строка, если не установлена. |
| `duration_ms` | `int` | Время, которое заняла отправка инструмента, в миллисекундах (измеряется с помощью `time.monotonic()` около `registry.dispatch()`). |

**Срабатывает:** В `model_tools.py`, внутри `handle_function_call()`, после возврата обработчика инструмента. Срабатывает один раз за вызов инструмента. **Не** срабатывает, если инструмент вызвал необработанное исключение (вместо этого ошибка перехватывается и возвращается в виде строки ошибки JSON, а `post_tool_call` срабатывает с этой строкой ошибки как `result`).

**Возвращаемое значение:** игнорируется.

**Случаи использования**. Регистрация результатов инструментов, сбор показателей, отслеживание показателей успеха/неуспехов инструментов, информационные панели о задержках, оповещения о бюджете для каждого инструмента, отправка уведомлений о завершении работы определенных инструментов.

**Пример: отслеживание показателей использования инструмента:**

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

### `pre_llm_call`

Срабатывает **один раз за ход**, до начала цикла вызова инструмента. Все действительные возвраты обратного вызова агрегируются в порядке плагинов и вводятся в пользовательское сообщение текущего хода.

**Подпись обратного вызова:**

```python
def my_callback(session_id: str, user_message: str, conversation_history: list,
                is_first_turn: bool, model: str, platform: str, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `session_id` | `str` | Уникальный идентификатор текущей сессии |
| `user_message` | `str` | Исходное сообщение пользователя на этот ход (до применения каких-либо навыков) |
| `conversation_history` | `list` | Копия полного списка сообщений (формат OpenAI: `[{"role": "user", "content": "..."}]`) |
| `is_first_turn` | `bool` | `True`, если это первый ход новой сессии, `False` на последующих ходах |
| `model` | `str` | Идентификатор модели (например, `"anthropic/claude-sonnet-4.6"`) |
| `platform` | `str` | Где запущен сеанс: `"cli"`, `"telegram"`, `"discord"` и т. д. |

**Срабатывает:** В `run_agent.py`, внутри `run_conversation()`, после сжатия контекста, но перед основным циклом `while`. Срабатывает один раз за вызов `run_conversation()` (т. е. один раз за ход пользователя), а не один раз за вызов API в цикле инструмента.

**Возвращаемое значение:** Если обратный вызов возвращает dict с ключом `"context"` или простую непустую строку, текст добавляется к сообщению пользователя текущего хода. Верните `None` для отсутствия инъекции.

```python
# Inject context
return {"context": "Recalled memories:\n- User likes Python\n- Working on hermes-agent"}

# Plain string (equivalent)
return "Recalled memories:\n- User likes Python"

# No injection
return None
```

**При внедрении контекста:** Всегда **сообщение пользователя**, а не системное приглашение. При этом кэш подсказок сохраняется — системное приглашение остается одинаковым на протяжении всех ходов, поэтому кэшированные жетоны используются повторно. Системная подсказка — территория Гермеса (наведение модели, применение инструментов, личность, навыки). Плагины вносят контекст вместе с вводом пользователя.

Чистое пользовательское сообщение `content` остается неизменным. Для обеспечения стабильности воспроизведения и кэша подсказок Hermes может сохранять точное сообщение, связанное с API, включая контекст, внедренный плагином, в боковой панели `api_content` строки.

Когда **несколько плагинов** возвращают контекст, их выходные данные объединяются двойными символами новой строки в порядке обнаружения плагинов (в алфавитном порядке по имени каталога).

**Случаи использования**: вызов памяти, внедрение контекста RAG, ограждения, пошаговая аналитика.

**Пример — вызов памяти:**

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

**Пример: ограждения:**

```python
POLICY = "Never execute commands that delete files without explicit user confirmation."

def guardrails(**kwargs):
    return {"context": POLICY}

def register(ctx):
    ctx.register_hook("pre_llm_call", guardrails)
```

---

### `post_llm_call`

Срабатывает **один раз за ход**, после завершения цикла вызова инструмента и получения окончательного ответа агентом. Срабатывает только при **успешных** поворотах — не срабатывает, если ход был прерван.

**Подпись обратного вызова:**

```python
def my_callback(session_id: str, user_message: str, assistant_response: str,
                conversation_history: list, model: str, platform: str, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `session_id` | `str` | Уникальный идентификатор текущей сессии |
| `user_message` | `str` | Исходное сообщение пользователя для этого хода |
| `assistant_response` | `str` | Окончательный текстовый ответ агента на этот ход |
| `conversation_history` | `list` | Копия полного списка сообщений после завершения хода |
| `model` | `str` | Идентификатор модели |
| `platform` | `str` | Где проходит сеанс |

**Срабатывает:** В `run_agent.py`, внутри `run_conversation()`, после завершения цикла инструмента с окончательным ответом. Защищено `if final_response and not interrupted` — поэтому оно **не** срабатывает, когда пользователь прерывает операцию в середине хода или когда агент достигает предела итерации, не выдавая ответа.

**Возвращаемое значение:** игнорируется.

**Случаи использования**: синхронизация данных разговоров с внешней системой памяти, вычисление показателей качества ответов, протоколирование сводок ходов, запуск последующих действий.

**Пример — синхронизация с внешней памятью:**

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

**Пример: отслеживать длину ответов:**

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

### `pre_verify`

Срабатывает **один раз за ход, когда агент редактирует код**, непосредственно перед его завершением (после встроенной защиты проверки при остановке). Это шлюз политики пользователя/плагина: обратный вызов может поддерживать работу агента — запускать проверку, откладывать ее, наводить порядок — вместо того, чтобы позволить ему остановиться.

Поставляемое компанией Hermes руководство по проверке не является ловушкой по умолчанию `pre_verify`. Он добавляется к подталкиванию проверки на остановке на основе фактических данных, когда в отредактированном коде отсутствуют свежие доказательства проверки, поэтому он не создает второй путь продолжения по умолчанию. Установите `agent.verify_guidance: false`, чтобы встроенные доказательства были краткими.

**Подпись обратного вызова:**

```python
def my_callback(session_id: str, platform: str, model: str, coding: bool,
                attempt: int, final_response: str, changed_paths: list, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `session_id` | `str` | Уникальный идентификатор текущей сессии |
| `platform` | `str` | Где запущен сеанс (`"cli"`, `"telegram"`, …) |
| `model` | `str` | Идентификатор модели |
| `coding` | `bool` | Независимо от того, находится ли очередь в позиции кодирования (в рабочей области кода) — сосредоточьтесь на этом |
| `attempt` | `int` | Сколько раз этот поворот уже был поддвинут (0 в первый раз) — самодроссель на этом |
| `final_response` | `str` | Ответ, который собирается дать агент |
| `changed_paths` | `list` | Файлы, которые агент редактировал в этом ходе (отсортированные, здесь всегда непустые) |

Назначьте перехватчик контексту кодирования, проверив `coding`, и сделайте его одноразовым с помощью `attempt` (оба перехватчика оболочки считываются из `.extra`), точно так же, как перехватчик `pre_tool_call` охватывает `tool_name` — таким образом, вы можете зарегистрировать несколько перехватчиков `pre_verify`, каждый из которых срабатывает только там, где он должен.

**Срабатывает:** В `agent/conversation_loop.py` в тот момент, когда агент принимает окончательный ответ, сразу после проверки приостановки — но только тогда, когда агент редактировал код на этом ходу и зарегистрирован хотя бы один крючок `pre_verify`.

**Возвращаемое значение — продолжение работы агента:**

```python
return {"action": "continue", "message": "Run the formatter on your changes, then finish."}
```

`message` добавляется как синтетический пользовательский ход, и цикл запускается снова. Форма остановки Claude-Code (`{"decision": "block", "reason": "..."}`, где блокировка остановки означает *продолжать движение*) также принимается. Директива без сообщения или любого другого возврата позволяет завершить ход.

**Ограничение:** последовательные директивы continue за один ход ограничены `agent.max_verify_nudges` (по умолчанию 3), поэтому перехватчик, который всегда говорит continue, никогда не сможет перехватить цикл. Попытка ответа сохраняется в истории, но не отображается пользователю, пока агента подталкивают.

**Сделайте его идемпотентным:** крючок срабатывает повторно после каждого подталкивания, поэтому ворота включаются `attempt` (`if attempt: return None`) — в противном случае он просто подталкивает, пока не будет достигнута граница.

**Случаи использования**: откладывать тесты/проверки во время творческой итерации, требовать зеленые проверки для определенных путей, блокировать «готово», пока не появится запись в журнале изменений, запускать контрольный список проверки для конкретного проекта.

**Пример: отложить проверку творческой работы с пользовательским интерфейсом, ограниченную + одноразовую:**

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

Для постоянного руководства, которое должно формировать встроенный подталкивание к недостающим доказательствам, используйте `agent.verify_guidance`. Для более широких правил кодирования, которые не требуют *воротной* проверки, отдайте предпочтение `agent.coding_instructions` в `config.yaml` — это соответствует заданию по кодированию и не требует дополнительных затрат.

---

### `transform_api_error_classification`

Срабатывает один раз за каждый неудачный вызов API, в верхней части `agent/error_classifier.classify_api_error()`, перед встроенным конвейером. Плагины провайдера используют его для устранения ошибок своего провайдера без исправлений ядра. Это изменение поведения (семейство преобразований): возвращаемая классификация управляет повторными попытками, сжатием, ротацией учетных данных и резервной маршрутизацией.

Обратные вызовы получают проанализированный контекст ошибки в виде кваргов — `provider` (самостоятельная область), `model`, `status_code`, `error_type`, `error_code`, `error_message`, `error_body`, `error`, `approx_tokens`, `context_length`, `num_messages`. Верните `None`, чтобы отклонить запрос, или запрос, чтобы заявить об ошибке:

```python
return {"reason": "model_not_found",   # required: a FailoverReason name
        "retryable": False, "should_fallback": True}  # optional recovery-hint overrides
```

Отправка осуществляется по принципу «сначала выполнить все, затем выбрать»: выполняется каждый обратный вызов, сбои изолируются, и побеждает первый действительный результат в порядке регистрации (действительные, но проигрышные результаты регистрируются во время выполнения). Недействительные слова и неизвестные причины пропускаются, поэтому сломанный плагин никогда не сможет нарушить классификацию.

**Конфиденциальность:** `error_message` и `error_body` могут содержать неотредактированные данные поставщика. **Только для плагинов Python** — регистрация оболочки отклоняется при разборе конфигурации с предупреждением.

---

### `on_session_start`

Срабатывает **один раз** при создании нового сеанса. **Не** срабатывает при продолжении сеанса (когда пользователь отправляет второе сообщение в существующем сеансе).

**Подпись обратного вызова:**

```python
def my_callback(session_id: str, model: str, platform: str, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `session_id` | `str` | Уникальный идентификатор нового сеанса |
| `model` | `str` | Идентификатор модели |
| `platform` | `str` | Где проходит сеанс |

**Срабатывает:** В `run_agent.py`, внутри `run_conversation()`, во время первого поворота нового сеанса — в частности, после построения системного приглашения, но до запуска цикла инструмента. Проверка `if not conversation_history` (нет предыдущих сообщений = новый сеанс).

**Возвращаемое значение:** игнорируется.

**Примеры использования:** Инициализация состояния на уровне сеанса, разогрев кешей, регистрация сеанса во внешней службе, запуск сеанса регистрации.

**Пример — инициализация кэша сеанса:**

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

### `on_session_end`

Срабатывает в **самом конце** каждого вызова `run_conversation()`, независимо от результата. Также срабатывает из обработчика выхода CLI, если агент находился в середине хода, когда пользователь вышел.

**Подпись обратного вызова:**

```python
def my_callback(session_id: str, completed: bool, interrupted: bool,
                model: str, platform: str, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `session_id` | `str` | Уникальный идентификатор сеанса |
| `completed` | `bool` | `True`, если агент выдал окончательный ответ, `False` в противном случае |
| `interrupted` | `bool` | `True`, если ход был прерван (пользователь отправил новое сообщение, `/stop`, или вышел) |
| `model` | `str` | Идентификатор модели |
| `platform` | `str` | Где проходит сеанс |

**Пожары:** В двух местах:
1. **`run_agent.py`** — в конце каждого вызова `run_conversation()`, после очистки. Всегда срабатывает, даже если ход ошибочный.
2. **`cli.py`** — в обработчике atexit CLI, но **только**, если агент находился в середине хода (`_agent_running=True`), когда произошел выход. Это перехватывает Ctrl+C и `/exit` во время обработки. В данном случае `completed=False` и `interrupted=True`.

**Возвращаемое значение:** игнорируется.

**Случаи использования**: очистка буферов, закрытие соединений, сохранение состояния сеанса, регистрация продолжительности сеанса, очистка ресурсов, инициализированных в `on_session_start`.

**Пример — промывка и очистка:**

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

**Пример — отслеживание продолжительности сеанса:**

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

### `on_session_finalize`

Срабатывает, когда CLI или шлюз **обрывает** активный сеанс — например, когда пользователь запускает `/new`, сборщик мусора шлюза завершает сеанс бездействия или CLI завершает работу с активным агентом. Используйте его для сброса состояния, привязанного к идентификатору исходящего сеанса. При сбросе шлюза сеанс замены уже существует до запуска этого обратного вызова.

**Подпись обратного вызова:**

```python
def my_callback(session_id: str | None, platform: str, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `session_id` | `str` или `None` | Идентификатор исходящего сеанса. Может быть `None`, если активного сеанса не существовало. |
| `platform` | `str` | `"cli"` или название платформы обмена сообщениями (`"telegram"`, `"discord"` и т. д.). |

**Срабатывает:** при отключении CLI/TUI, а также при сбросе шлюза, завершении работы или путях истечения срока простоя. Закрытие шлюза и истечение срока действия могут быть завершены без соответствующего `on_session_reset`.

**Возвращаемое значение:** игнорируется.

**Случаи использования**. Сохранение окончательных показателей сеанса до того, как идентификатор сеанса будет удален, закрытие ресурсов каждого сеанса, создание окончательного события телеметрии, удаление операций записи в очереди.

---

### `on_session_reset`

Срабатывает на границе сеанса CLI или TUI или когда шлюз **заменяет новый ключ сеанса** для активного чата. Это позволяет плагинам реагировать на очищенное состояние разговора, не дожидаясь следующего `on_session_start`.

**Подпись обратного вызова:**

```python
def my_callback(session_id: str, platform: str, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `session_id` | `str` | Идентификатор нового сеанса (уже изменен на новое значение). |
| `platform` | `str` | `"cli"`, `"tui"` или название платформы обмена сообщениями. |
| `reason` | `str`, необязательно | Присутствует в путях сброса CLI и шлюза. |
| `old_session_id` | `str`, необязательно | Идентификатор исходящего сеанса только для шлюза. |
| `new_session_id` | `str`, необязательно | Идентификатор сеанса замены только для шлюза. |

**Срабатывает:** CLI предоставляет `session_id`, `platform` и `reason`; TUI поставляет `session_id` и `platform`; шлюз добавляет `reason`, `old_session_id` и `new_session_id` после выделения замещающего ключа. При сбросе шлюза порядок следующий: создать и сохранить замену → `on_session_finalize(old_id)` → `on_session_reset(new_id)` → `on_session_start(new_id)` на первом входящем повороте.

**Возвращаемое значение:** игнорируется.

**Примеры использования**: сброс кешей каждого сеанса с ключом `session_id`, создание аналитики с ротацией сеансов, загрузка нового сегмента состояния.

---

См. **[Руководство по созданию плагинов](/developer-guide/plugins)** для получения полного пошагового руководства, включая схемы инструментов, обработчики и расширенные шаблоны перехватчиков.

---

### `subagent_start`

Срабатывает **один раз для каждого дочернего агента** после того, как `delegate_task` создал дочерний агент `AIAgent` и до того, как этот дочерний агент будет запущен. Независимо от того, делегируете ли вы одну задачу или группу из трех, этот крючок срабатывает один раз для каждого дочернего элемента.

Этот крючок специфичен для жизненного цикла делегирования/субагента. Это не универсальный шлюз «перед любым вызовом агента» для шлюза, CLI, cron, пакетной обработки, MoA или других выполнения агента, инициированного бегуном.

**Подпись обратного вызова:**

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

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `parent_session_id` | `str \| None` | Идентификатор сеанса делегирующего родительского агента. |
| `parent_turn_id` | `str` | Идентификатор хода родительского агента, запрошенного делегирования, если доступен. |
| `parent_subagent_id` | `str \| None` | Идентификатор родительского субагента, когда этот дочерний элемент был порожден другим субагентом; `None` для родительских агентов верхнего уровня. |
| `child_session_id` | `str \| None` | Идентификатор сеанса, выделенный для дочернего агента. |
| `child_subagent_id` | `str` | Стабильный идентификатор субагента, используемый для наблюдения и контроля делегирования. |
| `child_role` | `str` | Эффективная дочерняя роль после применения политики делегирования, например `"leaf"` или `"orchestrator"`. |
| `child_goal` | `str` | Делегированная цель/подсказка, которую будет выполнять дочерний агент. |

**Срабатывает:** В `tools/delegate_tool.py`, внутри `_build_child_agent()`, после того, как дочерний объект `AIAgent` был создан и аннотирован метаданными идентификатора субагента, и до того, как `_run_single_child()` запустит дочерний элемент.

**Возвращаемое значение:** игнорируется. Это только крючок наблюдателя; возврат значения не блокирует и не изменяет запуск дочернего агента.

**Случаи использования**: ведение журналов создания субагентов, сопоставление отношений между родительскими и дочерними сеансами, отслеживание вложенных деревьев делегирования, создание записей предварительного аудита, предварительное выделение ресурсов наблюдения для каждого дочернего агента.

**Пример — создание субагента журнала:**

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

:::информация
`subagent_start` полезен для наблюдения за делегированием, но не является обработкой политики блокировки. Чтобы заблокировать делегирование до создания дочернего элемента, используйте [`pre_tool_call`](#pre_tool_call), чтобы заблокировать вызов инструмента `delegate_task`.
:::

---

### `subagent_stop`

Срабатывает **один раз для каждого дочернего агента** после завершения `delegate_task`. Независимо от того, делегировали ли вы одну задачу или пакет из трех, этот крючок срабатывает один раз для каждого дочернего процесса, сериализуемого в родительском потоке.

**Подпись обратного вызова:**

```python
def my_callback(parent_session_id: str, child_role: str | None,
                child_summary: str | None, child_status: str,
                tool_call_history: list[dict], duration_ms: int, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `parent_session_id` | `str` | Идентификатор сеанса делегирующего родительского агента |
| `child_role` | `str \| None` | Тег роли Оркестратора установлен для дочернего элемента (`None`, если эта функция не включена) |
| `child_summary` | `str \| None` | Окончательный ответ, который ребенок возвращает родителю |
| `child_status` | `str` | `"completed"`, `"failed"`, `"interrupted"` или `"error"` |
| `tool_call_history` | `list[dict]` | Упорядоченные вызовы инструментов только с метаданными: `tool_name`, ограниченные `tool_input`, `input_bytes`, `output_bytes` и `status`; необработанные входные и выходные данные исключены |
| `duration_ms` | `int` | Время, потраченное настенными часами на бег ребенка, в миллисекундах |

**Срабатывает:** В `tools/delegate_tool.py`, после `ThreadPoolExecutor.as_completed()` истощаются все дочерние фьючерсы. Запуск передается родительскому потоку, поэтому авторам хуков не нужно думать о параллельном выполнении обратного вызова.

**Возвращаемое значение:** игнорируется.

**Случаи использования**: ведение журнала действий по оркестрации, накопление дочерней длительности для выставления счетов, запись записей аудита после делегирования.

**Пример: журнал действий оркестратора:**

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

:::информация
При интенсивном делегировании (например, роли оркестратора × 5 листьев × глубина вложенности) `subagent_stop` срабатывает много раз за ход. Обеспечьте быстрый обратный вызов; перенести дорогостоящую работу в фоновую очередь.
:::

---

### `pre_gateway_dispatch`

Срабатывает **один раз для каждого входящего `MessageEvent`** на шлюзе, после защиты внутренних событий, но **перед** аутентификацией/сопряжением и отправкой агента. Это точка перехвата для политик потока сообщений на уровне шлюза (окна только для прослушивания, передача вручную, маршрутизация для каждого чата и т. д.), которые не вписываются ни в один адаптер отдельной платформы.

**Подпись обратного вызова:**

```python
def my_callback(event, gateway, session_store, **kwargs):
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `event` | `MessageEvent` | Нормализованное входящее сообщение (имеет `.text`, `.source`, `.message_id`, `.internal` и т. д.). |
| `gateway` | `GatewayRunner` | Активный исполнитель шлюза, поэтому плагины могут вызывать `gateway.adapters[platform].send(...)` для ответов по побочному каналу (уведомления владельца и т. д.). |
| `session_store` | `SessionStore` | Для приема скрытой расшифровки через `session_store.append_to_transcript(...)`. |

**Срабатывает:** В `gateway/run.py`, внутри `GatewayRunner._handle_message()`, сразу после вычисления `is_internal`. **Внутренние события полностью пропускают перехват** (они генерируются системой (завершения фоновых процессов и т. д.) и не должны контролироваться политикой, ориентированной на пользователя).

**Возвращаемое значение:** `None` или dict. Побеждает первое признанное действие; остальные результаты плагина игнорируются. Исключения в обратных вызовах плагинов перехватываются и протоколируются; шлюз всегда не переходит к нормальной отправке в случае ошибки.

| Вернуться | Эффект |
|--------|--------|
| `{"action": "skip", "reason": "..."}` | Отбросить сообщение — ни ответа агента, ни процесса сопряжения, ни аутентификации. Предполагается, что плагин справился с этим (например, автоматически включился в расшифровку). |
| `{"action": "rewrite", "text": "new text"}` | Замените `event.text`, затем продолжите обычную отправку с измененным событием. Полезно для объединения буферизованных внешних сообщений в одно приглашение. |
| `{"action": "allow"}` / `None` | Обычная отправка — запускает полную цепочку аутентификации/сопряжения/агентского цикла. |

**Примеры использования**: групповые чаты только для прослушивания (отвечают только при наличии тегов; буферизуют окружающие сообщения в контекст); передача сообщений человеком (бесшумная обработка сообщений клиента, в то время как владелец обрабатывает чат вручную); ограничение скорости по профилям; маршрутизация на основе политик.

**Пример: отключите неавторизованные DM в автоматическом режиме, не активируя код сопряжения:**

```python
def deny_unauthorized_dms(event, **kwargs):
    src = event.source
    if src.chat_type == "dm" and not _is_approved_user(src.user_id):
        return {"action": "skip", "reason": "unauthorized-dm"}
    return None

def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", deny_unauthorized_dms)
```

**Пример: переписать буфер окружающего сообщения в одно приглашение при упоминании:**

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

### `gateway_platform_event`

Срабатывает для поддерживаемых собственных событий платформы только **после** успешной проверки авторизации шлюза на уровне профиля. Обратный вызов получает простые словари; необработанные объекты SDK, дескрипторы адаптеров, клиенты-боты и контексты обратного вызова никогда не являются частью этого стабильного контракта.

Реакции на сообщения Telegram были первым поддерживаемым событием; Последовали изменения, удаления сообщений и события жизненного цикла потока:

```python
def on_platform_event(platform, event_type, payload, **kwargs):
    if platform == "telegram" and event_type == "reaction":
        print(payload["chat_id"], payload["message_id"], payload["emojis"])
    elif event_type == "message_edited":
        print(platform, payload["chat_id"], payload["message_id"], payload["text"])

def register(ctx):
    ctx.register_hook("gateway_platform_event", on_platform_event)
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `platform` | `str` | Идентификатор стабильной платформы (`"telegram"`, `"discord"`). |
| `event_type` | `str` | Идентификатор локального контракта события (см. таблицу ниже). |
| `payload` | `dict` | Поля, специфичные для типа события, описаны ниже для каждого типа события. |

Каждая полезная нагрузка является аддитивной и зависит от события; версии полезной нагрузки монолитного шлюза не существует. Все идентификаторы являются строками; отсутствующие/недоступные поля — `None`, никогда не догадывался. Неверно сформированные события и события, источник которых не может быть авторизован, удаляются (закрываются при сбое). Временная перестройка приложения Telegram перерегистрирует наблюдателя вместе с основными обработчиками.

**Контракты полезной нагрузки на каждое событие (версия 1, добавка):**

| `event_type` | Платформы | Поля полезной нагрузки |
|--------------|-----------|----------------|
| `reaction` | телеграмма | `emojis: list[str]`, `custom_emoji_ids: list[str]`, `chat_id: str`, `message_id: str`, `thread_id: str \| None` (обновления реакций Telegram не имеют идентификатора темы, поэтому в настоящее время всегда `None`). |
| `message_edited` | телеграмма, раздор | `chat_id: str`, `message_id: str`, `thread_id: str \| None`, `text: str \| None` (отредактированный текст или заголовок, ограниченный; `None` для редактирования только мультимедиа или без кэширования), `edited_at: str \| None` (ISO 8601). |
| `message_deleted` | раздор | `chat_id: str`, `message_id: str`, `thread_id: str \| None`, `author_id: str \| None`. Событие удаления Discord не идентифицирует удаляющего; авторизованным источником является автор удаленного сообщения, а некэшированные удаления никогда не срабатывают. |
| `thread_created` | раздор | `thread_id: str`, `parent_chat_id: str \| None`, `name: str \| None`, `owner_id: str \| None`. |
| `thread_renamed` | раздор | `thread_id: str`, `parent_chat_id: str \| None`, `old_name: str \| None`, `new_name: str`. Запускается только тогда, когда имя действительно изменилось; другие обновления потоков (архив, медленный режим, теги) удаляются. Событие обновления потока Discord не содержит актера, поэтому авторизованным источником является владелец потока. |

Собственные прогрессивные изменения сообщений бота (потоковое вещание) никогда не запускают `message_edited` в Discord — события, созданные ботом, сбрасываются на место пожара.

Этот хук предназначен только для наблюдателя: он **не** добавляет доступ к необработанным событиям или доступ к адаптеру. **Доступ к полезной нагрузке необработанного SDK намеренно не поставляется** — объекты адаптера SDK меняют форму без предварительного уведомления и становятся неразвиваемой поверхностью API; там, где это действительно необходимо, требуются собственные явные возможности (`gateway.raw_events`) с меткой «нет гарантии стабильности» и собственный дизайн (отслеживается под номером 64228). Для *действия* на платформе (добавление реакции, переименование потока) используйте управляемый возможностями фасад `ctx.platform_actions`, описанный в [руководстве по плагинам](plugins.md#platform-actions) — по умолчанию он отключен за возможностью `gateway.platform_actions`. `PluginContext.dispatch_tool()` может вызывать только инструменты, зарегистрированные в реестре инструментов; `send_message` намеренно не зарегистрирован там (его транспорт зарезервирован для явных путей доставки CLI, cron, kanban и MCP). Будущий контракт на исходящую доставку должен сначала обеспечить стабильную доставку контента/дескрипторов для всех адаптеров; этот фрагмент не регистрирует предварительно инертный крючок `gateway_message_delivered`.

---

### `pre_approval_request`

Срабатывает до запроса решения об утверждении. Он охватывает подсказки — интерактивный интерфейс командной строки, Ink TUI, платформы шлюзов и клиенты ACP, а также `approvals.mode=smart` решения, принимаемые без подсказок человека (`surface="smart"`). В интеллектуальном режиме перехватчик запускается до вызова вспомогательного LLM.

Это подходящее место для подключения настраиваемого уведомления — например, приложения в строке меню macOS, которое выводит уведомление о разрешении/запрете, или журнала аудита, в котором записывается каждый запрос на утверждение с контекстом.

**Подпись обратного вызова:**

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

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `command` | `str` | Оценивается команда терминала или сценарий `execute_code`. Полезная нагрузка Smart и шлюза редактируется перед отправкой наблюдателя. Редактирование интеллектуального наблюдателя является обязательным, даже если `security.redact_secrets` отключен; если редактирование не удалось, умные перехватчики пропускаются. |
| `description` | `str` | Понятно понятные причины, по которым команда помечена (объединяется при совпадении нескольких шаблонов) |
| `pattern_key` | `str` | Первичный ключ шаблона, вызвавший утверждение (например, `"rm_rf"`, `"sudo"`) |
| `pattern_keys` | `list[str]` | Все совпавшие графические ключи |
| `session_key` | `str` | Идентификатор сеанса, полезный для определения области уведомлений для каждого чата |
| `surface` | `str` | `"cli"` для интерактивных подсказок CLI/TUI, `"gateway"` для асинхронных утверждений платформы или `"smart"` для автоматического принятия/отклонения решений вспомогательного LLM |

**Возвращаемое значение:** игнорируется. Крючки здесь доступны только наблюдателю; они не могут наложить вето или предварительно ответить на утверждение. Используйте [`pre_tool_call`](#pre_tool_call), чтобы заблокировать инструмент до того, как он достигнет системы утверждения.

**Случаи использования**: уведомления на рабочем столе, push-уведомления, ведение журнала аудита, веб-перехватчики Slack, маршрутизация эскалации, метрики.

**Пример — уведомление на рабочем столе в macOS:**

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

### `post_approval_response`

Срабатывает после принятия подсказки или интеллектуального решения об утверждении, по истечении времени запроса или когда шлюз не может доставить уведомление об утверждении. При ошибке уведомления выдается `choice="notify_failed"` до того, как будет принято какое-либо решение об утверждении.

**Подпись обратного вызова:**

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

Те же кварги, что и `pre_approval_request`, плюс:

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `choice` | `str` | В предлагаемых поверхностях используются `"once"`, `"session"`, `"always"`, `"deny"`, `"timeout"` или `"notify_failed"`; для принятия разумных решений используйте `"smart_approve"` или `"smart_deny"` |
| `decided_by` | `str` | `"aux_llm"` за умные решения; отсутствует на подсказках |

**Возвращаемое значение:** игнорируется.

**Случаи использования**: закройте соответствующее уведомление на рабочем столе, запишите окончательное решение в журнал аудита, обновите показатели, настройте ограничитель скорости.

```python
def log_decision(command, choice, session_key, **kwargs):
    logger.info("approval %s: %s for session %s", choice, command[:60], session_key)

def register(ctx):
    ctx.register_hook("post_approval_response", log_decision)
```

---

### `pre_transcription`

Срабатывает внутри диспетчера STT (`tools.transcription_tools.transcribe_audio`) **после** разрешения поставщика и **перед** вызовом любого бэкэнда, независимо от того, является ли этот бэкенд встроенным, `type: command` провайдером или провайдером, зарегистрированным в плагине. Позволяет плагину самому управлять запросом на транскрипцию, а не только потом наблюдать за транскрипцией.

**Подпись обратного вызова:**

```python
def my_callback(
    file_path: str,
    provider: str,
    model: str | None,
    language: str | None,
    prompt: str | None,
    source: str | None,
    **kwargs,
) -> dict | None:
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `file_path` | `str` | Абсолютный путь к аудиофайлу, который будет транскрибирован. Только для чтения. |
| `provider` | `str` | Разрешенный поставщик STT (`local`, `groq`, `openai`, `mistral`, `xai`, `elevenlabs`, `deepinfra`, `local_command`, имя поставщика команд или имя поставщика подключаемого модуля). |
| `model` | `str \| None` | Модель решена на данный момент или `None`, если применяется серверная часть по умолчанию. |
| `language` | `str \| None` | Язык из раздела конфигурации провайдера или `None`. |
| `prompt` | `str \| None` | Статическое значение [`stt.prompt`](/user-guide/configuration#transcription-prompt-vocabulary-hints) или `None`. |
| `source` | `str \| None` | Метка поверхности вызывающего абонента (`gateway`, `voice_mode`, …). Только наблюдаемость, не используется для отправки. |

**Возвращаемое значение:** `dict` с любым из `"prompt"`, `"language"`, `"model"`, сопоставленным со строками, или `None`, чтобы оставить запрос без изменений. Нестроковые значения, неизвестные ключи и `file_path` игнорируются (попытки `file_path` регистрируются как предупреждение). Результаты применяются в **порядке регистрации, число побед последнего автора по каждому полю**, поверх значения конфигурации `stt.prompt`. Возврат `""` для `prompt` очищает настроенное приглашение для этого запроса.

**Случаи использования**. Внедряйте список словарных слов для каждого пользователя или для каждого чата перед загрузкой аудио, принудительно `language` из языкового стандарта вызывающего абонента, понижение версии `model` для длинных записей, перенаправление источников шума на другую модель.

```python
VOCAB = "Hermes, Teknium, Nous Research, kanban"

def add_vocab(provider, prompt, source, **kwargs):
    if source != "gateway":
        return None
    return {"prompt": f"{prompt}. {VOCAB}" if prompt else VOCAB}

def register(ctx):
    ctx.register_hook("pre_transcription", add_vocab)
```

Не каждый бэкэнд принимает приглашение. `local` сопоставляет его с `initial_prompt` быстрого шепота; `openai`, `groq`, `mistral` и `deepinfra` отправляют его как `prompt`; Поставщики `xai`, `elevenlabs`, `local_command` и `type: command` регистрируются в DEBUG и расшифровываются без него. Полную матрицу и границы конфиденциальности см. в [таблице поддержки поставщика](/user-guide/configuration#transcription-prompt-vocabulary-hints). Ошибки перехвата являются открытыми при сбое: отправка продолжается с неизмененным запросом.

---

### `transform_tool_result`

Срабатывает **после** возврата инструмента и **до** добавления результата к диалогу. Позволяет плагину перезаписать строку результата ЛЮБОГО инструмента, а не только вывод терминала, прежде чем модель увидит ее.

**Подпись обратного вызова:**

```python
def my_callback(tool_name: str, args: dict, result: str, task_id: str, **kwargs) -> str | None:
```

Полная полезная нагрузка также включает `session_id`, `tool_call_id`, `turn_id`, `api_request_id`, `duration_ms`, `status`, `error_type` и `error_message`. `result` — конечный результат, возвращаемый отправкой инструмента; он и `args` могут содержать произвольный контент и секреты пользователя/инструмента.

**Возвращаемое значение:** Первый `str` заменяет результат (включая пустую строку); `None` оставляет его без изменений.

**Случаи использования**. Редактируйте персональные данные организации из выходных данных `web_extract`, переносите длинные ответы инструмента JSON в сводный заголовок, добавляйте подсказки, дополненные поиском, в результаты `read_file`, переписывайте отчеты субагента `delegate_task` в схему, специфичную для проекта.

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

Применяется к любому инструменту. О переписывании только для терминала см. `transform_terminal_output` ниже — он уже, работает до `transform_tool_result`, и его замена по-прежнему зависит от конечного ограничения вывода инструмента терминала.

---

### `transform_terminal_output`

Срабатывает внутри инструмента `terminal` после того, как захват процесса переднего плана уже ограничен средой, и до достижения конечного предела вывода. Он позволяет плагинам заменять захваченный stdout/stderr; замена по-прежнему зависит от конечного предела производительности.

**Подпись обратного вызова:**

```python
def my_callback(
    command: str,
    output: str,
    returncode: int,
    task_id: str,
    env_type: str,
    **kwargs,
) -> str | None:
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `command` | `str` | Команда оболочки, выдавшая выходные данные. |
| `output` | `str` | Комбинированный стандартный вывод/стандартный вывод после захвата ограниченного процесса. |
| `returncode` | `int` | Код возврата процесса. |
| `task_id` | `str` | Эффективный идентификатор задачи или пустая строка. |
| `env_type` | `str` | Тип среды выполнения. |

**Возвращаемое значение:** Сначала `str` заменяет выходные данные; `None` оставляет его без изменений. Команда и выходные данные могут содержать учетные данные или другие конфиденциальные данные.

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

Сопряжено с `transform_tool_result`, который впоследствии запускается для каждого инструмента, включая `terminal`.

---

### `transform_llm_output`

Срабатывает **один раз за ход** после завершения цикла вызова инструмента и получения окончательного ответа моделью, **до** того, как этот ответ будет доставлен пользователю (CLI, шлюзу или программному вызывающему объекту). Позволяет плагину перезаписать окончательный текст помощника, используя методы классического программирования — без дополнительных токенов вывода, сжигаемых в тексте SOUL или преобразований, управляемых навыками.

**Подпись обратного вызова:**

```python
def my_callback(
    response_text: str,
    session_id: str,
    model: str,
    platform: str,
    **kwargs,
) -> str | None:
```

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `response_text` | `str` | Окончательный текст ответа помощника на этот ход. |
| `session_id` | `str` | Идентификатор сеанса для этого диалога (может быть пустым для однократных запусков). |
| `model` | `str` | Название модели, вызвавшей ответ (например, `anthropic/claude-sonnet-4.6`). |
| `platform` | `str` | Платформа доставки (`cli`, `telegram`, `discord`, …; пустая, если не установлена). |

**Возвращаемое значение:** Непустое `str` для замены текста ответа, `None` или пустая строка, чтобы оставить его без изменений. **Первая непустая строка выигрывает** при регистрации нескольких плагинов. В отличие от преобразований инструмента и терминала, пустая строка не принимается в качестве замены.

**Случаи использования**. Применение преобразования личности/словаря (пиратский язык, Губка Боб), удаление пользовательских идентификаторов из окончательного текста, добавление нижнего колонтитула для подписи для конкретного проекта, применение руководства по стилю компании без сжигания токенов по инструкциям SOUL.

Если потоковая передача CLI включена, преобразование, предназначенное только для добавления, печатается после
обтекаемое тело. Преобразование, заменяющее ответ, печатается полностью после
тело потоковой передачи, помеченное как преобразование после потока, поэтому замена
контент никогда не теряется незаметно.

```python
import os, re

def spongebob(response_text, **kwargs):
    if os.environ.get("SPONGEBOB_MODE") != "on":
        return None  # pass through unchanged
    return re.sub(r"!", "!! Tartar sauce!", response_text)

def register(ctx):
    ctx.register_hook("transform_llm_output", spongebob)
```

Крючок защищен непустым, непрерывным ответом — он не сработает при прерывании кнопки «Стоп» или пустых ходах. Исключения регистрируются как предупреждения и не прерывают работу агента.

### Перехватчики наблюдения за API-запросами

#### `pre_api_request`

Срабатывает для каждой попытки провайдера непосредственно перед ее отправкой. Это только для наблюдателя. Устаревшие поля `user_message`, `conversation_history` и `request_messages` являются необработанными и намеренно не очищены для совместимости; новым потребителям следует предпочесть продезинфицированный конверт `request`.

#### `post_api_request`

Срабатывает после успешной нормализации ответа поставщика. Это только для наблюдателя. Предпочитайте очищенный `response`; `assistant_message` — это необработанное нормализованное сообщение, а `usage` содержит учетные данные.

#### `api_request_error`

Срабатывает при неудачной попытке поставщика с указанием времени статуса/повторной попытки, объекта `error` и очищенного `request`. Это только для наблюдателя. Сообщения об ошибках могут по-прежнему содержать данные поставщика или пользователя.

### `on_skill_lifecycle`

Срабатывает после авторитетного изменения состояния использования навыка. Он предназначен только для наблюдателей и предоставляет локальный `skill_name`, происхождение, идентификаторы корреляции, счетчик использования и флаги повторного использования.

### Наблюдатели жизненного цикла Канбана

#### `kanban_task_claimed`

Срабатывает после фиксации заявки в процессе диспетчера, непосредственно перед появлением работника.

#### `kanban_task_completed`

Пожары после завершения и очистки, обычно в рабочем процессе. Его `summary` может содержать контент проекта или пользователя.

#### `kanban_task_blocked`

Срабатывает после обычного заблокированного перехода. Путь ожидания зависимости вызывает его до завершения транзакции записи. Его `reason` может содержать контент проекта или пользователя.

Все три перехватчика канбана доступны только наблюдателям и содержат `task_id`, `profile_name`, `board`, `assignee` и `run_id`; завершено добавление `summary` и заблокировано добавление `reason`.

### Канбан — жизненный цикл рабочих, изменение задач и диспетчеризация наблюдателей

Пять дополнительных наблюдателей (RFC № 58548) расширяют семейство канбан. Все они доступны только для наблюдателей, срабатывают после фиксации соответствующей транзакции и замыкаются на `has_hook` — без подписчика поведение отправки не меняется. Хуки области задач содержат те же общие поля, что и перехватчики выше.

- **`on_kanban_worker_spawned`** — после возврата `spawn_fn` и сохранения рабочего PID. Добавляет `worker_pid` (может быть `None`) и `workspace_path`. Работает внутри диспетчерского шлюза; делайте обратные вызовы быстрыми.
- **`on_kanban_worker_exited`** — определяется по такту, когда `detect_crashed_workers` восстанавливает задачу с неработающим PID. Добавляет `worker_pid`, `exit_kind`, `exit_code`, `outcome`, `retry_status`.
- **`on_kanban_worker_stale_claim`** — при отзыве заявки с истекшим сроком жизни; Расширения live-PID не запускаются. Добавляет `worker_pid`, `heartbeat_stale`, `retry_status`.
- **`on_kanban_task_updated`** — после зафиксированной записи поля задачи вне жизненного цикла утверждения/завершения/блока (`assign_task`, переопределения модели/обоснования, редакторы информационной панели). Добавляет `changed_fields` — только имена полей, но не значения.
- **`on_kanban_dispatch_tick`** — один раз за тик диспетчера, строго после снятия блокировки диспетчеризации, включая тики простоя и блокировки. Полезная нагрузка: `board`, `profile_name`, `dry_run`, `outcome`, `result`.

---

## Крючки-ракушки

Объявите перехватчики сценариев оболочки в вашем `~/.hermes/config.yaml`, и Hermes будет запускать их как подпроцессы всякий раз, когда сработает соответствующее событие перехватчика плагина — как в сеансах CLI, так и в сеансах шлюза. Разработка плагинов Python не требуется.

Используйте перехватчики оболочки, если вы хотите, чтобы вставной однофайловый скрипт (Bash, Python, что-нибудь с шебангом) выполнял:

- **Блокировать или изменить вызов инструмента** — отклонять опасные команды `terminal`, применять политики для каждого каталога, требовать одобрения для деструктивных операций `write_file` / `patch` или перезаписывать аргументы (очистка путей, внедрение значений по умолчанию) перед запуском инструмента.
- **Запускать после вызова инструмента** — автоматическое форматирование файлов Python или TypeScript, которые только что написал агент, запись вызовов API, запуск рабочего процесса CI.
- **Внедрить контекст в следующий ход LLM** — добавьте к сообщению пользователя выходные данные `git status`, текущий день недели или полученные документы (см. [`pre_llm_call`](#pre_llm_call)).
- **Наблюдение за событиями жизненного цикла** — запись строки журнала при завершении субагента (`subagent_stop`) или запуске сеанса (`on_session_start`).

Перехватчики оболочки регистрируются путем вызова `agent.shell_hooks.register_from_config(cfg)` как при запуске CLI (`hermes_cli/main.py`), так и при запуске шлюза (`gateway/run.py`). Они естественным образом компонуются с помощью плагинов Python — оба проходят через один и тот же диспетчер.

### Краткое сравнение

| Размерность | Крючки-ракушки | [Перехватчики плагинов](#перехватчики-плагины) | [Перехватчики шлюза](#gateway-event-hooks) |
|-----------|-------------|-----------------------------------------------|------------------------|
| Объявлено в | `hooks:` блок в `~/.hermes/config.yaml` | `register()` в плагине `plugin.yaml` | каталог `HOOK.yaml` + `handler.py` |
| Живет под | `~/.hermes/agent-hooks/` (по соглашению) | `~/.hermes/plugins/<name>/` | `~/.hermes/hooks/<name>/` |
| Язык | Любой (Bash, Python, двоичный код Go,…) | Только Python | Только Python |
| Вбегает | CLI + шлюз | CLI + шлюз | Только шлюз |
| События | `VALID_HOOKS` (включая `subagent_stop`) | `VALID_HOOKS` | Жизненный цикл шлюза (`gateway:startup`, `agent:*`, `command:*`) |
| Можно заблокировать вызов инструмента | Да (`pre_tool_call`) | Да (`pre_tool_call`) | Нет |
| Может внедрить контекст LLM | Да (`pre_llm_call`) | Да (`pre_llm_call`) | Нет |
| Согласие | Подсказка при первом использовании для каждой пары `(event, command)` | Неявное (доверие к плагину Python) | Неявное (директор доверия) |
| Межпроцессная изоляция | Да (подпроцесс) | Нет (в процессе) | Нет (в процессе) |

### Схема конфигурации

```yaml
hooks:
  <event_name>:                  # Must be in VALID_HOOKS
    - matcher: "<regex>"         # Optional; used for pre/post_tool_call only
      command: "<shell command>" # Required; runs via shlex.split, shell=False
      timeout: <seconds>         # Optional; default 60, capped at 300
      fail_closed: <bool>        # Optional; default false. pre_tool_call only.
                                 # `failClosed` also accepted (Cursor/Claude Code compat)

hooks_auto_accept: false         # See "Consent model" below
```

Имена событий должны быть одним из [событий перехвата плагина](#plugin-hooks); опечатки приводят к ответу «Вы имели в виду X?» предупреждение и пропускаются. Неизвестные ключи внутри одной записи игнорируются; отсутствие `command` — это пропуск с предупреждением. `timeout > 300` зажат с предупреждением. `fail_closed: true` о событии, отличном от `pre_tool_call`, выдает предупреждение и игнорируется (только события с возможностью блокировки могут не закрыться).

### Протокол передачи данных JSON

Каждый раз при возникновении события Hermes запускает подпроцесс для каждого совпадающего перехватчика (если позволяет сопоставитель), передает полезную нагрузку JSON на **stdin** и считывает **stdout** обратно как JSON.

**stdin — полезная нагрузка, которую получает скрипт:**

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

`tool_name` и `tool_input` — это `null` для событий, не связанных с инструментом (`pre_llm_call`, `subagent_stop`, жизненный цикл сеанса). Дикт `extra` содержит все кварги, специфичные для события (`user_message`, `conversation_history`, `child_role`, `duration_ms`, …). Несериализуемые значения преобразуются в строки, а не опускаются.

**stdout — необязательный ответ:**

```jsonc
// Block a pre_tool_call (both shapes accepted; normalised internally):
{"decision": "block", "reason":  "Forbidden: rm -rf"}   // Claude-Code style
{"action":   "block", "message": "Forbidden: rm -rf"}   // Hermes-canonical

// Modify a pre_tool_call — rewrite tool args before dispatch:
{"action": "modify", "args": {"new_string": "fixed content"}}         // Hermes-canonical
{"decision": "modify", "tool_input": {"new_string": "fixed content"}} // Claude-Code style

// Inject context for pre_llm_call:
{"context": "Today is Friday, 2026-04-17"}

// Keep the agent going at the verify gate (pre_verify); both shapes accepted:
{"action": "continue", "message": "Run the formatter, then finish."}
{"decision": "block",  "reason":  "Run the formatter, then finish."}

// Silent no-op — any empty / non-matching output is fine:
```

Неверный формат JSON, ненулевые коды выхода и тайм-ауты регистрируют предупреждение, но никогда не прерывают цикл агента.

### Код выхода 2 = блокировка (совместимость с кодом Клода/курсором)

Хук `pre_tool_call`, который завершается с кодом **2**, блокирует вызов инструмента, даже если его стандартный вывод не содержит блочного JSON. Сообщение о блокировке разрешается в порядке приоритета:

1. блок стандартного вывода JSON (`reason` / `message`), если он присутствует;
2. первые 400 символов stderr;
3. общее значение по умолчанию `"Blocked by shell hook."`.

Итак, самый простой блокирующий крючок:

```bash
#!/usr/bin/env bash
echo "policy violation: rm -rf is not permitted" >&2
exit 2
```

Для событий, директива блока которых не соблюдается (все, кроме `pre_tool_call`), выход 2 обрабатывается как любой другой ненулевой выход: записывается предупреждение, а стандартный вывод все равно анализируется.

### Открытие при отказе и закрытие при отказе

По умолчанию перехватчики оболочки **не открываются**: ошибка появления, тайм-аут или неразбираемый стандартный вывод регистрируют предупреждение, и действие продолжается. Это правильное значение по умолчанию для перехватчиков наблюдения, но неправильное для ворот безопасности. Сбойный секретный сканер не должен молча разрешать вызов инструмента, который он должен был проверить.

Установите `fail_closed: true` (или `failClosed: true`, написание кода курсора/Клода) для записи `pre_tool_call`, чтобы инвертировать это:

```yaml
hooks:
  pre_tool_call:
    - matcher: "terminal|write_file|patch"
      command: "~/.hermes/agent-hooks/secret-scan.sh"
      timeout: 10
      fail_closed: true
```

С `fail_closed: true` каждый из них теперь **блокирует** вызов инструмента с помощью `hook <command> failed closed: <reason>`:

| Неудача | Открытие при отказе (по умолчанию) | `fail_closed: true` |
|---------|--------------------|--------------------|
| Команда не найдена/не исполняется | предупредить, продолжить | **блокировать** |
| Тайм-аут | предупредить, продолжить | **блокировать** |
| Стандартный вывод, отличный от JSON (например, трассировка стека) | предупредить, продолжить | **блокировать** |
| Чистый выход, действительный неактивный JSON (`{}`) | продолжать | продолжать |

`fail_closed` применяется только к событиям с возможностью блокировки (`pre_tool_call` сегодня); установка его на любое другое событие регистрирует предупреждение во время анализа конфигурации и игнорируется. `hermes hooks test` отражает эту семантику — строка `parsed` показывает именно ту форму блока, которую получит диспетчер.

### Рабочие примеры

#### 1. Автоматическое форматирование файлов Python после каждой записи.

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

Контекстное представление файла агентом **не** перечитывается автоматически — переформатирование влияет только на файл на диске. Последующие вызовы `read_file` выбирают отформатированную версию.

#### 2. Блокировать деструктивные команды `terminal`

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

#### 3. Вводить `git status` в каждый ход (эквивалент Claude-Code `UserPromptSubmit`)

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

Событие `UserPromptSubmit` в Claude Code намеренно не является отдельным событием Hermes — `pre_llm_call` срабатывает в том же месте и уже поддерживает внедрение контекста. Используйте его здесь.

#### 4. Регистрируйте каждое завершение субагента.

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

### Модель согласия

Каждая уникальная пара `(event, command)` запрашивает у пользователя подтверждение при первом ее просмотре Hermes, а затем сохраняет решение на `~/.hermes/shell-hooks-allowlist.json`. Последующие запуски (CLI или шлюз) пропускают запрос.

Три аварийных люка обходят интерактивную подсказку — достаточно любого:

1. Флаг `--accept-hooks` в CLI (например, `hermes --accept-hooks chat`).
2. Переменная среды `HERMES_ACCEPT_HOOKS=1`.
3. `hooks_auto_accept: true` в `~/.hermes/config.yaml`

Для запусков без TTY (шлюз, cron, CI) требуется один из этих трех — в противном случае любой вновь добавленный хук останется незарегистрированным и зарегистрирует предупреждение.

**Редактированию скриптов доверяют без уведомления.** Ключи белого списка указаны в точной командной строке, а не в хэше скрипта, поэтому редактирование скрипта на диске не делает согласие недействительным. `hermes hooks doctor` отмечает отклонение времени, чтобы вы могли заметить изменения и решить, следует ли их повторно утверждать.

#### Добавление в белый список вручную

Добавление в белый список вручную полезно для развертываний без TTY или сервисных учетных записей, когда оператор не может интерактивно ответить на приглашение при первом использовании. Файл белого списка — `~/.hermes/shell-hooks-allowlist.json`, ожидаемый формат — массив `approvals`. При каждом утверждении записывается перехватчик `event` и точная строка `command`:

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

Командная строка должна точно соответствовать настроенной команде перехватчика. Объект с ключом пути и полем `sha256` не является ожидаемым форматом и не одобрит перехватчик. Проверьте введенные вручную данные с помощью `hermes hooks list`.

### Интерфейс командной строки `hermes hooks`

| Команда | Что он делает |
|---------|--------------|
| `hermes hooks list` | Дамп настроенных перехватчиков со статусом сопоставления, тайм-аутом и согласием |
| `hermes hooks test <event> [--for-tool X] [--payload-file F]` | Запустите каждый соответствующий крючок против синтетической полезной нагрузки и распечатайте проанализированный ответ |
| `hermes hooks revoke <command>` | Удалить все записи белого списка, соответствующие `<command>` (вступит в силу при следующем перезапуске) |
| `hermes hooks doctor` | Для каждого настроенного перехватчика: проверьте бит выполнения, состояние списка разрешений, отклонение времени mtime, достоверность вывода JSON и приблизительное время выполнения |

### Безопасность

Перехватчики оболочки запускаются с использованием **ваших полных учетных данных пользователя** — та же граница доверия, что и запись cron или псевдоним оболочки. Считайте блок `hooks:` в `config.yaml` привилегированной конфигурацией:

- Только справочные сценарии, которые вы написали или полностью просмотрели.
– Храните сценарии внутри `~/.hermes/agent-hooks/`, чтобы путь можно было легко проверить.
– Повторно запустите `hermes hooks doctor` после извлечения общей конфигурации, чтобы обнаружить вновь добавленные перехватчики до их регистрации.
- Если ваш файл config.yaml контролируется всей командой, просмотрите PR, которые изменяют раздел `hooks:`, так же, как вы просматриваете конфигурацию CI.

### Порядок и приоритет

Как хуки плагинов Python, так и хуки оболочки проходят через один и тот же диспетчер `invoke_hook()`. Плагины Python регистрируются первыми (`discover_and_load()`), вторыми — перехватчики оболочки (`register_from_config()`), поэтому решения о блоках Python `pre_tool_call` имеют приоритет в случае равенства. Выигрывает первый действительный блок — агрегатор возвращается, как только какой-либо обратный вызов создает `{"action": "block", "message": str}` с непустым сообщением.

## Исходящие веб-перехватчики

Исходящие веб-перехватчики — это зеркало [платформы входящих веб-перехватчиков](/user-guide/messaging/webhooks): входящие веб-перехватчики будят Гермеса, когда мир меняется; исходящие веб-перехватчики сообщают миру, когда Hermes что-то делает. Настройте список конечных точек HTTP и событий жизненного цикла, о которых они заботятся, и Hermes отправляет POST подписанные полезные данные JSON на каждую конечную точку всякий раз, когда срабатывает соответствующее событие — без опроса на принимающей стороне.

Типичное использование:

– Уведомлять систему CI или панель мониторинга об окончании очереди агента (`on_session_end`).
- Отслеживать завершение работы субагентов по всему автопарку (`subagent_stop`)
- Передача активности инструмента во внешний мониторинг (`post_tool_call` с `matcher`)
- Разбудите *другой* экземпляр Hermes: укажите URL-адрес входящего веб-перехватчика этого экземпляра.

### Конфигурация

Добавьте список `hooks.outbound:` в `~/.hermes/config.yaml`:

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

Допустимо любое событие из набора плагинов-перехватчиков (`pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_start`, `on_session_end`, `subagent_start`, `subagent_stop`, ...). Неправильно сформированные записи предупреждаются и пропускаются — сломанный вебхук никогда не приводит к сбою агента. Изменения вступят в силу при следующем перезапуске сеанса CLI/шлюза.

Секреты: предпочитайте `secret_env` (имя переменной среды, обычно установленное в `~/.hermes/.env`) вместо встроенного литерала `secret:`, чтобы файл конфигурации не содержал учетных данных. Записи без секрета доставляются без подписи (помечены `hermes hooks list` как `UNSIGNED`).

### Формат провода

При каждом запуске POST отправляется тело JSON той же формы верхнего уровня, что и стандартный ввод перехватчиков оболочки, а также метаданные доставки:

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

Заголовки:

| Заголовок | Значение |
|--------|-------|
| `Content-Type` | `application/json` |
| `X-Hermes-Event` | Имя события перехвата |
| `X-Hermes-Delivery` | Уникальный идентификатор каждой доставки — то же значение, что и `delivery_id` в теле |
| `X-Hermes-Signature-256` | `sha256=<hex>` — HMAC-SHA256 исходного тела, в стиле GitHub; присутствует только тогда, когда настроен секрет |

Проверьте подпись точно так же, как если бы вы использовали вебхук GitHub:

```python
import hashlib, hmac

def verify(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

Поскольку `delivery_id` и `timestamp` находятся **внутри подписанного тела**, проверенный получатель также бесплатно получает защиту от повторного воспроизведения:

- **Dedupe** на `delivery_id` (или соответствующий заголовок `X-Hermes-Delivery`) — запоминайте недавно просмотренные идентификаторы и пропускайте дубликаты. Hermes повторяет неудачную доставку один раз, поэтому один и тот же идентификатор может законно прийти дважды.
- **Отклоняйте устаревшие события**, сверяя `timestamp` со своими часами с окном допуска (обычно по умолчанию — 5 минут). Злоумышленник, воспроизводящий перехваченный запрос, не сможет подделать новую временную метку без секрета.

### Семантика доставки

- **Принцип «выстрелил и забыл», вне горячих точек.** События мгновенно сериализуются и ставятся в очередь; один фоновый поток выполняет HTTP POST. Медленная или мертвая конечная точка никогда не сможет остановить вызов инструмента или поворот агента.
- **Только для уведомлений.** В отличие от перехватчиков оболочки, исходящие веб-перехватчики не могут блокировать вызовы инструментов или вводить контекст — тело ответа игнорируется. Они наблюдают, но никогда не управляют.
- **Ограниченные повторы.** Ошибки соединения и ответы 5xx повторяются один раз с отсрочкой; Ответы 4xx не повторяются (получатель сказал, что сам запрос неправильный). Неисправности протоколируются и удаляются — доставка осуществляется по принципу «максимально возможно», а не гарантированно.
- **Перенаправления никогда не выполняются.** Ответ 3xx рассматривается как неправильная конфигурация и регистрируется — после перенаправления POST подписанные полезные данные будут автоматически удалены. Наведите `url` на конечную конечную точку.
- **Ограниченная очередь.** Если очередь создает резервную копию (мертвая конечная точка, шторм событий), новые события удаляются с предупреждением, а не занимают неограниченную память.
- **Запрос согласия не требуется.** Исходящие цели не выполняют код на вашем компьютере — они получают данные по настроенному вами URL-адресу. `HERMES_SAFE_MODE=1` по-прежнему пропускает регистрацию, как и плагины и хуки оболочки. Обратите внимание, что полезные данные включают в себя входные данные инструментов и метаданные событий, поэтому указывайте цели только на конечные точки, которым вы доверяете, и предпочитайте `https://`.

`hermes hooks list` показывает настроенные исходящие цели вместе с перехватчиками оболочки, включая подписание каждой цели.