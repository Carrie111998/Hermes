---
sidebar_position: 9
---

# Добавление адаптера платформы

В этом руководстве рассматривается добавление новой платформы обмена сообщениями к шлюзу Hermes. Адаптер платформы подключает Hermes к внешней службе обмена сообщениями (Telegram, Discord, WeCom и т. д.), чтобы пользователи могли взаимодействовать с агентом через эту службу.

:::совет
Есть два способа добавить платформу:
- **Плагин** (рекомендуется для сообщества/сторонних разработчиков): перетащите каталог плагина в `~/.hermes/plugins/` — никаких изменений основного кода не требуется. См. [Путь к плагину](#plugin-path-recommended) ниже.
- **Встроенный**: изменяйте более 20 файлов в коде, конфигурации и документации. Используйте [Встроенный контрольный список](#step-by-step-checklist-built-in-path) ниже.
:::

## Обзор архитектуры

```
User ↔ Messaging Platform ↔ Platform Adapter ↔ Gateway Runner ↔ AIAgent
```

Каждый адаптер расширяет `BasePlatformAdapter` из `gateway/platforms/base.py` и реализует:

- **`connect()`** — Установить соединение (WebSocket, длинный опрос, HTTP-сервер и т. д.) *(аннотация)*
- **`disconnect()`** — Чистое завершение работы *(аннотация)*
- **`send()`** — Отправить текстовое сообщение в чат *(аннотация)*
- **`send_typing()`** — Показывать индикатор ввода (необязательное переопределение)
- **`get_chat_info()`** — вернуть метаданные чата (необязательное переопределение).

Входящие сообщения принимаются адаптером и пересылаются через `self.handle_message(event)`, который базовый класс направляет бегуну шлюза.

## Путь к плагину (рекомендуется)

Система плагинов позволяет добавлять адаптер платформы без изменения какого-либо основного кода Hermes. Ваш плагин представляет собой каталог с двумя файлами:

```
~/.hermes/plugins/my-platform/
  plugin.yaml      # Plugin metadata
  adapter.py       # Adapter class + register() entry point
```

### плагин.yaml

Метаданные плагина. Блоки `requires_env` и `optional_env` автоматически заполняют записи пользовательского интерфейса `hermes config` (см. [Surfacing Env Vars](#surfacing-env-vars-in-hermes-config) ниже).

```yaml
name: my-platform
label: My Platform
kind: platform
version: 1.0.0
description: My custom messaging platform adapter
author: Your Name
requires_env:
  - MY_PLATFORM_TOKEN          # bare string works
  - name: MY_PLATFORM_CHANNEL  # or rich dict for better UX
    description: "Channel to join"
    prompt: "Channel"
    password: false
optional_env:
  - name: MY_PLATFORM_HOME_CHANNEL
    description: "Default channel for cron delivery"
    password: false
```

#### Инструменты для исходящего клиента: `provides_tools`

Плагины `kind: platform` **отложены**: модуль адаптера (и его SDK
импорт) загружаются только тогда, когда шлюз, cron или путь `send_message` сначала запрашивают
реестр платформы для платформы. Если ваш плагин также поставляется с исходящим *client
инструменты* агент должен иметь возможность звонить из любого сеанса (входящий в комплект `a2a`
плагина `a2a_call` / `a2a_discover` и т. д.), поместите их в выделенный `tools.py`
с функцией `register_tools(ctx)` и объявите их в манифесте:

```yaml
provides_tools:
  - my_platform_call
  - my_platform_list
```

При объявлении `provides_tools` Hermes импортирует только `tools.py` во время плагина.
обнаружение и регистрация клиентских инструментов в каждом процессе — CLI и TUI
включено — при этом адаптер остается отложенным. Сохраните пакет `__init__.py`
import-light и вытащите адаптер изнутри `register()`, чтобы нетерпеливый
импорт остается дешевым. Без поля ничего не меняется: весь плагин остаётся
отложено.

Пользователи включают набор инструментов для каждой платформы, как и любую другую, например.
`hermes tools enable my_platform --platform cli` или перечислив набор инструментов
ключ под `platform_toolsets` в `config.yaml`. Названия платформ плагинов
также допустимы цели `--platform`, поэтому входящий сеанс на вашей платформе может
получить собственные исходящие инструменты.

### адаптер.py

```python
import os
from gateway.platforms.base import (
    BasePlatformAdapter, SendResult, MessageEvent, MessageType,
)
from gateway.config import Platform, PlatformConfig


class MyPlatformAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("my_platform"))
        extra = config.extra or {}
        self.token = os.getenv("MY_PLATFORM_TOKEN") or extra.get("token", "")

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Connect to the platform API, start listeners
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # Send message via platform API
        return SendResult(success=True, message_id="...")

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "dm"}


def check_requirements() -> bool:
    return bool(os.getenv("MY_PLATFORM_TOKEN"))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("MY_PLATFORM_TOKEN") or extra.get("token"))


def _env_enablement() -> dict | None:
    token = os.getenv("MY_PLATFORM_TOKEN", "").strip()
    channel = os.getenv("MY_PLATFORM_CHANNEL", "").strip()
    if not (token and channel):
        return None
    seed = {"token": token, "channel": channel}
    home = os.getenv("MY_PLATFORM_HOME_CHANNEL")
    if home:
        seed["home_channel"] = {"chat_id": home, "name": "Home"}
    return seed


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="my_platform",
        label="My Platform",
        adapter_factory=lambda cfg: MyPlatformAdapter(cfg),
        # PASSIVE probe — "are deps/config present right now?".  Called from
        # status displays and config loading, so it must NEVER pip-install.
        check_fn=check_requirements,
        # ACTIVE installer (optional) — only for platforms with a
        # lazy-installable SDK.  create_adapter() calls it when check_fn
        # returns False, right before the gateway connects the platform.
        # Typically wraps tools.lazy_deps.ensure_and_bind(...).  Omit it
        # and a False check_fn is a hard block.
        # ensure_deps_fn=ensure_requirements,
        validate_config=validate_config,
        required_env=["MY_PLATFORM_TOKEN"],
        install_hint="pip install my-platform-sdk",
        # Env-driven auto-configuration — seeds PlatformConfig.extra from
        # env vars before adapter construction. See "Env-Driven Auto-
        # Configuration" section below.
        env_enablement_fn=_env_enablement,
        # Cron home-channel delivery support. Lets deliver=my_platform cron
        # jobs route without editing cron/scheduler.py. See "Cron Delivery"
        # section below.
        cron_deliver_env_var="MY_PLATFORM_HOME_CHANNEL",
        # Per-platform user authorization env vars
        allowed_users_env="MY_PLATFORM_ALLOWED_USERS",
        allow_all_env="MY_PLATFORM_ALLOW_ALL_USERS",
        # Message length limit for smart chunking (0 = no limit)
        max_message_length=4000,
        # LLM guidance injected into system prompt
        platform_hint=(
            "You are chatting via My Platform. "
            "It supports markdown formatting."
        ),
        # Display
        emoji="💬",
    )

    # Optional: register platform-specific tools
    ctx.register_tool(
        name="my_platform_search",
        toolset="my_platform",
        schema={...},
        handler=my_search_handler,
    )
```

### Конфигурация

Пользователи настраивают платформу в `config.yaml`:

```yaml
gateway:
  platforms:
    my_platform:
      enabled: true
      extra:
        token: "..."
        channel: "#general"
```

Или через переменные среды (которые адаптер читает в `__init__`).

### Что система плагинов обрабатывает автоматически

Когда вы вызываете `ctx.register_platform()`, за вас обрабатываются следующие точки интеграции — никаких изменений основного кода не требуется:

| Точка интеграции | Как это работает |
|---|---|
| Создание адаптера шлюза | Реестр проверяется перед встроенной цепочкой if/elif |
| Разбор конфига | `Platform._missing_()` принимает любое имя платформы |
| Проверка подключенной платформы | Реестр `validate_config()` вызван |
| Авторизация пользователя | `allowed_users_env` / `allow_all_env` проверено |
| Автоматическое включение только для Env | `env_enablement_fn` семена `PlatformConfig.extra` + `home_channel` |
| Конфигурационный мост YAML | `apply_yaml_config_fn` переводит ключи `config.yaml` в переменные окружения/дополнения |
| Доставка Крон | `cron_deliver_env_var` заставляет `deliver=<name>` работать |
| `hermes config` Записи пользовательского интерфейса | `requires_env` / `optional_env` в `plugin.yaml` автозаполнение |
| механизм отправки (`tools/send_message_tool.py`) | Маршруты через адаптер живого шлюза |
| Кроссплатформенная доставка Webhook | Реестр проверен на наличие известных платформ |
| `/update` командный доступ | `allow_update_command` флаг |
| Каталог каналов | Платформы плагинов, включенные в список |
| Подсказки системы | `platform_hint` в контексте LLM |
| Разбиение сообщений | `max_message_length` для умного разделения |
| Редактирование личных данных | `pii_safe` флаг |
| `hermes status` | Показывает платформы плагинов с тегом `(plugin)` |
| `hermes gateway setup` | Платформы плагинов появляются в меню настроек |
| `hermes tools` / `hermes skills` | Платформы плагинов в конфигурации каждой платформы |
| Токен-блокировка (многопрофильная) | Используйте `acquire_scoped_lock()` в своем `connect()` |
| Предупреждение об потерянной конфигурации | Описательный журнал при отсутствии плагина |

## Автономные расширения пути отправки

Автономная платформа может участвовать в исходящей доставке через хост.
направьте `hermes send --to ...` и cron `deliver=platform:...`, объявив send
поведение на том же `PlatformEntry`, созданном `ctx.register_platform()`.
`send_message` намеренно не является инструментом модели, вызываемым агентом; плагины должны
не регистрировать эквивалентную поверхность модели, которая позволяет агенту инициировать исходящий трафик
сообщения самостоятельно.

```python
async def _send_request(args, chat_id, platform_name, pconfig):
    # `args` contains the host-driven send request fields.
    message_id = await client.send(
        address=chat_id,
        body=args["message"],
        subject=args.get("subject"),
    )
    return {"success": True, "platform": platform_name,
            "chat_id": chat_id, "message_id": message_id}


def _parse_address(raw):
    normalized = raw.strip().lower()
    if normalized.startswith("@") and "@" in normalized[1:]:
        return normalized, None  # (chat_id, optional thread_id)
    return None                 # continue to channel-directory resolution


def _validate_address(address):
    # True accepts; False rejects; a string rejects with that diagnostic.
    return True if address.endswith("@example.com") else "unsupported domain"


def register(ctx):
    ctx.register_platform(
        name="fmsg",
        label="Fixture Message",
        adapter_factory=lambda cfg: FmsgAdapter(cfg),
        check_fn=check_requirements,
        parse_target_ref_fn=_parse_address,
        validate_target_ref_fn=_validate_address,
        # May be a regular function or async def. Hermes awaits any awaitable
        # result, including callable objects and functools.partial wrappers.
        send_message_handler=_send_request,
        # Prefer this lower-level hook when cron must send from a process
        # without the live gateway.
        standalone_sender_fn=_standalone_send,
    )
```

Целевое разрешение распределяется по всем трем исходящим поверхностям. Вывод парсера
сначала нормализуется, а идентификаторы каталогов каналов являются доверенными. Парсер плагина должен
явно принять собственный целевой синтаксис; неразрешенные строки никогда не передаются
сквозь непрозрачно. Неизвестные платформы и сбои валидатора возвращают диагностическое сообщение.
вместо того, чтобы молча пытаться доставить. Плагин принудительной перезагрузки/профиля
переходы отменяют регистрацию принадлежащих им записей, поэтому парсеры и обработчики не могут проникнуть в
следующий профиль.

## Автоконфигурация на основе Env

Большинство пользователей настраивают платформу, добавляя переменные окружения в `~/.hermes/.env`, а не редактируя `config.yaml`. Хук `env_enablement_fn` позволяет вашему плагину выбирать эти переменные окружения **до** создания адаптера, поэтому `hermes gateway status`, `get_connected_platforms()` и доставка cron видят правильное состояние без создания экземпляра SDK платформы.

```python
def _env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from env vars.

    Called by the platform registry during load_gateway_config().
    Return None when the platform isn't minimally configured — the
    caller then skips auto-enabling. Return a dict to seed extras.

    The special 'home_channel' key is extracted and becomes a proper
    HomeChannel dataclass on the PlatformConfig; every other key is
    merged into PlatformConfig.extra.
    """
    token = os.getenv("MY_PLATFORM_TOKEN", "").strip()
    channel = os.getenv("MY_PLATFORM_CHANNEL", "").strip()
    if not (token and channel):
        return None
    seed = {"token": token, "channel": channel}
    home = os.getenv("MY_PLATFORM_HOME_CHANNEL")
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("MY_PLATFORM_HOME_CHANNEL_NAME", "Home"),
        }
    return seed


def register(ctx):
    ctx.register_platform(
        name="my_platform",
        label="My Platform",
        adapter_factory=lambda cfg: MyPlatformAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        env_enablement_fn=_env_enablement,
        # ... other fields
    )
```


## YAML→env Config Bridge

Некоторые пользователи предпочитают устанавливать ключи `config.yaml` (`my_platform.require_mention`, `my_platform.allowed_channels` и т. д.) вместо переменных окружения. Хук `apply_yaml_config_fn` позволяет вашему плагину владеть этим переводом вместо того, чтобы заставлять ядро ​​`gateway/config.py` знать схему YAML вашей платформы.

```python
import os

def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict | None:
    """Translate config.yaml `my_platform:` keys into env vars / extras.

    yaml_cfg     — the full top-level parsed config.yaml dict
    platform_cfg — the platform's own sub-dict (yaml_cfg.get("my_platform", {}))

    May mutate os.environ directly (use `not os.getenv(...)` guards to
    preserve env > YAML precedence) and/or return a dict to merge into
    PlatformConfig.extra. Return None or {} for no extras.
    """
    if "require_mention" in platform_cfg and not os.getenv("MY_PLATFORM_REQUIRE_MENTION"):
        os.environ["MY_PLATFORM_REQUIRE_MENTION"] = str(platform_cfg["require_mention"]).lower()
    allowed = platform_cfg.get("allowed_channels")
    if allowed is not None and not os.getenv("MY_PLATFORM_ALLOWED_CHANNELS"):
        if isinstance(allowed, list):
            allowed = ",".join(str(v) for v in allowed)
        os.environ["MY_PLATFORM_ALLOWED_CHANNELS"] = str(allowed)
    return None  # nothing extra to merge into PlatformConfig.extra

def register(ctx):
    ctx.register_platform(
        name="my_platform",
        ...,
        apply_yaml_config_fn=_apply_yaml_config,
    )
```

Перехват вызывается во время `load_gateway_config()` после общего цикла общего ключа (который обрабатывает общие ключи, такие как `unauthorized_dm_behavior`, `notice_delivery`, `reply_prefix`, `require_mention` и т. д.) и перед `_apply_env_overrides()`, поэтому вашему плагину нужно только соединить **ключи, специфичные для платформы**.

Исключения, вызванные перехватчиком, поглощаются и протоколируются на уровне отладки — плагин, работающий неправильно, никогда не прерывает загрузку конфигурации шлюза.


## Доставка Cron

Чтобы задания cron `deliver=my_platform` направлялись на настроенный домашний канал, задайте для `cron_deliver_env_var` имя переменной среды, которая содержит идентификатор чата/комнаты/канала по умолчанию:

```python
ctx.register_platform(
    name="my_platform",
    ...
    cron_deliver_env_var="MY_PLATFORM_HOME_CHANNEL",
)
```

Планировщик считывает эту переменную окружения при разрешении домашней цели для заданий `deliver=my_platform`, а также рассматривает платформу как допустимую цель cron при проверках в стиле `_KNOWN_DELIVERY_PLATFORMS`. Если ваш `env_enablement_fn` отправляет `home_channel` dict (см. выше), он имеет приоритет — `cron_deliver_env_var` является резервным вариантом для заданий cron, которые выполняются до заполнения env.

### Доставка cron вне процесса

`cron_deliver_env_var` делает вашу платформу признанной целью `deliver=`. Чтобы фактическая отправка прошла успешно, когда задание cron выполняется в процессе, отдельном от шлюза (т. е. `hermes cron run` отдельно от `hermes gateway`), зарегистрируйте `standalone_sender_fn`:

```python
async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Open an ephemeral connection / acquire a fresh token, send, and close."""
    # ... open connection, send message, return result ...
    return {"success": True, "message_id": "..."}
    # or {"error": "..."}

ctx.register_platform(
    name="my_platform",
    ...
    cron_deliver_env_var="MY_PLATFORM_HOME_CHANNEL",
    standalone_sender_fn=_standalone_send,
)
```

Почему этот крючок необходим: встроенные платформы (Telegram, Discord, Slack и т. д.) отправляют прямые помощники REST в `tools/send_message_tool.py`, поэтому cron может доставлять данные без удержания шлюза в том же процессе. Платформы плагинов исторически зависели от `_gateway_runner_ref()`, который возвращает `None` вне процесса шлюза, поэтому без `standalone_sender_fn` отправка на стороне cron завершается с ошибкой `No live adapter for platform '<name>'`.

Функция получает те же `pconfig` и `chat_id`, что и живой адаптер, плюс необязательные ключевые аргументы `thread_id`, `media_files` и `force_document`. Возврат `{"success": True, "message_id": ...}` рассматривается как успешная доставка; возврат `{"error": "..."}` отображает сообщение в `delivery_errors` cron. Исключения, возникающие внутри функции, перехватываются диспетчером и сообщаются как `Plugin standalone send failed: <reason>`. Эталонные реализации находятся в `plugins/platforms/{irc,teams,google_chat}/adapter.py`.

## Появление переменных окружения в `hermes config`

`hermes_cli/config.py` сканирует `plugins/platforms/*/plugin.yaml` во время импорта и автоматически заполняет `OPTIONAL_ENV_VARS` из блоков `requires_env` и (необязательно) `optional_env`. Используйте форму rich-dict для предоставления правильных описаний, подсказок, флагов паролей и URL-адресов — пользовательский интерфейс настройки CLI подберет их бесплатно.

```yaml
# plugins/platforms/my_platform/plugin.yaml
name: my_platform-platform
label: My Platform
kind: platform
version: 1.0.0
description: >
  My Platform gateway adapter for Hermes Agent.
author: Your Name
requires_env:
  - name: MY_PLATFORM_TOKEN
    description: "Bot API token from the My Platform console"
    prompt: "My Platform bot token"
    url: "https://my-platform.example.com/bots"
    password: true
  - name: MY_PLATFORM_CHANNEL
    description: "Channel to join (e.g. #hermes)"
    prompt: "Channel"
    password: false
optional_env:
  - name: MY_PLATFORM_HOME_CHANNEL
    description: "Default channel for cron delivery (defaults to MY_PLATFORM_CHANNEL)"
    prompt: "Home channel (or empty)"
    password: false
  - name: MY_PLATFORM_ALLOWED_USERS
    description: "Comma-separated user IDs allowed to talk to the bot"
    prompt: "Allowed users (comma-separated)"
    password: false
```

**Поддерживаемые ключи dict:** `name` (обязательно), `description`, `prompt`, `url`, `password` (bool; автоматически определяется по суффиксу `*_TOKEN` / `*_SECRET` / `*_KEY` / `*_PASSWORD` / `*_JSON`). если опущен), `category` (по умолчанию `"messaging"`).

Записи «голой строки» (`- MY_PLATFORM_TOKEN`) по-прежнему работают — они получают общее описание, автоматически полученное из `label` плагина. Если жестко закодированная запись для той же переменной уже существует в `OPTIONAL_ENV_VARS`, она выигрывает (обратная совместимость); форма плагина.yaml действует как запасной вариант.

## UX Slow-LLM для конкретной платформы

Некоторые платформы имеют ограничения, которые меняют способ представления медленного ответа LLM:

- **LINE** выдает одноразовый *токен ответа*, срок действия которого истекает примерно через 60 секунд после входящего события. Ответ с помощью этого токена бесплатен; возврат к дозированному API-интерфейсу Push невозможен. Если LLM не завершен к установленному сроку, можно выбрать «сжечь оплаченную квоту Push» или «сделать что-нибудь умнее с токеном ответа до истечения срока его действия».
- **WhatsApp** помечает сеанс как неактивный через 24 часа, после чего принимаются только шаблонные сообщения.
- В **SMS** нет концепции индикаторов ввода или прогрессивных обновлений — длинные ответы просто выглядят так, будто бот не в сети.

Это реальные ограничения, которые база `BasePlatformAdapter` не может предвидеть. Поверхность плагина намеренно оставляет место для адаптера, позволяющего накладывать специфичный для платформы UX поверх базового цикла набора текста без расширения списка kwarg.

### Шаблон: подкласс `_keep_typing` для слоя промежуточного UX

`BasePlatformAdapter._keep_typing` — это тактовый сигнал индикатора набора текста — он выполняется как фоновая задача во время генерации LLM и отменяется при доставке ответа. Чтобы наложить поведение, специфичное для платформы, на пороговое значение (например, отправить пузырь «все еще думает» через 45 секунд), переопределите `_keep_typing` в своем адаптере, запланируйте свою собственную задачу рядом с `super()._keep_typing()` и уничтожьте ее в `finally`:

```python
class LineAdapter(BasePlatformAdapter):
    async def _keep_typing(self, chat_id: str, *args, **kwargs) -> None:
        if self.slow_response_threshold <= 0:
            await super()._keep_typing(chat_id, *args, **kwargs)
            return

        async def _fire_at_threshold() -> None:
            try:
                await asyncio.sleep(self.slow_response_threshold)
            except asyncio.CancelledError:
                raise
            # Platform-specific work here — for LINE, send a Template
            # Buttons "Get answer" bubble using the cached reply token
            # so the user can fetch the cached response later via a
            # fresh (free) reply token from the postback callback.
            await self._send_slow_response_button(chat_id)

        side_task = asyncio.create_task(_fire_at_threshold())
        try:
            await super()._keep_typing(chat_id, *args, **kwargs)
        finally:
            if not side_task.done():
                side_task.cancel()
                try:
                    await side_task
                except (asyncio.CancelledError, Exception):
                    pass
```

Ключевые моменты:

- **Всегда `await super()._keep_typing(...)`.** Сердцебиение при наборе текста полезно независимо — не заменяйте его, накладывайте поверх него.
- **Снести побочную задачу в `finally`.** Когда LLM завершает работу (или `/stop` отменяет выполнение), шлюз отменяет задачу ввода. Ваша побочная задача также должна учитывать эту отмену, иначе она задержится и может сработать после того, как ответ уже был доставлен.
- **Соединитесь с `interrupt_session_activity`**, чтобы разрешить любое бесхозное состояние пользовательского интерфейса, когда пользователь вводит `/stop`. Для LINE это означает переход записи кэша обратной передачи с `PENDING` на `ERROR`, чтобы постоянная кнопка «Получить ответ» доставляла сообщение «Выполнение было прервано» вместо зацикливания.

### Шаблон: подкласс `send` для маршрутизации через кэш вместо немедленной отправки

Если ваш UX с медленным откликом кэширует ответ для последующего извлечения (поток обратной передачи LINE), ваше переопределение `send` должно распознавать три режима:

1. **Ожидание обратной передачи активно для этого чата** → кэшируйте ответ под request_id, не отправляйте ничего видимого.
2. **Подтверждение занятости системы** (`⚡ Interrupting`, `⏳ Queued`, `⏩ Steered`) → обходить кеш и отправлять видимую информацию, чтобы пользователь видел ответ шлюза на его ввод.
3. **Обычный ответ** → отправьте сообщение с помощью маркера ответа или push-уведомления, как обычно.

```python
async def send(self, chat_id: str, content: str, **kw) -> SendResult:
    if _is_system_bypass(content):
        return await self._send_text_chunks(chat_id, content, force_push=False)
    pending_rid = self._pending_buttons.get(chat_id)
    if pending_rid:
        self._cache.set_ready(pending_rid, content)
        return SendResult(success=True, message_id=pending_rid)
    return await self._send_text_chunks(chat_id, content, force_push=False)
```

`_SYSTEM_BYPASS_PREFIXES` — это собственные префиксы подтверждения занятости шлюза (`⚡`, `⏳`, `⏩`, `💾`). Всегда пропускайте их видимым образом, независимо от кэшированного состояния UX.

### Когда этот шаблон уместен

Используйте подход переопределения цикла ввода, когда:

- Исходящий API платформы имеет жесткое ограничение временного окна (одноразовый токен ответа, истекающий прикрепленный сеанс и т. д.) И
- *Видимый пузырь в середине полета* является приемлемым UX на этой платформе.

Используйте более простой путь `slow_response_threshold = 0` Always-Push, когда:

- На платформе нет значимого различия между бесплатным и платным, ИЛИ
- Сообщество пользователей предпочитает режим «загрузка… загрузка… ГОТОВО», а не интерактивный промежуточный пузырь.

LINE поддерживает оба варианта: пороговое значение по умолчанию равно 45 секундам для бесплатной выборки обратной передачи, а `LINE_SLOW_RESPONSE_THRESHOLD=0` возвращается к режиму «всегда отправлять резервную копию».

### Эталонная реализация

Полную реализацию обратной передачи LINE см. в `plugins/platforms/line/adapter.py` — конечный автомат `RequestCache` (`PENDING → READY → DELIVERED`, плюс `ERROR` для `/stop`), переопределение `_keep_typing`, которое запускает всплывающее окно «Кнопки шаблона» при пороговом значении, переопределение `send`, которое выполняет маршрутизацию через кеш, и переопределение `interrupt_session_activity`, которое разрешает потерянное ОЖИДАНИЕ. записи.

### Справочные реализации (путь к плагину)

Полный рабочий пример см. в `plugins/platforms/irc/` в репозитории — полностью асинхронный IRC-адаптер без внешних зависимостей. `plugins/platforms/teams/` охватывает Bot Framework/адаптивные карты, `plugins/platforms/google_chat/` охватывает API-интерфейсы REST на основе OAuth, а `plugins/platforms/line/` охватывает API-интерфейсы обмена сообщениями на основе веб-перехватчиков с специфичным для платформы медленным пользовательским интерфейсом LLM.

---

## Пошаговый контрольный список (встроенный путь)

:::примечание
Этот контрольный список предназначен для добавления платформы непосредственно в основную кодовую базу Hermes — обычно это делается основными участниками официально поддерживаемых платформ. Платформы сообщества и сторонние платформы должны использовать указанный выше [Путь к плагину](#plugin-path-recommended).
:::

### 1. Перечисление платформ

Добавьте свою платформу в перечисление `Platform` в `gateway/config.py`:

```python
class Platform(Enum):
    # ... existing platforms ...
    NEWPLAT = "newplat"
```

### 2. Файл адаптера

Создайте `plugins/platforms/newplat/adapter.py`:

```python
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter, MessageEvent, MessageType, SendResult,
)

def check_newplat_requirements() -> bool:
    """Return True if dependencies are available."""
    return SOME_SDK_AVAILABLE

class NewPlatAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.NEWPLAT)
        # Read config from config.extra dict
        extra = config.extra or {}
        self._api_key = extra.get("api_key") or os.getenv("NEWPLAT_API_KEY", "")

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Set up connection, start polling/webhook
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # Send message via platform API
        return SendResult(success=True, message_id="...")

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "dm"}
```

Для входящих сообщений создайте `MessageEvent` и вызовите `self.handle_message(event)`:

```python
source = self.build_source(
    chat_id=chat_id,
    chat_name=name,
    chat_type="dm",  # or "group"
    user_id=user_id,
    user_name=user_name,
)
event = MessageEvent(
    text=content,
    message_type=MessageType.TEXT,
    source=source,
    message_id=msg_id,
)
await self.handle_message(event)
```

### 3. Конфигурация шлюза (`gateway/config.py`)

Три точки соприкосновения:

1. **`get_connected_platforms()`** — добавьте проверку необходимых учетных данных вашей платформы.
2. **`load_gateway_config()`** — Добавить запись карты окружения токена: `Platform.NEWPLAT: "NEWPLAT_TOKEN"`
3. **`_apply_env_overrides()`** — Сопоставьте все переменные окружения `NEWPLAT_*` с конфигурацией.

### 4. Бегущий за воротами (`gateway/run.py`)

Шесть точек соприкосновения:

1. **`_create_adapter()`** — Добавьте ветку `elif platform == Platform.NEWPLAT:`.
2. **`_is_user_authorized()` Карта разрешенных_пользователей** — `Platform.NEWPLAT: "NEWPLAT_ALLOWED_USERS"`
3. **`_is_user_authorized()` картаallow_all** — `Platform.NEWPLAT: "NEWPLAT_ALLOW_ALL_USERS"`
4. **Ранняя проверка кортежа `_any_allowlist`** — добавьте `"NEWPLAT_ALLOWED_USERS"`.
5. **Ранняя проверка кортежа `_allow_all`** — добавьте `"NEWPLAT_ALLOW_ALL_USERS"`.
6. **`_UPDATE_ALLOWED_PLATFORMS` замороженный набор** — добавьте `Platform.NEWPLAT`

### 5. Кроссплатформенная доставка

1. **`gateway/platforms/webhook.py`** — добавьте `"newplat"` в кортеж типа доставки.
2. **`cron/scheduler.py`** — Добавить в `_KNOWN_DELIVERY_PLATFORMS` замороженный набор и карту платформы `_deliver_result()`.

### 6. Интеграция CLI

1. **`hermes_cli/config.py`** — добавьте все переменные `NEWPLAT_*` в `_EXTRA_ENV_KEYS`.
2. **`hermes_cli/gateway.py`** — добавьте запись в список `_PLATFORMS` с ключом, меткой, эмодзи, token_var, setup_instructions и переменными.
3. **`hermes_cli/platforms.py`** — добавьте запись `PlatformInfo` с меткой и default_toolset (используется TUI `skills_config` и `tools_config`).
4. **`hermes_cli/setup.py`** — добавьте функцию `_setup_newplat()` (можно делегировать `gateway.py`) и добавьте кортеж в список платформ обмена сообщениями.
5. **`hermes_cli/status.py`** — Добавьте запись обнаружения платформы: `"NewPlat": ("NEWPLAT_TOKEN", "NEWPLAT_HOME_CHANNEL")`.
6. **`hermes_cli/dump.py`** — добавьте `"newplat": "NEWPLAT_TOKEN"` в словарь обнаружения платформы.

### 7. Инструменты

1. **`tools/send_message_tool.py`** — добавьте `"newplat": Platform.NEWPLAT` на карту платформы.
2. **`tools/cronjob_tools.py`** — добавьте `newplat` в строку описания цели доставки.

### 8. Наборы инструментов

1. **`toolsets.py`** — добавьте определение набора инструментов `"hermes-newplat"` с помощью `_HERMES_CORE_TOOLS`.
2. **`toolsets.py`** — добавьте `"hermes-newplat"` в список включений `"hermes-gateway"`.

### 9. Необязательно: подсказки по платформе

**`agent/prompt_builder.py`** — Если ваша платформа имеет определенные ограничения на отрисовку (нет уценки, ограничения на длину сообщения и т. д.), добавьте запись в `PLATFORM_HINTS` dict. Это вводит рекомендации для конкретной платформы в системную подсказку:

```python
PLATFORM_HINTS = {
    # ...
    "newplat": (
        "You are chatting via NewPlat. It supports markdown formatting "
        "but has a 4000-character message limit."
    ),
}
```

Не всем платформам нужны подсказки — добавляйте их только в том случае, если поведение агента должно отличаться.

### 10. Тесты

Создайте `tests/gateway/test_newplat.py` покрытие:

- Конструкция адаптера из конфига
- Построение событий сообщений
- Метод отправки (имитация внешнего API)
- Особенности платформы (шифрование, маршрутизация и т. д.)

### 11. Документация

| Файл | Что добавить |
|------|-------------|
| `website/docs/user-guide/messaging/newplat.md` | Полная страница настройки платформы |
| `website/docs/user-guide/messaging/index.md` | Таблица сравнения платформ, схема архитектуры, таблица наборов инструментов, раздел безопасности, ссылка на следующие шаги |
| `website/docs/reference/environment-variables.md` | Все переменные среды NEWPLAT_* |
| `website/docs/reference/toolsets-reference.md` | набор инструментов гермес-newplat |
| `website/docs/integrations/index.md` | Ссылка на платформу |
| `website/sidebars.ts` | Запись на боковой панели страницы документации |
| `website/docs/developer-guide/architecture.md` | Количество адаптеров + список |
| `website/docs/developer-guide/gateway-internals.md` | Список файлов адаптера |

## Аудит четности

Прежде чем отметить PR новой платформы как завершенный, запустите аудит четности для установленной платформы:

```bash
# Find every .py file mentioning the reference platform
search_files "bluebubbles" output_mode="files_only" file_glob="*.py"

# Find every .py file mentioning the new platform
search_files "newplat" output_mode="files_only" file_glob="*.py"

# Any file in the first set but not the second is a potential gap
```

Повторите эти действия для файлов `.md` и `.ts`. Исследуйте каждый пробел — это перечисление платформ (требует обновления) или ссылка на конкретную платформу (пропустить)?

## Общие шаблоны

### Адаптеры длинного опроса

Если ваш адаптер использует длинный опрос (например, Telegram или Weixin), используйте задачу цикла опроса:

```python
async def connect(self):
    self._poll_task = asyncio.create_task(self._poll_loop())
    self._mark_connected()

async def _poll_loop(self):
    while self._running:
        messages = await self._fetch_updates()
        for msg in messages:
            await self.handle_message(self._build_event(msg))
```

### Адаптеры обратного вызова/веб-перехватчика

Если платформа отправляет сообщения на вашу конечную точку (например, обратный вызов WeCom), запустите HTTP-сервер:

```python
async def connect(self):
    self._app = web.Application()
    self._app.router.add_post("/callback", self._handle_callback)
    # ... start aiohttp server
    self._mark_connected()

async def _handle_callback(self, request):
    event = self._build_event(await request.text())
    await self._message_queue.put(event)
    return web.Response(text="success")  # Acknowledge immediately
```

Для платформ с жесткими сроками ответа (например, 5-секундный лимит WeCom) всегда подтверждайте запрос немедленно и доставляйте ответ агента заранее через API позже. Сеансы агентов длятся 3–30 минут — встроенные ответы в окне обратного вызова невозможны.

### Блокировки токенов

Если адаптер поддерживает постоянное соединение с уникальными учетными данными, добавьте блокировку области действия, чтобы предотвратить использование одних и тех же учетных данных двумя профилями:

```python
from gateway.status import acquire_scoped_lock, release_scoped_lock

async def connect(self, *, is_reconnect: bool = False):
    acquired, _existing = acquire_scoped_lock("newplat", self._token)
    if not acquired:
        logger.error("Token already in use by another profile")
        return False
    # ... connect

async def disconnect(self):
    release_scoped_lock("newplat", self._token)
```

## Эталонные реализации

| Адаптер | Узор | Сложность | Хорошая ссылка для |
|---------|---------|------------|-------------------|
| `bluebubbles.py` | ОТДЫХ + вебхук | Средний | Простая интеграция REST API |
| `weixin.py` | Длинный опрос + CDN | Высокий | Обработка мультимедиа, шифрование |
| `plugins/platforms/wecom/callback_adapter.py` | Обратный вызов/вебхук | Средний | HTTP-сервер, шифрование AES, несколько приложений |
| `plugins/platforms/irc/adapter.py` | Длинный опрос + протокол IRC | Высокий | Полнофункциональный плагин-адаптер с блокировкой токена |