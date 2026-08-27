---
sidebar_position: 9
title: Плагины контекстного движка
description: Как создать плагин контекстного движка, который заменяет встроенный ContextCompressor
---

# Создание плагина контекстного движка

Плагины механизма контекста заменяют встроенный `ContextCompressor` альтернативной стратегией управления контекстом разговора. Например, механизм управления контекстом без потерь (LCM), который создает базу данных знаний вместо обобщения с потерями.

## Как это работает

Управление контекстом агента построено на основе `ContextEngine` ABC (`agent/context_engine.py`). Встроенный `ContextCompressor` является реализацией по умолчанию. Механизмы плагинов должны реализовывать один и тот же интерфейс.

Одновременно может быть активен только **один** механизм контекста. Выбор зависит от конфигурации:

```yaml
# config.yaml
context:
  engine: "compressor"    # default built-in
  engine: "lcm"           # activates a plugin engine named "lcm"
```

Механизмы плагинов **никогда не активируются автоматически** — пользователь должен явно указать `context.engine` для имени плагина.

## Структура каталогов

Каждый контекстный движок находится в `plugins/context_engine/<name>/`:

```
plugins/context_engine/lcm/
├── __init__.py      # exports the ContextEngine subclass
├── plugin.yaml      # metadata (name, description, version)
└── ...              # any other modules your engine needs
```

## Азбука ContextEngine

Ваш движок должен реализовать следующие **обязательные** методы:

```python
from agent.context_engine import ContextEngine

class LCMEngine(ContextEngine):

    @property
    def name(self) -> str:
        """Short identifier, e.g. 'lcm'. Must match config.yaml value."""
        return "lcm"

    def update_from_response(self, usage: dict) -> None:
        """Called after every LLM call with the usage dict.

        Update self.last_prompt_tokens, self.last_completion_tokens,
        self.last_total_tokens from the response.
        """

    def should_compress(self, prompt_tokens: int = None) -> bool:
        """Return True if compaction should fire this turn."""

    def compress(self, messages: list, current_tokens: int = None,
                 focus_topic: str = None) -> list:
        """Compact the message list and return a new (possibly shorter) list.

        The returned list must be a valid OpenAI-format message sequence.

        ``focus_topic`` is an optional topic string from manual
        ``/compress <focus>``; engines that support guided compression should
        prioritise preserving information related to it, others may ignore it.
        """
```

### Атрибуты класса, которые должен поддерживать ваш движок

Агент считывает их непосредственно для отображения и регистрации:

```python
last_prompt_tokens: int = 0
last_completion_tokens: int = 0
last_total_tokens: int = 0
threshold_tokens: int = 0        # when compression triggers
context_length: int = 0          # model's full context window
compression_count: int = 0       # how many times compress() has run
```

### Дополнительные методы

У них есть разумные значения по умолчанию в ABC. Переопределите при необходимости:

| Метод | По умолчанию | Переопределить, когда |
|--------|---------|--------------|
| `on_session_start(session_id, **kwargs)` | Нет операции | Вам необходимо загрузить постоянное состояние (DAG, DB) |
| `on_session_end(session_id, messages)` | Нет операции | Вам нужно очистить состояние, закрыть соединения |
| `on_session_reset()` | Сбрасывает счетчики токенов | Вам нужно очистить состояние каждого сеанса |
| `update_model(model, context_length, ...)` | Обновления context_length + порог | Вам необходимо пересчитать бюджеты при смене модели |
| `get_tool_schemas()` | Возвращает `[]` | Ваш движок предоставляет инструменты, вызываемые агентом (например, `lcm_grep`) |
| `handle_tool_call(name, args, **kwargs)` | Возвращает ошибку JSON | Вы реализуете обработчики инструментов |
| `should_compress_preflight(messages)` | Возвращает `False` | Вы можете сделать дешевую оценку перед вызовом API |
| `get_status()` | Стандартный токен/пороговый словарь | У вас есть специальные метрики для раскрытия |
| `select_context(request_messages, *, conversation_messages, incoming_message, budget_tokens)` | Возвращает `None` (без операций) | Вы выбираете/маршрутизируете, какой контекст входит в **этот** запрос (извлечение, маршрутизация тем) — см. ниже |
| `on_turn_complete(messages, usage=None, **kwargs)` | Нет операции | Вы поглощаете/индексируете/наблюдаете за завершенным ходом — см. ниже |

## Выбор и наблюдение контекста за ход

`compress()` отвечает: «Контекст слишком длинный → сделайте его короче». Два дополнительных,
Хуки no-op-default охватывают ортогональную ось *выбора/наблюдения*, поэтому
движку больше не приходится принудительно переводить `should_compress()` в `True` и злоупотреблять
`compress()` как обратный вызов за ход:

