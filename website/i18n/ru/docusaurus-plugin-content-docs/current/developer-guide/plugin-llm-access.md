---
sidebar_position: 11
title: Доступ к плагину LLM
description: Запускайте любой вызов LLM изнутри плагина через ctx.llm — чат или структурированный,
  синхронный или асинхронный. Принадлежная хосту аутентификация, закрывающийся при
  сбое шлюз доверия, дополнительная проверка схемы JSON.
---

# Плагин LLM Access

`ctx.llm` — это поддерживаемый плагином способ выполнения вызова LLM.
Завершение чата, структурированное извлечение, синхронизация, асинхронность, с или без
изображения — та же поверхность, те же ворота доверия, те же учетные данные, принадлежащие хосту.

Плагины используют это, когда им нужно сделать что-то, требующее
модель, но не является частью разговора агента. Крючок, который
переписывает ошибку инструмента в то, что может прочитать неинженер. А
адаптер шлюза, который преобразует входящее сообщение перед постановкой в очередь
это. Косая черта, которая суммирует длинную вставку. Запланированное задание
который оценивает вчерашнюю активность и записывает одну строку в статус
доска. Предварительный фильтр, который решает, стоит ли будить сообщение.
агент вообще готов.

Это задания, над которыми агент не должен быть в курсе. Они хотят одного
Звонок в LLM, напечатанный ответ и все готово.

## Наименьший возможный вызов

```python
result = ctx.llm.complete(messages=[{"role": "user", "content": "ping"}])
return result.text
```

Вот и весь API в одной строке. Ни ключей, ни конфигурации провайдера, нет
Инициализация SDK. Плагин работает с любым поставщиком и
модель, которую пользователь использует в данный момент — когда он меняет провайдера,
Плагин следует за ними автоматически.

## Более полный пример чата

```python
result = ctx.llm.complete(
    messages=[
        {"role": "system", "content": "Rewrite errors as one short sentence a non-engineer can act on."},
        {"role": "user",   "content": traceback_text},
    ],
    max_tokens=64,
    purpose="hooks.error-rewrite",
)
return result.text
```

`purpose` — это строка аудита в свободной форме. Она отображается в `agent.log`.
и в `result.audit`, чтобы операторы могли видеть, какой плагин какой создал.
позвони. Необязательно, но рекомендуется для всего, что часто срабатывает.

## Структурированный вывод

Когда плагину нужен типизированный ответ, переключитесь на структурированный ряд:

```python
result = ctx.llm.complete_structured(
    instructions="Score this support reply for urgency (0–1) and pick a category.",
    input=[{"type": "text", "text": message_body}],
    json_schema=TRIAGE_SCHEMA,
    purpose="support.triage",
    temperature=0.0,
    max_tokens=128,
)

if result.parsed["urgency"] > 0.8:
    await dispatch_to_oncall(result.parsed["category"], message_body)
```

Хост запрашивает у провайдера выходные данные JSON и анализирует их локально.
в качестве запасного варианта проверяет соответствие вашей схеме, если `jsonschema`
установлен и возвращает объект Python на `result.parsed`. Если
модель не может создать действительный JSON, `result.parsed` — это `None` и
`result.text` несет необработанный ответ.

## Что дает тебе этот переулок

* **Один звонок, четыре фигуры.** `complete()` для чата,
  `complete_structured()` для введенного JSON, `acomplete()` и
  `acomplete_structured()` для asyncio. Те же аргументы, тот же результат
  объекты.
* **Учетные данные, принадлежащие хосту.** Токены OAuth, потоки обновления,
  пул учетных данных, дополнительные переопределения для каждой задачи — все учетные данные
  концепция Гермеса уже применяется. Плагин никогда не видит
  жетон; хост приписывает обратный вызов через `result.audit`.
* **Ограничено.** Одиночный синхронный или асинхронный вызов. Никакой потоковой передачи, никаких инструментов
  циклы, нет состояния диалога, которым можно было бы управлять. Укажите входные данные, получите
  результат, возврат.
* **Доверие, закрытое при отказе.** Плагин, который вы никогда не настраивали, не может
  выбрать собственного поставщика, модель, агента или сохраненные учетные данные.
  Позиция по умолчанию — «используйте то, что использует пользователь». Операторы соглашаются
  для конкретных переопределений для каждого плагина в `config.yaml`.

