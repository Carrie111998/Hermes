---
sidebar_position: 8
title: Плагины поставщиков памяти
description: Как создать плагин поставщика памяти для агента Hermes
---

# Создание плагина поставщика памяти

Плагины поставщика памяти предоставляют агенту Hermes постоянные межсессионные знания, выходящие за рамки встроенных MEMORY.md и USER.md. В этом руководстве рассказывается, как его создать.

:::совет
Поставщики памяти — это один из двух типов **плагинов поставщиков**. Другой — [Плагины контекстного движка](/developer-guide/context-engine-plugin), которые заменяют встроенный компрессор контекста. Оба следуют одному и тому же шаблону: одиночный выбор, управление на основе конфигурации, управление через `hermes plugins`.
:::

## Схемы установки

Hermes обнаруживает поставщиков памяти из четырех источников в следующем порядке:

| Источник | Расположение | Заметки |
|---|---|---|
| В комплекте | `plugins/memory/<name>/` | Поставляется с Гермесом. Закрыто для новых поставщиков — см. [ВКЛАД](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md). |
| Пользователь | `$HERMES_HOME/plugins/<name>/` | Загружено пользователем для каждого профиля. |
| Проект | `./.hermes/plugins/<name>/` | Зарегистрируйтесь через `HERMES_ENABLE_PROJECT_PLUGINS=1`. |
| Пакет | `hermes_agent.memory_providers` точка входа | `pip install`, копировать нечего. |

Более ранние источники выигрывают из-за конфликта имен, поэтому каталог попадает в рабочий
дерево никогда не сможет скрыть поставляемого поставщика.

:::примечание
Это обратный порядок последующих побед в общей системе плагинов. Память
провайдер активируется *имя* (`memory.provider`), поэтому теневое копирование будет
незаметно перенаправить память агента, а не просто переопределить инструмент.
:::

Discovery только *перечисляет* — он никогда не импортирует провайдера. Ничего не работает, пока
`memory.provider` называет это.

### Поставщик каталогов

Поставщик каталогов находится в `plugins/memory/<name>/`, если он связан с
Hermes, в `$HERMES_HOME/plugins/<name>/` при установке пользователем или в
`./.hermes/plugins/<name>/` для локального проекта:

```
plugins/memory/my-provider/
├── __init__.py      # MemoryProvider implementation + register() entry point
├── plugin.yaml      # Metadata (name, description, hooks)
└── README.md        # Setup instructions, config reference, tools
```

### Пакетный поставщик

Поставщик, установленный через pip, публикует точку входа в
`hermes_agent.memory_providers` группа. Имя точки входа — поставщик
имя, которое пользователи выбирают в `memory.provider`; его значение указывает на поставщика
`register(ctx)` функция:

```toml title="pyproject.toml"
[project.entry-points."hermes_agent.memory_providers"]
my-provider = "my_provider:register"
```

Наведите точку входа на **пакет** или на `register(ctx)` внутри него и
сохраняйте свою реализацию, навыки и другие ресурсы в обычном Python.
макет упаковки. Копия под номером `$HERMES_HOME/plugins/` не требуется.

Точка входа пакета получает все, что делает установка из каталога, включая
два файла, которые Hermes читает с диска, а не импортирует — `config_schema.py`
(панель конфигурации информационной панели) и `cli.py` (ваш `hermes <provider>`
подкоманды). Оба находятся рядом с `__init__.py` вашего пакета, поэтому укажите
точка входа в пакете, а не в отдельном модуле, если вы отправляете любой из них.

## Азбука поставщика памяти

Ваш плагин реализует абстрактный базовый класс `MemoryProvider` из `agent/memory_provider.py`:

```python
from agent.memory_provider import MemoryProvider

class MyMemoryProvider(MemoryProvider):
    @property
    def name(self) -> str:
        return "my-provider"

    def is_available(self) -> bool:
        """Check if this provider can activate. NO network calls."""
        return bool(os.environ.get("MY_API_KEY"))

    def initialize(self, session_id: str, **kwargs) -> None:
        """Called once at agent startup.

        kwargs always includes:
          hermes_home (str): Active HERMES_HOME path. Use for storage.
        """
        self._api_key = os.environ.get("MY_API_KEY", "")
        self._session_id = session_id

    # ... implement remaining methods
```