```python
def select_context(self, request_messages, *, conversation_messages=None,
                   incoming_message=None, budget_tokens=0):
    """Choose/replace the context for THIS request, before dispatch.

    Return a new message list to use for this one provider call (retrieval,
    topic routing, role/branch switching), or None to leave it unchanged.
    Request-only: the persisted conversation history is never mutated.
    """

def on_turn_complete(self, messages, usage=None, **kwargs):
    """Observe a finished turn after the assistant/tool loop completes.

    Receives a shallow copy of the finalized transcript plus the turn's
    canonical usage dict (or None if no provider response was reached), so the
    engine can ingest/index/summarize for the next select_context(). The return
    value is ignored.
    """
```

Контракт:

- **По умолчанию нет операций, открытие при сбое.** Оба значения по умолчанию — `return None`. Отсутствующий хук, исключение или недопустимое возвращаемое значение оставляют запрос нетронутым, поэтому сбойный движок никогда не бывает хуже, чем его отсутствие. Хост также проверяет подлинность унаследованного значения по умолчанию ABC и полностью пропускает его, поэтому нереализующие механизмы (включая встроенный компрессор) вообще не оплачивают работу по каждому запросу.
- **`select_context()` предназначен только для запроса.** Возвращаемый список заменяет сообщения для одного вызова провайдера; сохранившаяся история никогда не пишется. Возврат `None`, `[]`, не-списка или списка, содержащего не-дикты, все доступны для немодифицированного запроса.
- **Стабильность порядка/кеша.** Хук запускается **перед** контролем кэша подсказок и дезинфицирующим средством каждого запроса, поэтому (а) замена по-прежнему проходит ту же проверку, что и любой запрос, и (б) отсутствие операций по умолчанию оставляет запрос байтовым - поведение кэша подсказок остается неизменным для нереализующих механизмов. Механизм, заменяющий список, меняет только свой собственный префикс кэша. Оценивается по запросу поставщика (повторное выполнение при повторных попытках).
- **`on_turn_complete()`** — наблюдение только после поворота; рассматривать `messages` как доступный только для чтения. **Укрытие максимально возможное:** оно выполняется из стандартного завершающего шва. Некоторые аномальные пути раннего возврата в цикле (например, блокировка политики контента или сбой терминала провайдера) сохраняются и возвращаются без маршрутизации через финализацию, поэтому в настоящее время они не создают этот хук — рассматривайте его как максимально возможное наблюдение за завершенными ходами, а не как гарантированный обратный вызов для каждого раннего выхода. Объединение всех конечных путей за одним швом финализации — это отдельное продолжение.

### Когда использовать эти хуки, а когда НЕ следует

- **Реализуйте `select_context()` только тогда, когда ваш движок должен *заменить*
  контекст для каждого запроса** — выбор с расширенным поиском, маршрутизация по теме/ветви,
  переключение ролей. Это единственный глагол, который может менять местами сообщения, входящие в
  запрос: крючок плагина `pre_llm_call` доступен только для внедрения согласно документированной конструкции
  (он добавляется к сообщению пользователя и никогда не перезаписывает список, чтобы сохранить
  префикс Prompt-cache). Если вам не нужна замена, не внедряйте ее.
- **Если вашему плагину требуется только наблюдение/поглощение после поворота** (индексирование,
  синхронизация памяти, аналитика), внедрить **поставщик памяти** (`sync_turn()` —
  см. [Плагины поставщика памяти](./memory-provider-plugin.md)) вместо
  контекстный движок. Контекстный движок берет на себя ответственность за сжатие сеанса.
  политика; поставщик памяти наблюдает за ходами, ничего не владея.
  `on_turn_complete()` существует как зеркало наблюдения для двигателей, которые
  *уже* нужен `select_context()` — чтобы тот же компонент мог учиться на
  Turn он просто маршрутизируется, а не как обратный вызов Turn общего назначения.
- **Влияние реального `select_context()` на кэш подсказок.** Ненулевой выбор
  естественным образом меняет префикс Prompt-Cache для ходов, где он меняет
  выбор — префикс этого запроса больше не соответствует кэшированному провайдером
  префикс, поэтому эти повороты перезаписывают кеш, а не читают его. Двигатели должны
  возвращать **стабильные выборки, когда ничего не изменилось** (тот же объект или
  равный список) и изменяют контекст только тогда, когда решение о маршрутизации действительно
  отличается; выбор, который перетасовывается за ход, бесшумно лишается возможности повторного использования кеша
  каждый поворот.

## Инструменты двигателя

Механизмы контекста могут предоставлять инструменты, которые агент вызывает напрямую. Возврат схем из `get_tool_schemas()` и обработка вызовов в `handle_tool_call()`:

```python
def get_tool_schemas(self):
    return [{
        "name": "lcm_grep",
        "description": "Search the context knowledge graph",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"],
        },
    }]

def handle_tool_call(self, name, args, **kwargs):
    if name == "lcm_grep":
        results = self._search_dag(args["query"])
        return json.dumps({"results": results})
    return json.dumps({"error": f"Unknown tool: {name}"})
```

Инструменты ядра добавляются в список инструментов агента при запуске и отправляются автоматически — регистрация в реестре не требуется.

## Регистрация

### Через каталог (рекомендуется)

Поместите свой двигатель в `plugins/context_engine/<name>/`. `__init__.py` должен экспортировать подкласс `ContextEngine`. Система обнаружения автоматически находит и создает экземпляр.

### Через общую систему плагинов

Общий плагин также может зарегистрировать контекстный движок:

```python
def register(ctx):
    engine = LCMEngine(context_length=200000)
    ctx.register_context_engine(engine)
```

Можно зарегистрировать только один двигатель. Второй плагин, пытающийся зарегистрироваться, отклонен с предупреждением.

## Жизненный цикл

```
1. Engine instantiated (plugin load or directory discovery)
2. on_session_start() — conversation begins
3. update_from_response() — after each API call
4. should_compress() — checked each turn
5. compress() — called when should_compress() returns True
6. on_session_end() — session boundary (CLI exit, /reset, gateway expiry)
```

`on_session_reset()` вызывается на `/new` или `/reset` для очистки состояния каждого сеанса без полного завершения работы.

## Конфигурация

Пользователи выбирают ваш движок через `hermes plugins` → Плагины провайдера → Context Engine или редактируя `config.yaml`:

```yaml
context:
  engine: "lcm"   # must match your engine's name property
```

Блок конфигурации `compression` (`compression.threshold`, `compression.protect_last_n` и т. д.) относится только к встроенному `ContextCompressor`, за одним явным исключением: `compression.model_thresholds` (переопределение пороговых значений для каждой модели) является частью контракта контекстного механизма. Хост назначает разрешенную карту `engine.model_thresholds` *перед* начальным вызовом `update_model()`, а базовый класс `update_model()` применяет ее (совпадение самой длинной подстроки, возврат к настроенному порогу механизма). Механизмы, которые переопределяют `update_model()`, имеют собственную политику сжатия и могут учитывать или игнорировать карту — `from agent.context_compressor import resolve_model_threshold`, чтобы повторно использовать ту же логику разрешения. Что касается всего остального, ваш движок должен при необходимости определить свой собственный формат конфигурации, читая из `config.yaml` во время инициализации.

## Тестирование

```python
from agent.context_engine import ContextEngine

def test_engine_satisfies_abc():
    engine = YourEngine(context_length=200000)
    assert isinstance(engine, ContextEngine)
    assert engine.name == "your-name"

def test_compress_returns_valid_messages():
    engine = YourEngine(context_length=200000)
    msgs = [{"role": "user", "content": "hello"}]
    result = engine.compress(msgs)
    assert isinstance(result, list)
    assert all("role" in m for m in result)
```

Полный комплект тестов контракта ABC см. в разделе `tests/agent/test_context_engine.py`.

## Потокобезопасность

Когда `compression.context_timeout_seconds > 0` (по умолчанию), Hermes запускает
весь проход сжатия, включая `compress()` вашего двигателя и границу
обратные вызовы и `on_pre_compress` / любого поставщика памяти.
`on_session_switch` — в объединенном потоке демона с тайм-аутом на стороне хоста.
Поэтому ваш двигатель должен предполагать:

- Вызовы могут поступать в произвольный поток из пула. Не полагайтесь на нить
  состояние сходства или `threading.local`, доступное для обсуждения.
- Список сообщений, который вы получаете, представляет собой личный глубокий снимок; мутировав его в
  место разрешено (устаревший контракт), но мутация становится только видимой
  если проход фиксируется. После тайм-аута хоста ваша все еще работающая работа
  отброшено — никогда не публикуйте во внешнем/долговременном состоянии вне коммита.
- Проходы для *различных* сеансов могут выполняться одновременно на братьях и сестрах пула; а
  один экземпляр механизма/поставщика, общий для нескольких сеансов, должен быть потокобезопасным.

## См. также

- [Сжатие и кэширование контекста](/developer-guide/context-compression-and-caching) — как работает встроенный компрессор.
- [Плагины провайдера памяти](/developer-guide/memory-provider-plugin) — аналогичная система плагинов с одним выбором для памяти.
- [Плагины](/user-guide/features/plugins) — общий обзор системы плагинов.