## Быстрый старт

Ниже представлены два полных плагина — один чат, другой структурированный. Оба корабля
внутри одной функции `register(ctx)` и снаружи нужен ноль
конфигурация для работы с любой активной моделью пользователя.

### Завершение чата — `/tldr`

```python
def register(ctx):
    ctx.register_command(
        name="tldr",
        handler=lambda raw: _tldr(ctx, raw),
        description="Summarise the supplied text in one paragraph.",
        args_hint="<text>",
    )


def _tldr(ctx, raw_args: str) -> str:
    text = raw_args.strip()
    if not text:
        return "Usage: /tldr <text to summarise>"
    result = ctx.llm.complete(
        messages=[
            {"role": "system",
             "content": "Summarise the user's text in one tight paragraph. No preamble."},
            {"role": "user", "content": text},
        ],
        max_tokens=256,
        temperature=0.3,
        purpose="tldr",
    )
    return result.text
```

`result.text` — ответ модели; `result.usage` несет токен
считает; `result.provider` и `result.model` содержат атрибуцию.

### Структурированное извлечение — `/paste-to-tasks`

```python
def register(ctx):
    ctx.register_command(
        name="paste-to-tasks",
        handler=lambda raw: _paste_to_tasks(ctx, raw),
        description="Turn freeform meeting notes into structured tasks.",
        args_hint="<text>",
    )


_TASKS_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner":  {"type": "string"},
                    "action": {"type": "string"},
                    "due":    {"type": "string", "description": "ISO date or empty"},
                },
                "required": ["action"],
            },
        },
    },
    "required": ["tasks"],
}


def _paste_to_tasks(ctx, raw_args: str) -> str:
    if not raw_args.strip():
        return "Usage: /paste-to-tasks <meeting notes>"
    result = ctx.llm.complete_structured(
        instructions=(
            "Extract concrete action items from these meeting notes. "
            "One task per actionable line. If no owner is named, leave 'owner' blank."
        ),
        input=[{"type": "text", "text": raw_args}],
        json_schema=_TASKS_SCHEMA,
        schema_name="meeting.tasks",
        purpose="paste-to-tasks",
        temperature=0.0,
        max_tokens=512,
    )
    if result.parsed is None:
        return f"Couldn't parse a response. Raw output:\n{result.text}"
    lines = [f"- [{t.get('owner') or '?'}] {t['action']}" for t in result.parsed["tasks"]]
    return "\n".join(lines) or "(no tasks found)"
```