## Обязательные методы

### Основной жизненный цикл

| Метод | При вызове | Должен реализовать? |
|--------|-----------|-----------------|
| `name` (недвижимость) | Всегда | **Да** |
| `is_available()` | Инициализация агента перед активацией | **Да** — нет сетевых вызовов |
| `initialize(session_id, **kwargs)` | Запуск агента | **Да** |
| `get_tool_schemas()` | После инициализации для внедрения инструмента | **Да** |
| `handle_tool_call(tool_name, args, **kwargs)` | Когда агент использует ваши инструменты | **Да** (если у вас есть инструменты) |

### Конфигурация

| Метод | Цель | Должен реализовать? |
|--------|---------|-----------------|
| `get_config_schema()` | Объявить поля конфигурации для `hermes memory setup` | **Да** |
| `save_config(values, hermes_home)` | Записать несекретную конфигурацию в исходное расположение | **Да** (если только env-var) |

### Дополнительные крючки

| Метод | При вызове | Вариант использования |
|--------|-----------|----------|
| `system_prompt_block()` | Система оперативной сборки | Статическая информация о провайдере |
| `prefetch(query, *, session_id="")` | Перед каждым вызовом API | Вернуть вызванный контекст |
| `queue_prefetch(query, *, session_id="")` | После каждого хода | Предварительный разогрев для следующего поворота |
| `sync_turn(user, assistant, *, session_id="", messages=None)` | После каждого завершенного хода | Продолжать разговор |
| `on_session_end(messages)` | Разговор заканчивается | Окончательная экстракция/промывка |
| `on_pre_compress(messages)` | До сжатия контекста | Сохраните информацию, прежде чем удалить ее |
| `on_memory_write(action, target, content)` | Встроенная память пишет | Зеркало для вашего бэкэнда |
| `shutdown()` | Выход из процесса | Очистка соединений |

## Контрольные точки предварительного сжатия (закрытие при сбое)

`on_pre_compress()` по умолчанию является максимальным: если ваш провайдер повышает ставку,
хост регистрирует сбой, и сжатие продолжается. Это правильное значение по умолчанию для
извлечение информации — и это неправильный метод для провайдера, чья работа заключается в архивировании
расшифровать доказательства в надежное хранилище *до* перезаписи с потерями. Для этого
если хост предлагает договор контрольной точки согласия (API v2):

```python
from agent.memory_provider import MemoryProvider

class MyArchivingProvider(MemoryProvider):
    # Opt in: every successful on_pre_compress() return means the durable
    # checkpoint is committed. Raise on any failure — do not return partial
    # success. Version 1 (the inherited default) is the implicit historical
    # contract: best-effort semantics, raw message list.
    pre_compress_checkpoint_api_version = 2

    def on_pre_compress(self, messages):
        ids = self._archive(messages)   # must be durable before returning
        return f"checkpoint: {ids}"     # forwarded into the summary prompt
```

Операторы включают принудительное применение для каждого развертывания:

```yaml
compression:
  checkpoint_required: true   # default: false
```

При включенном шлюзе сжатие **закрывается** перед любой перезаписью с потерями, если только
активный провайдер, рекламирующий API, завершил свою контрольную точку:
несжатый транскрипт сохраняется, при попытке уплотнения возникают ошибки
`BLOCKED_MISSING_PREREQUISITE`, и его можно будет повторить, как только ваш магазин
выздоравливает. Если шлюз отключен (по умолчанию), для существующих поставщиков ничего не меняется.

Врата привязаны ко всем органам уплотнения, а не только к Гермесу.
сумматор: собственное уплотнение на стороне сервера (`compression.codex_responses_native`)
подавляется, пока ворота поставлены на охрану, микроуплотнение после поворота
(`compression.micro_compact`) принудительно отключается при инициализации агента (он поглощает старые
преобразуется в скользящую сводку без каких-либо контрольных точек на пути), и
режим API `codex_app_server` отклоняется при инициализации агента — агент кодекса
уплотняет свою собственную нить без истинной границы предварительного уплотнения, поэтому
требуемый контрольно-пропускной пункт там не может быть гарантирован. Гермес, умеющий контролировать контрольно-пропускные пункты
компрессор остается единственным источником потерь.