Третий проработанный пример, на этот раз с вводом изображений, находится в
[`hermes-example-plugins`](https://github.com/NousResearch/hermes-example-plugins/tree/main/plugin-llm-example)
репо (сопутствующее репо для справочных плагинов — не входит в комплект поставки)
сам Гермес-агент). Для асинхронной поверхности (`acomplete()` /
`acomplete_structured()` с `asyncio.gather()`), см.
[`plugin-llm-async-example`](https://github.com/NousResearch/hermes-example-plugins/tree/main/plugin-llm-async-example)
в том же репо.

## Когда какой использовать

| Вы хотите… | Достичь |
|---|---|
| Текстовый ответ в свободной форме (перевод, резюме, переписывание, генерация) | `complete()` |
| Многоходовая подсказка (система + примеры из нескольких кадров + пользователь) | `complete()` |
| Напечатанный ответ, проверенный по схеме | `complete_structured()` |
| Ввод изображения или текста с напечатанным ответом | `complete_structured()` |
| Тот же вызов из асинхронного кода (адаптеры шлюза, асинхронные перехватчики) | `acomplete()` / `acomplete_structured()` |

Все остальное — выбор провайдера, разрешение модели, авторизация, резервный вариант,
таймаут, маршрутизация видения — одинаково во всех четырех.

## поверхность API

`ctx.llm` является экземпляром `agent.plugin_llm.PluginLlm`.

### `complete()`

```python
result = ctx.llm.complete(
    messages=[{"role": "user", "content": "Hi"}],
    provider=None,         # optional, gated — Hermes provider id (e.g. "openrouter")
    model=None,            # optional, gated — whatever string that provider expects
    temperature=None,
    max_tokens=None,
    timeout=None,          # seconds
    agent_id=None,         # optional, gated
    profile=None,          # optional, gated — explicit auth-profile name
    purpose="optional-audit-string",
    task=None,             # optional — a plugin-registered auxiliary slot
)
# → PluginLlmCompleteResult(text, provider, model, agent_id, usage, audit)
```

Простое завершение чата. `messages` — стандартная форма OpenAI.
список `{"role": "...", "content": "..."}` диктов. Многооборотный
подсказки (система + несколько пар пользователь/помощник + конечный пользователь) работают
точно так же, как и с OpenAI SDK.

`provider=` и `model=` независимы и имеют одинаковую форму.
в качестве основной конфигурации хоста (`model.provider` + `model.model`). Установить
просто `model=`, чтобы использовать активного провайдера пользователя с другим
модель на нем. Установите оба, чтобы полностью переключить провайдера. Любой аргумент
без согласия оператора поднимается на `PluginLlmTrustError`.

### `complete_structured()`

```python
result = ctx.llm.complete_structured(
    instructions="What you want extracted.",
    input=[
        {"type": "text",  "text": "..."},
        {"type": "image", "data": b"...", "mime_type": "image/png"},
        {"type": "image", "url":  "https://..."},
    ],
    json_schema={...},     # optional — triggers parsed result + validation
    json_mode=False,       # set True without a schema to ask for JSON anyway
    schema_name=None,      # optional human-readable schema name
    system_prompt=None,
    provider=None,         # optional, gated
    model=None,            # optional, gated
    temperature=None,
    max_tokens=None,
    timeout=None,
    agent_id=None,
    profile=None,
    purpose=None,
    task=None,             # optional — a plugin-registered auxiliary slot
)
# → PluginLlmStructuredResult(text, provider, model, agent_id,
#                             usage, parsed, content_type, audit)
```

Входные данные представляют собой текстовые блоки или блоки изображений (необработанные байты кодируются в формате Base64).
автоматически как URL-адрес `data:`). Когда `json_schema` или
`json_mode=True` предоставляется, хост запрашивает вывод JSON через
`response_format`, анализирует его локально как запасной вариант и проверяет
против вашей схемы, если установлен `jsonschema`.

* `result.content_type == "json"` — `result.parsed` — Python.
  объект, соответствующий вашей схеме.
* `result.content_type == "text"` — синтаксический анализ или проверка не удались;
  проверьте `result.text` на наличие необработанного ответа модели.

### Асинхронный

```python
result = await ctx.llm.acomplete(messages=..., task="classifier")
result = await ctx.llm.acomplete_structured(
    instructions=..., input=..., task="classifier"
)
```

Те же аргументы и типы результатов, что и у их аналогов синхронизации. Использование
они из адаптеров шлюза, асинхронных перехватчиков или любого кода плагина.
уже работает в цикле asyncio.

### Вспомогательные вызовы, маршрутизируемые задачами

Передайте `task=` в любую из четырех форм вызова, когда плагину это необходимо.
собственный настроенный вспомогательный маршрут. Зарегистрируйте эту задачу во время установки плагина;
настройки плагина по умолчанию применяются до тех пор, пока оператор не переопределит своего поставщика и модель
в `auxiliary.<task>`:

```python
def register(ctx):
    ctx.register_auxiliary_task(
        "classifier", display_name="Classifier", description="Classify input."
    )


result = ctx.llm.complete(messages=[...], task="classifier")
result = ctx.llm.complete_structured(instructions=..., input=..., task="classifier")
```

```yaml
auxiliary:
  classifier:
    provider: openrouter
    model: vendor/model-id
```

Плагины могут предоставлять настройки регистрации поставщика/модели по умолчанию для своих собственных.
задачи. Конфигурация оператора в `auxiliary.<task>` переопределяет эти
значения по умолчанию и управляет выбором развертывания. Плагин может использовать только задачу
он зарегистрировался; неизвестные или внешние имена задач завершаются сбоем раньше поставщика
призыв. `allow_task_override: true` — это явное разрешение оператора для
использование встроенных вспомогательных задач Hermes; он не разрешает использование другого плагина
задачи. Опустите `task=` (или используйте `"auto"`), чтобы сохранить активным основного поставщика/модель.

### Атрибуты результата

```python
@dataclass
class PluginLlmCompleteResult:
    text: str                    # the assistant's response
    provider: str                # e.g. "openrouter", "anthropic"
    model: str                   # whatever the provider returned for this call
    agent_id: str                # whose model/auth was used
    usage: PluginLlmUsage        # tokens + cache + cost estimate
    audit: Dict[str, Any]        # plugin_id, purpose, profile

@dataclass
class PluginLlmStructuredResult:
    # same fields as PluginLlmCompleteResult, plus:
    parsed: Optional[Any]        # JSON object when content_type == "json"
    content_type: str            # "json" or "text"
    # audit also carries schema_name when supplied
```

`usage` несет `input_tokens`, `output_tokens`, `total_tokens`,
`cache_read_tokens`, `cache_write_tokens` и `cost_usd`, когда
поставщик возвращает эти поля.

## Ворота доверия

Поведение по умолчанию — закрытие при отказе. Без `plugins.entries`
config, плагин может:

* запустите любой из четырех методов против активного провайдера пользователя
  и модель,
* установить аргументы формирования запроса (`temperature`, `max_tokens`,
  `timeout`, `system_prompt`, `purpose`, `messages`, `instructions`,
  `input`, `json_schema`),

… и все. `provider=`, `model=`, `agent_id=` и `profile=`
аргументы вызывают `PluginLlmTrustError` до тех пор, пока оператор не согласится.
Аналогично, `task=` может использовать только зарегистрированную вспомогательную задачу плагина.
если только оператор не предоставит `allow_task_override` для встроенной задачи.

**Большинству плагинов этот раздел никогда не нужен.** Плагин, который просто вызывает
`ctx.llm.complete(messages=...)` без переопределений работает против
все, что у пользователя активно и работает без конфигурации. Блок ниже
актуально только тогда, когда плагин специально хочет закрепить
другая модель или поставщик, чем у пользователя.

```yaml
plugins:
  entries:
    my-plugin:
      llm:
        # Allow this plugin to choose a different Hermes provider
        # (must be one Hermes already knows about — same names as
        # `hermes model` and config.yaml model.provider).
        allow_provider_override: true

        # Optionally restrict which providers. Use ["*"] for any.
        allowed_providers:
          - openrouter
          - anthropic

        # Allow this plugin to ask for a specific model.
        allow_model_override: true

        # Optionally restrict which models. Use ["*"] for any.
        # Models are matched literally against whatever string the
        # plugin sends — Hermes does not look anything up.
        allowed_models:
          - openai/gpt-4o-mini
          - anthropic/claude-3-5-haiku

        # Allow cross-agent calls (rare).
        allow_agent_id_override: false

        # Allow the plugin to request a specific stored auth profile
        # (e.g. a different OAuth account on the same provider).
        allow_profile_override: false
```

Идентификатор плагина — это поле манифеста `name:` для плоских плагинов или поле
ключ на основе пути для вложенных плагинов (`image_gen/openai`,
`memory/honcho` и т. д.).

### Что контролируют ворота

| Переопределить | По умолчанию | Конфигурационный ключ |
| --------------- | ------- | -------------------------------- |
| `provider=` | отрицается | `allow_provider_override: true` |
| ↳ белый список | — | `allowed_providers: [...]` |
| `model=` | отрицается | `allow_model_override: true` |
| ↳ белый список | — | `allowed_models: [...]` |
| `agent_id=` | отрицается | `allow_agent_id_override: true` |
| `profile=` | отрицается | `allow_profile_override: true` |
| встроенный `task=` | отрицается | `allow_task_override: true` |

Каждое переопределение регулируется независимо. Предоставление `allow_model_override`
**не** также предоставляет `allow_provider_override` — доверенный плагин
выбор модели по-прежнему привязан к активному поставщику пользователя, если только
он также получает шлюз провайдера.

### Что воротам НЕ нужно обеспечивать

* Аргументы формирования запроса — `temperature`, `max_tokens`,
  `timeout`, `system_prompt`, `purpose`, `messages`, `instructions`,
  `input`, `json_schema`, `schema_name`, `json_mode` — всегда
  разрешено; они не выбирают учетные данные или маршруты.
* Положение запрета по умолчанию означает, что ненастроенный плагин все еще может делать
  полезная работа — она просто работает с активным поставщиком и моделью.
  Операторам нужно подумать только о `plugins.entries` для плагинов.
  которым нужна более тонкая маршрутизация.

## Что принадлежит хосту