То, что получает ваш провайдер, зависит от заявленной версии API. Версия 1
поставщики (неявное значение по умолчанию — каждый ранее существовавший поставщик) сохраняют
исторический контракт: необработанный список сообщений, как и раньше. Версия 2
Вместо этого поставщики контрольных точек получают нормализованные прямые доказательства:
только текстовые строки пользователя/помощника — результаты инструментов, системные сообщения,
`tool_calls` полезная нагрузка сообщений помощника (их текст сохраняется), и предыдущие
сводки уплотнения фильтруются на стороне хоста. Предыдущие резюме признаются
через постоянный маркер сообщения `_compressed_summary`, который сохраняется в процессе
перезапускается, поэтому возобновленный сеанс никогда не передает сводные данные обратно в
ваш архив.

**Контрольные точки должны быть идемпотентными.** После неудачно закрытого блока следующий
попытка уплотнения снова вызывает `on_pre_compress()` с той же расшифровкой —
и транскрипт, который увеличился лишь незначительно, дает в значительной степени перекрывающиеся
доказательства. Ключ вашего архива пишется по содержимому (например, расшифровка
дайджест) и upsert, поэтому повторные попытки и перекрытие дедупликации вместо
накопление дублирующих архивов.

Контрактные испытания: `tests/agent/test_pre_compress_checkpoint_contract.py`.

## Схема конфигурации

`get_config_schema()` возвращает список дескрипторов полей, используемых `hermes memory setup`:

```python
def get_config_schema(self):
    return [
        {
            "key": "api_key",
            "description": "My Provider API key",
            "secret": True,           # → written to .env
            "required": True,
            "env_var": "MY_API_KEY",   # explicit env var name
            "url": "https://my-provider.com/keys",  # where to get it
        },
        {
            "key": "region",
            "description": "Server region",
            "default": "us-east",
            "choices": ["us-east", "eu-west", "ap-south"],
        },
        {
            "key": "project",
            "description": "Project identifier",
            "default": "hermes",
        },
    ]
```

Поля с `secret: True` и `env_var` переходят в `.env`. Несекретные поля передаются в `save_config()`.

:::tip Минимальная и полная схема
Каждое поле в `get_config_schema()` запрашивается в течение `hermes memory setup`. Поставщикам с большим количеством опций следует сохранять схему минимальной — включать только поля, которые пользователь **должен** настроить (ключ API, необходимые учетные данные). Документируйте дополнительные настройки в ссылке на файл конфигурации (например, `$HERMES_HOME/myprovider.json`), а не запрашивайте их все во время установки. Это позволяет ускорить работу мастера установки, сохраняя при этом поддержку расширенной настройки. См. пример поставщика Supermemory — он запрашивает только ключ API; все остальные параметры находятся в `supermemory.json`.
:::

## Сохранить конфигурацию

```python
def save_config(self, values: dict, hermes_home: str) -> None:
    """Write non-secret config to your native location."""
    import json
    from pathlib import Path
    config_path = Path(hermes_home) / "my-provider.json"
    config_path.write_text(json.dumps(values, indent=2))
```

Для поставщиков только env-var оставьте значение по умолчанию no-op.

## Точка входа плагина

```python
def register(ctx) -> None:
    """Called by the memory plugin discovery system."""
    ctx.register_memory_provider(MyMemoryProvider())
```

Поставщик также может предоставлять навыки, доступные только для чтения, из того же обратного вызова. Навыки
уточняются по имени точки входа и загружаются только тогда, когда этот поставщик памяти
активен:

```python
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"

def register(ctx) -> None:
    ctx.register_memory_provider(MyMemoryProvider())
    ctx.register_skill(
        "maintenance",
        SKILLS_DIR / "maintenance" / "SKILL.md",
        "Maintain the provider's memory store",
    )
```

При активной точке входа `my-provider` навык доступен как
С `my-provider:maintenance` по `skill_view()`.

## плагин.yaml

```yaml
name: my-provider
version: 1.0.0
description: "Short description of what this provider does."
hooks:
  - on_session_end    # list hooks you implement
```

## Потоковый контракт

**`sync_turn()` ДОЛЖЕН быть неблокирующим.** Если ваш сервер имеет задержку (вызовы API, обработка LLM), запустите работу в потоке демона:

```python
def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
    def _sync():
        try:
            self._api.ingest(user_content, assistant_content, session_id=session_id, messages=messages)
        except Exception as e:
            logger.warning("Sync failed: %s", e)

    if self._sync_thread and self._sync_thread.is_alive():
        self._sync_thread.join(timeout=5.0)
    self._sync_thread = threading.Thread(target=_sync, daemon=True)
    self._sync_thread.start()
```

`messages` — это необязательный контекст диалога в стиле OpenAI на момент завершения.
поворот. Если он присутствует, он включает в себя сообщения пользователя/помощника, вызовы инструментов помощника,
и сообщения о результатах работы инструмента. Провайдеры, которым не нужен необработанный контекст поворота, могут опустить
параметр `messages`; Гермес продолжит звонить им с наследием
подпись.

Поставщики облачных услуг должны документировать, какие части `messages` отправляются с устройства.
Вызовы инструментов и результаты работы инструментов могут содержать пути к файлам, выходные данные команд или другие
данные рабочей области.

## Изоляция профиля

Все пути хранения **должны** использовать kwarg `hermes_home` из `initialize()`, а не жестко запрограммированный `~/.hermes`:

```python
# CORRECT — profile-scoped
from hermes_constants import get_hermes_home
data_dir = get_hermes_home() / "my-provider"

# WRONG — shared across all profiles
data_dir = Path("~/.hermes/my-provider").expanduser()
```

## Тестирование

Сквозные шаблоны см. в `tests/agent/test_memory_provider.py` и смежных тестах памяти (`tests/agent/test_memory_session_switch.py`, `tests/agent/test_memory_user_id.py`, `tests/run_agent/test_memory_provider_init.py`).

```python
from agent.memory_manager import MemoryManager

mgr = MemoryManager()
mgr.add_provider(my_provider)
mgr.initialize_all(session_id="test-1", platform="cli")

# Test tool routing
result = mgr.handle_tool_call("my_tool", {"action": "add", "content": "test"})

# Test lifecycle
mgr.sync_all("user msg", "assistant msg")
mgr.on_session_end([])
mgr.shutdown_all()
```

## Добавление команд CLI

Плагины поставщика памяти могут регистрировать собственное дерево подкоманд CLI (например, `hermes my-provider status`, `hermes my-provider config`). При этом используется система обнаружения, основанная на соглашениях — никаких изменений в основных файлах не требуется.

### Как это работает

1. Добавьте файл `cli.py` в каталог вашего плагина.
2. Определите функцию `register_cli(subparser)`, которая строит дерево argparse.
3. Система плагинов памяти обнаруживает его при запуске через `discover_plugin_cli_commands()`.
4. Ваши команды появятся под `hermes <provider-name> <subcommand>`.

**Связь с активным провайдером.** Ваши команды CLI появляются только в том случае, если ваш провайдер является активным `memory.provider` в конфигурации. Если пользователь не настроил вашего провайдера, ваши команды не будут отображаться в `hermes --help`.

### Пример

```python
# plugins/memory/my-provider/cli.py

def my_command(args):
    """Handler dispatched by argparse."""
    sub = getattr(args, "my_command", None)
    if sub == "status":
        print("Provider is active and connected.")
    elif sub == "config":
        print("Showing config...")
    else:
        print("Usage: hermes my-provider <status|config>")

def register_cli(subparser) -> None:
    """Build the hermes my-provider argparse tree.

    Called by discover_plugin_cli_commands() at argparse setup time.
    """
    subs = subparser.add_subparsers(dest="my_command")
    subs.add_parser("status", help="Show provider status")
    subs.add_parser("config", help="Show provider config")
    subparser.set_defaults(func=my_command)
```

### Эталонная реализация

См. `plugins/memory/honcho/cli.py` полный пример с 13 подкомандами, межпрофильным управлением (`--target-profile`) и чтением/записью конфигурации.

### Структура каталогов с помощью CLI

```
plugins/memory/my-provider/
├── __init__.py      # MemoryProvider implementation + register()
├── plugin.yaml      # Metadata
├── cli.py           # register_cli(subparser) — CLI commands
└── README.md        # Setup instructions
```

## Правило единого поставщика

Одновременно может быть активен только **один** поставщик внешней памяти. Если пользователь пытается зарегистрировать секунду, MemoryManager отклоняет его с предупреждением. Это предотвращает раздувание схемы инструмента и конфликты серверных частей.