Полный список того, что `ctx.llm` делает для плагина, чтобы вы
не обязательно:

* **Решение поставщика.** Читает `model.provider` + `model.model`.
  из конфигурации пользователя (или явных переопределений, если им доверяют).
* **Auth.** Извлекает ключи API, токены OAuth или токены обновления из
  `~/.hermes/auth.json` /env, включая пул учетных данных, когда
  один настроен. Плагин их никогда не видит.
* **Маршрутизация изображения.** При вводе изображения и пользовательском
  активная текстовая модель является только текстовой, хост возвращается к
  автоматически настраивается модель видения.
* **Резервная цепочка.** Если основной провайдер пользователя 5xxs или 429s,
  запрос проходит через обычный резервный вариант Hermes с поддержкой агрегатора
  прежде чем он вернет ошибку плагину.
* **Тайм-аут.** Учитывает ваш аргумент `timeout=` и возвращается к
  `auxiliary.<task>.timeout` config или глобальное дополнительное значение по умолчанию.
* **Формирование JSON.** Отправляет `response_format` провайдеру, когда
  вы запрашиваете JSON, а затем повторно анализируете локально из изолированного кода
  ответ, если поставщик вернул его.
* **Проверка схемы.** Проверяется по вашему `json_schema`, когда
  `jsonschema` установлен; записывает строку отладки и пропускает строгий
  проверка в противном случае.
* **Журнал аудита.** Каждый вызов записывает одну строку INFO в `agent.log` с
  идентификатор плагина, поставщик/модель, цель и общее количество токенов.

## Чем владеет плагин

* **Форма запроса.** `messages` для чата, `instructions` + `input`
  для структурированного. Плагин создает приглашение; хост управляет им.
* **Схема.** Верните любую форму. Хозяин не делает вывод
  это для тебя.
* **Обработка ошибок.** `complete_structured()` вызывает `ValueError` при
  пустые входные данные и при сбое проверки схемы. `PluginLlmTrustError`
  срабатывает, когда шлюз доверия отказывает в переопределении. Что-нибудь еще
  (поставщик 5xx, учетные данные не настроены, тайм-аут) вызывает что-либо
  `auxiliary_client.call_llm()` повышает ставку.
* **Стоимость.** Каждый звонок осуществляется платным поставщиком услуг пользователя. Не надо
  зацикливаться на `complete()` для каждого сообщения шлюза, не задумываясь
  о расходах токенов.

## Где это находится на поверхности плагина

Существующие методы `ctx.*` расширяют существующую подсистему Hermes:

| `ctx.register_tool` | добавляет инструмент, который может вызвать агент |
| `ctx.register_platform` | подключает новый адаптер шлюза |
| `ctx.register_image_gen_provider` | заменяет серверную часть генерации изображений |
| `ctx.register_memory_provider` | заменяет серверную часть памяти |
| `ctx.register_context_engine` | заменяет компрессор контекста |
| `ctx.register_hook` | наблюдает за событием жизненного цикла |

`ctx.llm` — это первая поверхность, позволяющая плагину работать одинаково
модель, с которой разговаривает пользователь, *вне диапазона*, без каких-либо
выше. Это его единственная работа. Если вашему плагину необходимо зарегистрировать
инструмент, который вызывает агент, используйте `register_tool`. Если ему нужно отреагировать
для события жизненного цикла используйте `register_hook`. Если ему необходимо сделать это
собственный вызов модели — по любой причине, структурированной или нет — `ctx.llm`.

## Ссылка

* Реализация: [`agent/plugin_llm.py`](https://github.com/NousResearch/hermes-agent/blob/main/agent/plugin_llm.py).
* Тесты: [`tests/agent/test_plugin_llm.py`](https://github.com/NousResearch/hermes-agent/blob/main/tests/agent/test_plugin_llm.py)
* Справочные плагины (сопутствующий репозиторий):
  * [`plugin-llm-example`](https://github.com/NousResearch/hermes-example-plugins/tree/main/plugin-llm-example) — синхронизировать структурированное извлечение с вводом изображения.
  * [`plugin-llm-async-example`](https://github.com/NousResearch/hermes-example-plugins/tree/main/plugin-llm-async-example) — асинхронно с `asyncio.gather()`
* Вспомогательный клиент (двигатель под капотом): см.
  [Среда выполнения поставщика](/developer-guide/provider-runtime).