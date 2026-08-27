---
sidebar_label: Build a Plugin
slug: /developer-guide/plugins
title: Создайте плагин Hermes
description: Пошаговое руководство по созданию полноценного плагина Hermes с инструментами,
  крючками, файлами данных и навыками.
---

# Создайте плагин Hermes

В этом руководстве описывается создание полноценного плагина Hermes с нуля. К концу у вас будет работающий плагин с множеством инструментов, крючками жизненного цикла, отправленными файлами данных и набором навыков — всем, что поддерживает система плагинов.

:::info Не знаете, какое руководство вам нужно?
Hermes имеет несколько различных подключаемых интерфейсов: некоторые используют API Python `register_*`, другие управляются конфигурацией или вставляются в каталоги. Сначала используйте эту карту:

| Если вы хотите добавить… | Читать |
|---|---|
| Пользовательские инструменты, перехватчики, команды с косой чертой, навыки или подкоманды CLI | **Это руководство** (общая информация о плагине) |
| Расширение **собственного настольного приложения** (панели, страницы, строка состояния, палитра, темы) | [SDK плагина для рабочего стола](/developer-guide/desktop-plugin-sdk) |
| Расширение **веб-панели** (вкладки, слоты оболочки, темы) | [Расширение информационной панели](/user-guide/features/extending-the-dashboard) |
| **Бэкэнд LLM/вывода** (новый поставщик) | [Плагины поставщика моделей](/developer-guide/model-provider-plugin) |
| **канал шлюза** (Discord/Telegram/IRC/Teams/и т. д.) | [Добавление адаптеров платформы](/developer-guide/добавление-платформы-адаптеров) |
| **Бэкэнд памяти** (Honcho/Mem0/Supermemory/и т. д.) | [Плагины поставщика памяти](/developer-guide/memory-provider-plugin) |
| **Механизм сжатия контекста** | [Плагины контекстного движка](/developer-guide/context-engine-plugin) |
| **Инструмент для создания изображений** | [Плагины поставщика изображений](/developer-guide/image-gen-provider-plugin) |
| **Бэкэнд для создания видео** | [Плагины поставщиков генерации видео](/developer-guide/video-gen-provider-plugin) |
| **Сервис веб-поиска/извлечения** | [Плагины поставщика веб-поиска](/developer-guide/web-search-provider-plugin) |
| **Облачный браузер** (поставщик сеансов CDP в стиле Browserbase) | [Плагины поставщика браузера](/developer-guide/browser-provider-plugin) |
| **Бэкэнд-менеджер секретов** (хранилище/менеджер паролей/хранилище ключей ОС) | [Плагины с секретным исходным кодом](/developer-guide/secret-source-plugin) |
| **Панель мониторинга OIDC/поставщик аутентификации** | [Веб-панель — пользовательские поставщики](/user-guide/features/web-dashboard#custom-providers) — `ctx.register_dashboard_auth_provider()` |
| **Бэкэнд TTS** (любой интерфейс командной строки — Piper, VoxCPM, Kokoro, клонирование голоса и т. д.) | [Поставщики пользовательских команд TTS](/user-guide/features/tts#custom-command-providers) — на основе конфигурации, Python не требуется |
| **Бэкэнд STT** (пользовательский шепот/ASR CLI) | [Транскрипция голосового сообщения](/user-guide/features/tts#voice-message-transcription-stt) — установите `HERMES_LOCAL_STT_COMMAND` в шаблон с токенами argv |
| **Внешние инструменты через MCP** (файловая система, GitHub, Linear, любой сервер MCP) | [MCP](/user-guide/features/mcp) — объявить `mcp_servers.<name>` в `config.yaml` |
| **Перехватчики событий шлюза** (срабатывание при запуске, события сеанса, команды) | [Перехватчики событий](/user-guide/features/hooks#gateway-event-hooks) — поместите `HOOK.yaml` + `handler.py` в `~/.hermes/hooks/<name>/` |
| **Перехватчики оболочки** (запуск команды оболочки при возникновении событий) | [Shell Hooks](/user-guide/features/hooks#shell-hooks) — объявить под `hooks:` в `config.yaml` |
| **Дополнительные источники навыков** (пользовательские репозитории GitHub, частные индексы навыков) | [Навыки](/user-guide/features/skills) — `hermes skills tap add <repo>` · [Публикация крана](/user-guide/features/skills#publishing-a-custom-skill-tap) |
| Первоклассный **основной** поставщик логических выводов (не плагин) | [Добавление поставщиков](/developer-guide/добавление-поставщиков) |

См. полную [таблицу подключаемых интерфейсов](/user-guide/features/plugins#pluggable-interfaces--where-to-go-for-each) для получения консолидированного представления каждой поверхности расширения, включая стили, управляемые конфигурацией (TTS, STT, MCP, перехватчики оболочки) и стили встраиваемых каталогов (перехватчики шлюза).
:::

:::caution Плагины сторонних продуктов поставляются отдельно, а не в основное дерево
Плагины, которые интегрируют **чужой продукт или проект** — серверные части наблюдения/метрики, коннекторы SaaS поставщиков, аналитические панели, вставки платных услуг — создаются и распространяются как **отдельные репозитории плагинов**, а не объединяются в `NousResearch/hermes-agent`. Пользователи устанавливают их в `~/.hermes/plugins/` или через точку входа pip; все в этом руководстве работает так же, как и в автономном репозитории. Это решение по подключению и обслуживанию (ядро движется быстро, и мы не владеем вашим сервером), а не планка качества — плагин может быть превосходным и при этом принадлежать к собственному репозиторию. Продвигайте его на канале Nous Research Discord `#plugins-skills-and-skins`. Политику см. в [CONTRIBUTING.md](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md).
:::

## Пакеты подключаемых модулей портативного агента v1

Hermes также может устанавливать и загружать пакеты каталогов, предназначенные для Агента.
Плагины формата v1.0.0. Это адаптер совместимости для портативного
компоненты, которыми уже владеет Hermes. Он не заменяет родной `plugin.yaml` plus.
`register(ctx)` плагинов.

```text
my-portable-plugin/
├── plugin.json
├── skills/
│   └── summarize/
│       ├── SKILL.md
│       └── references/
└── mcp.json
```

Установите и активируйте портативный пакет, выполнив обычный рабочий процесс:

```bash
hermes plugins install owner/repository --no-enable
hermes plugins list
hermes plugins enable <plugin-name>
```

Переносимые пакеты отключаются после установки, если вы явно не включите их.
их. Включенный пакет может предоставлять непосредственные каталоги `skills/*/SKILL.md`.
и серверы stdio MCP из корня `mcp.json`. Навыки доступны только для чтения, имеют пространство имен,
и загружен через `skills_list` плюс `skill_view`. Команды MCP передаются как
один исполняемый токен с отдельным списком аргументов, никогда не через оболочку.
Используйте `skills_list`, чтобы узнать полное название квалифицированного навыка. Портативный навык
пространства имен имеют детерминированную форму `agent-plugin-<slug>-<hash>`, производную
из обнаруженного ключа плагина, чтобы очищенные имена не могли конфликтовать.

Гермес проверяет `plugin.json`, заголовок «Навыки агента», фиксированный компонент
местоположения, `mcp.json`, разрешенные пути и локальное сдерживание символических ссылок. Это делает
не получать схемы JSON при загрузке пакета. Плохой навык или запись MCP
пропускается на своей границе, когда допустимые одноуровневые компоненты все еще могут загружаться.
`PLUGIN_ROOT` указывает на разрешенный корень пакета. `PLUGIN_DATA` указывает на
доступный для записи каталог в области профиля, управляемый Hermes.
Значения, объявленные в переносимом MCP `env`, являются видимыми данными пакета, а не секретом.
механизм хранения. Не размещайте учетные данные в `mcp.json`.

Текущее переносимое подмножество поддерживает записи stdio и Streamable HTTP MCP.
Портативные записи `streamable-http` маршрутизируются через существующие собственные файлы Hermes.
удаленный клиент MCP (та же среда выполнения, которая обеспечивает `mcp_servers` на основе URL-адреса).
config) с применением граничных правил версии 1: URL-адрес должен быть абсолютным.
http(s) без информации о пользователе или фрагмента, принимается только простой HTTP
для хостов `localhost`/loopback и настроенные заголовки никогда не пересылаются
через перенаправление между источниками. Устаревшие записи `sse` сообщаются и
пропущен. Плагины агента v1 не определяют доверие, разрешения, происхождение или
песочница. Включение пакета предоставляет его инструкциям и локальному исполняемому файлу
такая же позиция полного доверия, как и у других установленных плагинов Hermes.

[Визуализированная спецификация](https://agent-plugins.org/specification) на данный момент
помечает v1.0.0 как рабочий проект, а
[хранилище версионных спецификаций](https://github.com/agentplugins/agent-plugins-spec/blob/main/spec/1.0.0.md)
записывает его как Опубликовано. Поведение ключей Гермеса на канонической схеме v1.0.0
идентификаторы и нормативный текст, а не изменяемую метку статуса. Это
явно поддерживаемое подмножество, а не заявление о полном соответствии подключаемых модулей агента.

## Контракт совместимости собственного плагина

Собственные плагины `plugin.yaml` и `register(ctx)` защищены поведением,
не по одному глобальному номеру API плагина. Гермес не раскрывает
`PLUGIN_API_VERSION`, требуйте соответствие `api:` всего манифеста или прикрепите API
версию для несвязанных значений. Плагин, использующий документированное поведение, должен
продолжать работать после обычного обновления Hermes.

Правила совместимости:

- **Аддитивное развитие.** Документированные методы `PluginContext` не удаляются и не
  переименован. Новые параметры являются необязательными, имеют значения по умолчанию и должны быть
  только ключевые слова. Существующие поля возврата не удаляются и не перепечатываются автоматически.
- **Полезные данные перехватчика являются полезными данными ключевых слов.** Новые данные перехватчика добавляются как ключевое слово.
  поля, никогда не изменяя значение или положение существующего поля.
  Hermes проверяет подписи обратного вызова: устаревший обратный вызов получает поля, которые он
  объявляет, в то время как обратный вызов с `**kwargs` получает полный текущий
  полезная нагрузка. Новые плагины должны принимать `**kwargs`, чтобы они могли использовать дополнительные
  данные без другого изменения подписи.
- **Манифесты открыты для дополнений.** Неизвестные поля `plugin.yaml` игнорируются.
  Поэтому более старые версии Hermes могут загружать плагин, манифест которого содержит
  метаданные, представленные в более новой версии, при условии, что сам код плагина использует
  поддерживается поведение во время выполнения.
- **Интерфейсы поставщика расширяются за счет настроек по умолчанию.** Новые методы поставщика имеют
  реализация по умолчанию. Новый контекст обратного вызова не является обязательным и предназначен только для пересылки.
  когда проверка подписи показывает, что провайдер принимает ее. Добавление
  абстрактный метод или безусловно переданный аргумент требует
  окно миграции, а не изменение подписи в день флага.
- **Версия контракта, пересекающего границу.** Возможность может нести свою
  собственную версию схемы, когда она определяет полезную нагрузку или постоянный формат (для
  например, полезная нагрузка наблюдателя или состояние секретного источника). Сохранять поля аддитивными
  внутри этой локальной схемы. Сохраняемое состояние и конфигурация плагина должны оставаться
  читаемый или отправить явную миграцию; возобновленные сеансы, написанные старым
  формат все равно должен воспроизводиться. Не добавляйте литералы версии в несвязанный обратный вызов.
  или значения контекста.

### Политика устаревания

Задокументированное поведение собственного плагина может быть признано устаревшим только при наличии всех
следующее:

1. инструкции по замене и миграции в руководстве и релизе плагина
   заметки;
2. предупреждение, выдаваемое не более одного раза для каждого процесса, с указанием замены и
   самый ранний выпуск удаления;
3. поддержка старого поведения как минимум через два последующих незначительных
   релизы; и
4. Обеспечение совместимости на основе поведения как для устаревшего пути, так и для
   замена во всем этом окне.

Удаление после закрытия окна должно включать любую миграцию, необходимую для сохраненных данных.
или возобновляемые сеансы. На практике предпочтительны аддитивные псевдонимы и адаптеры.
к удалению.

Hermes обеспечивает соблюдение этого контракта, обнаружив замороженные внешние плагины
из изолированного `HERMES_HOME`. Эти тесты загружают и вызывают плагин через
`PluginManager`; они утверждают реальные результаты регистрации и обратного вызова, а не
чем внутренние списки символов или формы исходного кода.

## Что вы строите

Плагин **калькулятора** с двумя инструментами:
- `calculate` — оценивать математические выражения (`2**16`, `sqrt(144)`, `pi * 5**2`)
- `unit_convert` — конвертировать между единицами измерения (`100 F → 37.78 C`, `5 km → 3.11 mi`)

Плюс крючок, который регистрирует каждый вызов инструмента, и прилагаемый файл навыков.

## Шаг 1: Создайте каталог плагина

Создайте каталог и перейдите к шагу 2:

```bash
mkdir -p ~/.hermes/plugins/calculator
cd ~/.hermes/plugins/calculator
```

### Проверка с помощью Plugin Doctor

`hermes plugins doctor [path-or-id]` выполняет то же обнаружение каталогов,
синтаксический анализатор манифеста, импорт пространства имен, `register(ctx)`, реестр перехватчиков и инструмент
реестр, используемый самим Hermes. Он сообщает о неверных именах хуков и обратных вызовах, которые
не принимать `**kwargs`, сбои при регистрации и отклонение между заявленным и
зарегистрированные инструменты/крючки. Передайте `--ci` для выхода из ненулевого значения в случае ошибки:

```bash
hermes plugins doctor . --ci
```

Доктор использует временный `HERMES_HOME`, восстанавливает состояние регистрации плагина после
проверку и блокирует прямые соединения сокетов Python, чтобы отловить случайные
доступ к сети во время регистрации. Это не песочница: код плагина все еще
выполняется внутри процесса с разрешениями текущего пользователя и может порождать подпроцессы,
поэтому запускайте Doctor только для кода, которому вы доверяете достаточно для импорта.

## Шаг 2. Напишите манифест

Создайте `plugin.yaml`:

```yaml
name: calculator
version: 1.0.0
description: Math calculator — evaluate expressions and convert units
provides_tools:
  - calculate
  - unit_convert
provides_hooks:
  - post_tool_call
```

Это говорит Гермесу: «Я плагин под названием калькулятор, я предоставляю инструменты и крючки». Поля `provides_tools` и `provides_hooks` представляют собой списки того, что регистрирует плагин.

Необязательные поля, которые вы можете добавить:
```yaml
author: Your Name
requires_env:          # gate loading on env vars; prompted during install
  - SOME_API_KEY       # simple format — plugin disabled if missing
  - name: OTHER_KEY    # rich format — shows description/url during install
    description: "Key for the Other service"
    url: "https://other.com/keys"
    secret: true
capabilities:          # privileged host surfaces you request (consent flow)
  - tools.override     # replace built-in tools (needs user consent)
  - llm.model_override # choose the model for host-owned LLM calls
```

### Объявление возможностей

Если вашему плагину требуется привилегированная поверхность хоста — переопределив встроенный инструмент,
выбор модели для вызовов `ctx.llm` и т. д. — объявите ее в `capabilities:`.
Во время установки/включения пользователь видит список и дает согласие один раз; если позже
версия добавляет возможность, поток обновления снова запрашивает просто добавление.
Необъявленные или несогласованные возможности просто отключаются (закрываются при сбое), поэтому
**проверьте их перед использованием и корректно ухудшите**:

```python
def register(ctx):
    if ctx.has_capability("tools.override"):
        ctx.register_tool(..., override=True)
    else:
        ctx.register_tool(...)   # register under a non-conflicting name
```

Идентификаторы известных возможностей: `tools.override`, `llm.provider_override`,
`llm.model_override`, `llm.agent_id_override`, `llm.profile_override`,
`llm.task_override` (см. `hermes_cli/plugin_capabilities.py` для
канонический реестр). Неизвестные идентификаторы игнорируются. Более старые возможности
ключи конфигурации (`plugins.entries.<id>.allow_tool_override`, …) все еще работают, но
устарели — вместо этого объявляйте возможности, чтобы пользователи получали единый,
проверяемый экран согласия. Возможности — согласие + аудит, **не
песочница**: они закрывают поверхности API хоста, не более того.

**Плагины, распространяемые через Pip**, после установки не имеют каталога `plugin.yaml`,
поэтому вместо этого объявите возможности в метаданных распространения через компаньон
`hermes_agent.plugin_capabilities` группа точек входа. Каждая декларация
с именем `<plugin-id>.<capability-id>` и указывает на тот же объект, что и ваш
`hermes_agent.plugins` точка входа:

```toml
[project.entry-points."hermes_agent.plugins"]
calculator = "my_pkg:register"

[project.entry-points."hermes_agent.plugin_capabilities"]
"calculator.tools.override" = "my_pkg:register"
```

Hermes считывает их из установленных метаданных, не импортируя ваш код, поэтому
`hermes plugins capabilities` и поток согласия остаются точными для пункта
устанавливает.

### Ссылка на манифест v2

`plugin.yaml` также поддерживает дополнительную схему **v2** (#64165). Каждое поле
необязательно; манифест без `manifest_version` является манифестом версии 1 и остается
полностью поддерживается навсегда. Неизвестные поля никогда не прерывают загрузку — они игнорируются.
с предупреждением (прямая совместимость) и `manifest_version` новее, чем
это Гермес понимает, все еще грузится предупреждением.

| Поле | Тип | Значение |
|---|---|---|
| `manifest_version` | интервал | Манифест версии **формата файла**. Отсутствует = `1`. Текущий максимум: `2`. Независимо от `api_version`. |
| `api_version` | интервал | Во время выполнения **генерация API плагина**, на которую нацелен плагин (поверхность ctx/сигнатуры перехватчиков). Намеренно отдельная ось от `manifest_version` — плагин `api_version: 1` может использовать манифест v2. |
| `requires_plugins` | список | Зависимости между плагинами: `- id: other-plugin` с дополнительным `version_range: ">=1.0,<2"`. **Рекомендация**: при отсутствии зависимости регистрируется четкое предупреждение, но плагин все равно загружается — проверьте во время выполнения с помощью `ctx.has_plugin("other-plugin")`. Загрузка **order** учитывает эти ребра: когда A требует B, `register()` B выполняется перед A (топологическая сортировка, алфавитный тай-брейк; циклы предупреждают и возвращаются к алфавитному порядку). |
| `python_dependencies` | список ул | Заявленные требования к пунктам (например, `"requests>=2.0,<3"`). **Только шов декларации** — Hermes проверяет их, а `hermes plugins install` / `hermes plugins doctor` выявляет недостающие с подсказкой `pip install`, но Hermes **никогда не устанавливает** их автоматически. Закрепите верхние границы. |
| `config_schema` | картографирование | Описание ключей в формате JSON в `plugins.entries.<id>.settings`: `api_url: {type: str, default: "", description: "...", required: false}`. Проверено при загрузке; несоответствия регистрируют предупреждения, требующие принятия мер, с указанием ключа и ожидаемого типа — никогда не происходит сбоев загрузки. Типы: `str`, `int`, `float`, `bool`, `list`, `dict` (плюс псевдонимы схемы JSON). |
| `license` | ул | Идентификатор лицензии в стиле SPDX (например, `MIT`). |
| `homepage` | ул | URL-адрес проекта. |
| `tags` | список ул | Теги обнаружения произвольной формы (например, `[gateway, telegram]`). |

```yaml
# plugin.yaml — manifest v2 example
name: my-plugin
version: 1.2.0
manifest_version: 2
api_version: 1
license: MIT
homepage: https://github.com/owner/my-plugin
tags: [gateway, demo]
requires_plugins:
  - id: other-plugin
    version_range: ">=1.0,<2"
python_dependencies:
  - "somepkg>=1.0,<2"     # surfaced, never auto-installed
config_schema:
  api_url: {type: str, default: "", description: "Service endpoint"}
```

::: обратите внимание: изоляция зависимости от pip отложена
`python_dependencies` намеренно предназначен только для объявления и отображения. Установка
произвольные пакеты в общий венв Гермеса — это конфликт и цепочка поставок
поверхность, поэтому конструкция изоляции установочного шва (файл ограничений устанавливает
против блокировки хоста, против каталогов, предоставляемых для каждого плагина, и против обнаружения конфликтов
с отказом) является явно отсроченным последующим наблюдением — см. обзор второго раунда на
[#64165](https://github.com/NousResearch/hermes-agent/issues/64165) и
[#15220](https://github.com/NousResearch/hermes-agent/issues/15220). Плагин
пакеты (#64166) основаны на этих полях версии 2.
:::

## Шаг 3: Напишите схемы инструментов

Создайте `schemas.py` — это то, что читает LLM, чтобы решить, когда вызывать ваши инструменты:

```python
"""Tool schemas — what the LLM sees."""

CALCULATE = {
    "name": "calculate",
    "description": (
        "Evaluate a mathematical expression and return the result. "
        "Supports arithmetic (+, -, *, /, **), functions (sqrt, sin, cos, "
        "log, abs, round, floor, ceil), and constants (pi, e). "
        "Use this for any math the user asks about."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Math expression to evaluate (e.g., '2**10', 'sqrt(144)')",
            },
        },
        "required": ["expression"],
    },
}

UNIT_CONVERT = {
    "name": "unit_convert",
    "description": (
        "Convert a value between units. Supports length (m, km, mi, ft, in), "
        "weight (kg, lb, oz, g), temperature (C, F, K), data (B, KB, MB, GB, TB), "
        "and time (s, min, hr, day)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "value": {
                "type": "number",
                "description": "The numeric value to convert",
            },
            "from_unit": {
                "type": "string",
                "description": "Source unit (e.g., 'km', 'lb', 'F', 'GB')",
            },
            "to_unit": {
                "type": "string",
                "description": "Target unit (e.g., 'mi', 'kg', 'C', 'MB')",
            },
        },
        "required": ["value", "from_unit", "to_unit"],
    },
}
```

**Почему схемы важны.** Поле `description` позволяет LLM решить, когда использовать ваш инструмент. Будьте конкретны в том, что он делает и когда его использовать. `parameters` определяет, какие аргументы передает LLM.

## Шаг 4. Напишите обработчики инструментов

Создайте `tools.py` — это код, который фактически выполняется, когда LLM вызывает ваши инструменты:

```python
"""Tool handlers — the code that runs when the LLM calls each tool."""

import json
import math

# Safe globals for expression evaluation — no file/network access
_SAFE_MATH = {
    "abs": abs, "round": round, "min": min, "max": max,
    "pow": pow, "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "log": math.log, "log2": math.log2, "log10": math.log10,
    "floor": math.floor, "ceil": math.ceil,
    "pi": math.pi, "e": math.e,
    "factorial": math.factorial,
}


def calculate(args: dict, **kwargs) -> str:
    """Evaluate a math expression safely.

    Rules for handlers:
    1. Receive args (dict) — the parameters the LLM passed
    2. Do the work
    3. Return a JSON string — ALWAYS, even on error
    4. Accept **kwargs for forward compatibility
    """
    expression = args.get("expression", "").strip()
    if not expression:
        return json.dumps({"error": "No expression provided"})

    try:
        result = eval(expression, {"__builtins__": {}}, _SAFE_MATH)
        return json.dumps({"expression": expression, "result": result})
    except ZeroDivisionError:
        return json.dumps({"expression": expression, "error": "Division by zero"})
    except Exception as e:
        return json.dumps({"expression": expression, "error": f"Invalid: {e}"})


# Conversion tables — values are in base units
_LENGTH = {"m": 1, "km": 1000, "mi": 1609.34, "ft": 0.3048, "in": 0.0254, "cm": 0.01}
_WEIGHT = {"kg": 1, "g": 0.001, "lb": 0.453592, "oz": 0.0283495}
_DATA = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
_TIME = {"s": 1, "ms": 0.001, "min": 60, "hr": 3600, "day": 86400}


def _convert_temp(value, from_u, to_u):
    # Normalize to Celsius
    c = {"F": (value - 32) * 5/9, "K": value - 273.15}.get(from_u, value)
    # Convert to target
    return {"F": c * 9/5 + 32, "K": c + 273.15}.get(to_u, c)


def unit_convert(args: dict, **kwargs) -> str:
    """Convert between units."""
    value = args.get("value")
    from_unit = args.get("from_unit", "").strip()
    to_unit = args.get("to_unit", "").strip()

    if value is None or not from_unit or not to_unit:
        return json.dumps({"error": "Need value, from_unit, and to_unit"})

    try:
        # Temperature
        if from_unit.upper() in {"C","F","K"} and to_unit.upper() in {"C","F","K"}:
            result = _convert_temp(float(value), from_unit.upper(), to_unit.upper())
            return json.dumps({"input": f"{value} {from_unit}", "result": round(result, 4),
                             "output": f"{round(result, 4)} {to_unit}"})

        # Ratio-based conversions
        for table in (_LENGTH, _WEIGHT, _DATA, _TIME):
            lc = {k.lower(): v for k, v in table.items()}
            if from_unit.lower() in lc and to_unit.lower() in lc:
                result = float(value) * lc[from_unit.lower()] / lc[to_unit.lower()]
                return json.dumps({"input": f"{value} {from_unit}",
                                 "result": round(result, 6),
                                 "output": f"{round(result, 6)} {to_unit}"})

        return json.dumps({"error": f"Cannot convert {from_unit} → {to_unit}"})
    except Exception as e:
        return json.dumps({"error": f"Conversion failed: {e}"})
```

**Основные правила для кураторов:**
1. **Подпись:** `def my_handler(args: dict, **kwargs) -> str`
2. **Возврат:** Всегда строка JSON. И успех, и ошибка.
3. **Никогда не поднимайте:** Перехватывайте все исключения, вместо этого возвращайте ошибку в формате JSON.
4. **Принять `**kwargs`:** Гермес может передать дополнительный контекст в будущем.

## Шаг 5: Напишите регистрацию

Создайте `__init__.py` — это связывает схемы с обработчиками:

```python
"""Calculator plugin — registration."""

import logging

from . import schemas, tools

logger = logging.getLogger(__name__)

# Track tool usage via hooks
_call_log = []

def _on_post_tool_call(tool_name, args, result, task_id, **kwargs):
    """Hook: runs after every tool call (not just ours)."""
    _call_log.append({"tool": tool_name, "session": task_id})
    if len(_call_log) > 100:
        _call_log.pop(0)
    logger.debug("Tool called: %s (session %s)", tool_name, task_id)


def register(ctx):
    """Wire schemas to handlers and register hooks."""
    ctx.register_tool(name="calculate",    toolset="calculator",
                      schema=schemas.CALCULATE,    handler=tools.calculate)
    ctx.register_tool(name="unit_convert", toolset="calculator",
                      schema=schemas.UNIT_CONVERT, handler=tools.unit_convert)

    # This hook fires for ALL tool calls, not just ours
    ctx.register_hook("post_tool_call", _on_post_tool_call)
```

**Что делает `register()`:**
- Вызывается ровно один раз при запуске
- `ctx.register_tool()` помещает ваш инструмент в реестр — модель его сразу видит
- `ctx.register_hook()` подписывается на события жизненного цикла.
- `ctx.register_cli_command()` регистрирует подкоманду CLI (например, `hermes my-plugin <subcommand>`).
- `ctx.register_command()` регистрирует команду косой черты в сеансе (например, `/myplugin <args>` внутри чата CLI/шлюза) — см. [Зарегистрировать команды косой черты](#register-slash-commands) ниже
- `ctx.dispatch_tool(name, arguments)` — вызвать любой другой инструмент (встроенный или из другого плагина) с контекстом родительского агента (одобрения, учетные данные, Task_id), подключенным автоматически. Полезно для обработчиков косой черты, которым необходимо вызвать `terminal`, `read_file` или любой другой инструмент, как если бы модель вызывала его напрямую.
- `ctx.get_config()` / `ctx.set_config()` имеет доступ только к пространству имен настроек этого плагина; `ctx.state` хранит данные времени выполнения, принадлежащие плагину, в активном профиле.
- Если эта функция выходит из строя, плагин отключается, но Hermes продолжает работать нормально

**`dispatch_tool` пример — косая черта, запускающая инструмент:**

```python
def handle_scan(ctx, raw_args: str):
    """Implement /scan by invoking the terminal tool through the registry."""
    result = ctx.dispatch_tool("terminal", {"command": f"find . -name '{raw_args}'"})
    return result  # returned to the caller's chat UI

def register(ctx):
    # Handlers receive a single raw_args string; close over ctx via a lambda.
    ctx.register_command(
        "scan",
        lambda raw: handle_scan(ctx, raw),
        description="Find files matching a glob",
    )
```

Отправленный инструмент проходит через обычные конвейеры утверждения, редактирования и составления бюджета — это настоящий вызов инструмента, а не обходной путь к ним.

### Сохранение настроек и состояния времени выполнения

Используйте ключи конфигурации, относящиеся к плагину, для обеспечения видимости пользователю. Гермес решает их
под `plugins.entries.<plugin-id>.settings` и отклоняет глобальные, кросс-плагины,
и пути обхода:

```python
def register(ctx):
    endpoint = ctx.get_config("endpoint", default="https://example.invalid")
    retries = ctx.get_config("retry.attempts", default=3)

    ctx.set_config("endpoint", endpoint)
    ctx.set_config("retry.attempts", retries)
```

Вместо этого используйте `ctx.state` для принадлежащих плагину курсоров, кешей и данных дедупликации.
чем размещение бухгалтерского учета во время выполнения в `config.yaml`:

```python
def register(ctx):
    cursor = ctx.state.get("cursor", default={"page": 0})
    ctx.state.set("cursor", {"page": cursor["page"] + 1})
```

Состояние ограничено профилем, атомарно заменяется, безопасно для одновременных записей,
и ограничен 10 МБ на плагин. Портативные пакеты используют тот же каталог, что и
их `PLUGIN_DATA`; нативные плагины получают устойчивость к коллизиям,
Безопасное для Windows пространство имен. Неправильно сформированное существующее состояние сообщается и сохраняется.

Конфигурация и состояние имеют разных владельцев: настройки — это видимое пользователю поведение в
`config.yaml`, а состояние — это данные времени выполнения, принадлежащие плагину, в
`<HERMES_HOME>/plugin-data/`. Ни один из API не предоставляет пространство имен другого плагина.

## Шаг 6: Проверьте это

Запускаем Гермес:

```bash
hermes
```

В списке инструментов баннера вы должны увидеть `calculator: calculate, unit_convert`.

Попробуйте эти подсказки:
```
What's 2 to the power of 16?
Convert 100 fahrenheit to celsius
What's the square root of 2 times pi?
How many gigabytes is 1.5 terabytes?
```

Проверьте статус плагина:
```
/plugins
```

Выход:
```
Plugins (1):
  ✓ calculator v1.0.0 (2 tools, 1 hooks)
```

### Отладка обнаружения плагинов

Если ваш плагин не отображается — или отображается, но не загружается — установите `HERMES_PLUGINS_DEBUG=1`, чтобы получать подробные журналы обнаружения на stderr:

```bash
HERMES_PLUGINS_DEBUG=1 hermes plugins list
```

Вы увидите для каждого источника плагина (в комплекте, пользователя, проекта, точки входа):

- какие каталоги были просканированы и сколько манифестов каждый из них дал
- для каждого манифеста: разрешенный ключ, имя, тип, источник, путь на диске.
- причины пропуска: `disabled via config`, `not enabled in config`, `exclusive plugin`, `no plugin.yaml, depth cap reached`
- при загрузке: импортируемый плагин, а также однострочное описание того, что зарегистрировал `register(ctx)` (инструменты, перехватчики, команды слэша, команды CLI).
- при сбое синтаксического анализа: полная обратная трассировка исключения (ошибки сканера YAML и т. д.)
- при сбое `register()`: полная обратная трассировка, указывающая на строку в вашем `__init__.py`, которая вызвала

Одни и те же журналы всегда записываются в `~/.hermes/logs/agent.log` на уровне WARNING (только сбои) и уровне DEBUG (все), когда установлена переменная env. Поэтому, если вы не можете работать с env var (например, изнутри шлюза), вместо этого завершите файл журнала:

```bash
hermes logs --level WARNING | grep -i plugin
```

Распространенные причины, по которым плагин не отображается:

- **Не включено в конфигурации** — плагины включены. Запустите `hermes plugins enable <name>` (имя взято из выходных данных `plugins list`, которое может быть `<category>/<plugin>` для вложенных макетов).
- **Неправильная структура каталога:** Собственные пакеты используют `~/.hermes/plugins/<plugin-name>/plugin.yaml` (плоский) или один уровень категории. Портативные пакеты используют корень `plugin.json` в тех же местах. Все, что глубже, игнорируется.
- **Отсутствует `__init__.py`:** Собственным пакетам необходимы как `plugin.yaml`, так и `__init__.py` с функцией `register(ctx)`. Портативные пакеты не импортируют Python и не требуют `__init__.py`.
- **Неверный `kind`** — адаптерам шлюза в манифесте требуется `kind: platform`. Поставщики памяти автоматически определяются как `kind: exclusive` и направляются через конфигурацию `memory.provider` вместо `plugins.enabled`.

## Окончательная структура вашего плагина

```
~/.hermes/plugins/calculator/
├── plugin.yaml      # "I'm calculator, I provide tools and hooks"
├── __init__.py      # Wiring: schemas → handlers, register hooks
├── schemas.py       # What the LLM reads (descriptions + parameter specs)
└── tools.py         # What runs (calculate, unit_convert functions)
```

Четыре файла, четкое разделение:
- **Манифест** объявляет, что представляет собой плагин.
- **Схемы** описывают инструменты для LLM.
- **Обработчики** реализуют реальную логику.
- **Регистрация** объединяет все

## Что еще могут плагины?

### Файлы данных корабля

Поместите любые файлы в каталог вашего плагина и прочитайте их во время импорта:

```python
# In tools.py or __init__.py
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent
_DATA_FILE = _PLUGIN_DIR / "data" / "languages.yaml"

with open(_DATA_FILE) as f:
    _DATA = yaml.safe_load(f)
```

Это касается файлов, которые вы *отправляете*. Состояние, которое вы *пишите*, другое — см. следующее
раздел.

### Сохранение устойчивого состояния

Никогда не записывайте состояние времени выполнения в каталог вашего плагина: это установка.
дерево, а `hermes plugins update`/`remove` — вытащите или удалите его — ваш
данные пользователей умирают вместе с ним. Санкционированный дом — это корень данных каждого плагина,
который выдерживает оба и следует активному профилю:

```python
from plugins.plugin_storage import plugin_data_dir, plugin_db

# <hermes home>/plugin-data/<name>/ — created on first use
state_file = plugin_data_dir("my-plugin") / "state.json"

# Or a SQLite database at <data dir>/data.db (WAL mode, thread-friendly)
conn = plugin_db("my-plugin")
conn.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY)")
```

Один каталог для каждого плагина означает, что данные каждого плагина можно проверить в одном каталоге.
предсказуемое место. Секретам здесь не место — чтение учетных данных происходит
стандартный путь `.env`/secret-scope, как и везде.

### Набор навыков

Плагины могут отправлять файлы навыков, которые агент загружает через `skill_view("plugin:skill")`. Зарегистрируйте их в своем `__init__.py`:

```
~/.hermes/plugins/my-plugin/
├── __init__.py
├── plugin.yaml
└── skills/
    ├── my-workflow/
    │   └── SKILL.md
    └── my-checklist/
        └── SKILL.md
```

```python
from pathlib import Path

def register(ctx):
    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
```

Теперь агент может загружать ваши навыки, используя их имена в пространстве имен:

```python
skill_view("my-plugin:my-workflow")   # → plugin's version
skill_view("my-workflow")              # → built-in version (unchanged)
```

**Основные свойства:**
- Навыки плагина доступны **только для чтения** — они не вводятся в `~/.hermes/skills/` и не могут редактироваться через `skill_manage`.
- Навыки плагинов **не** перечислены в индексе `<available_skills>` системной подсказки — они являются явной загрузкой по согласию.
— Названия простых навыков не затрагиваются — пространство имен предотвращает конфликты со встроенными навыками.
- Когда агент загружает навык плагина, перед ним добавляется контекстный баннер пакета со списком родственных навыков из того же плагина.

:::совет Устаревший шаблон
Старый шаблон `shutil.copy2` (копирование навыка в `~/.hermes/skills/`) по-прежнему работает, но создает риск конфликта имен со встроенными навыками. Предпочитайте `ctx.register_skill()` для новых плагинов.
:::

### Вход в переменные среды

Если вашему плагину нужен ключ API:

```yaml
# plugin.yaml — simple format (backwards-compatible)
requires_env:
  - WEATHER_API_KEY
```

Если `WEATHER_API_KEY` не установлен, плагин отключается с четким сообщением. Ни сбоев, ни ошибок в агенте — просто «Плагин погоды отключен (отсутствует: WEATHER_API_KEY)».

Когда пользователи запускают `hermes plugins install`, им **интерактивно** предлагается указать недостающие переменные `requires_env`. Значения сохраняются в `.env` автоматически.

Для удобства установки используйте расширенный формат с описаниями и URL-адресами регистрации:

```yaml
# plugin.yaml — rich format
requires_env:
  - name: WEATHER_API_KEY
    description: "API key for OpenWeather"
    url: "https://openweathermap.org/api"
    secret: true
```

| Поле | Требуется | Описание |
|-------|----------|-------------|
| `name` | Да | Имя переменной среды |
| `description` | Нет | Показывается пользователю во время установки |
| `url` | Нет | Где получить удостоверение |
| `secret` | Нет | Если `true`, ввод скрыт (как поле пароля) |

Оба формата могут быть смешаны в одном списке. Уже установленные переменные пропускаются автоматически.

### Отложенная установка дополнительных зависимостей Python

Если ваш плагин включает в себя SDK, который будет установлен не у каждого пользователя (SDK поставщика, тяжелая библиотека ML, пакет для конкретной платформы), не добавляйте `import` в верхнюю часть модуля. Используйте помощник `tools.lazy_deps.ensure(...)` внутри обработчика инструмента — Hermes установит пакет при первом использовании, в зависимости от конфигурации пользователя `security.allow_lazy_installs`.

```python
# tools.py
from tools.lazy_deps import ensure, FeatureUnavailable

def my_tool_handler(args, **kwargs):
    try:
        ensure("my-plugin.my-backend")   # key must be in LAZY_DEPS
    except FeatureUnavailable as exc:
        return {"error": str(exc)}

    import my_backend_sdk   # safe now
    ...
```

Два правила из модели безопасности в `tools/lazy_deps.py`:

| Правило | Почему |
|---|---|
| Ваш функциональный ключ должен появиться в белом списке `LAZY_DEPS` в дереве | Не позволяет вредоносной конфигурации уговорить Hermes установить произвольные пакеты — допускаются только те спецификации, которые поставляет сама Hermes |
| Спецификации указаны только по имени PyPI | Никаких путей `--index-url`, `git+https://` или file:. Закрепите версии с PEP 440 (`"my-sdk>=1.2,<2"`) внутри записи белого списка |

Для сторонних плагинов, распространяемых через pip, объявите необязательные deps как `[project.optional-dependencies]` extras в своем собственном `pyproject.toml` и сообщите пользователям `pip install your-plugin[backend]` — этот путь не проходит через `lazy_deps`. Танец с ленивой установкой наиболее полезен для **связанных** плагинов, где доставка жесткой зависимости при каждой установке приведет к раздуванию базового пространства Hermes.

Когда `security.allow_lazy_installs: false` установлен глобально, `ensure()` немедленно вызывает `FeatureUnavailable` с подсказкой по исправлению — ваш плагин должен перехватить его и корректно деградировать (возвращать результат ошибки, а не завершать цикл инструмента).



### Потокобезопасные ленивые синглтоны

Плагины часто кэшируют дорогостоящий объект — клиент SDK, сеанс HTTP, пул соединений — в переменной уровня модуля, созданной при первом использовании:

```python
_client = None

def get_client():
    global _client
    if _client is not None:
        return _client
    _client = ExpensiveClient(...)   # ← TOCTOU race
    return _client
```

Это ножевой пистолет. Hermes запускает несколько потоков в одном процессе (делегированные вызовы инструментов, фоновые рабочие процессы, вилка самосовершенствования), поэтому два потока могут достичь `get_client()` до того, как будет установлен `_client`, **оба** проходят проверку `is not None`, **оба** запускают дорогостоящую сборку, а вторая запись блокирует первый — утечка любого ресурса, открытого проигравшим (соединение, дескриптор файла, фоновый поток).

Не поворачивайте замок вручную. Используйте помощники в `plugins/plugin_utils.py`:

```python
from plugins.plugin_utils import lazy_singleton, SingletonSlot

# Zero-arg accessor → decorate it:
@lazy_singleton
def get_client():
    return ExpensiveClient(load_config())   # runs exactly once

client = get_client()    # safe across threads
get_client.reset()       # drop the instance (tests / teardown)


# Accessor that takes a build argument → use a slot:
_slot: SingletonSlot = SingletonSlot()

def get_client(config=None):
    return _slot.get(lambda: ExpensiveClient(resolve(config)))

def reset_client():
    _slot.reset()
```

Оба сериализуют одновременные первые вызовы с двойной проверкой блокировки и запускают фабрику не более одного раза. Если фабрика поднимается, ничего не кэшируется и следующий вызов повторяется. Плагин памяти «honcho» (`plugins/memory/honcho/client.py`) является эталонным потребителем.

> Эмпирическое правило: каждый раз, когда вы пишете `global _something`, за которым следует проверка `is None` и сборка, вместо этого используйте один из них.



### Условная доступность инструмента

Для инструментов, которые зависят от дополнительных библиотек:

```python
ctx.register_tool(
    name="my_tool",
    schema={...},
    handler=my_handler,
    check_fn=lambda: _has_optional_lib(),  # False = tool hidden from model
)
```

### Переопределение встроенного инструмента

Чтобы заменить встроенный инструмент своей собственной реализацией (например, поменять местами
инструмент браузера по умолчанию для бэкэнда Chrome CDP или замените
`web_search` с пользовательским корпоративным индексом), передайте `override=True`:

```python
def register(ctx):
    ctx.register_tool(
        name="browser_navigate",             # same name as the built-in
        toolset="plugin_my_browser",         # your own toolset namespace
        schema={...},
        handler=my_custom_navigate,
        override=True,                       # explicit opt-in
    )
```

Без `override=True` реестр отклоняет любую регистрацию, которая могла бы
затенить существующий инструмент из другого набора инструментов — это предотвращает
случайные перезаписи. Дополнительное переопределение **встроенного** инструмента
требует от оператора согласия через
`plugins.entries.<plugin_id>.allow_tool_override: true` в `config.yaml`;
без этих ворот `register_tool(override=True)` поднимает ставку
`PluginToolOverrideError`. Переопределение регистрируется, поэтому оно
проверяемый в `~/.hermes/logs/agent.log`. Плагины загружаются после встроенных
инструменты, поэтому порядок регистрации правильный: ваш обработчик заменяет
встроенный.

**Плагинам, не входящим в комплект, также требуется разрешение оператора.** Для любого плагина, который
не поставляется с ядром Hermes (пользователь, проект или исходный код),
`override=True` для существующего встроенного инструмента дополнительно требует
Согласие на каждый плагин в `config.yaml`:

```yaml
plugins:
  entries:
    my-plugin:                    # the plugin's registry key from `hermes plugins list`
      allow_tool_override: true
```

Без гранта `ctx.register_tool(..., override=True)` поднимает
`PluginToolOverrideError`; поскольку исключения `register()` перехватываются
загрузчик, плагин отключается и Гермес продолжает работу. Ворота существуют
потому что включенный плагин, который молча заменяет привилегированный встроенный
например, `shell_exec` или `write_file` могут перехватывать все, что есть в модели
маршруты через него. Плагины в комплекте исключены: существует переопределение
решение сопровождающего. Если конфигурация не может быть загружена, ворота не закрываются.

Обычно вы никогда не редактируете этот ключ вручную. `hermes plugins enable <name>`
спрашивает, предоставить ли возможность при включении отдельного плагина
(по умолчанию нет) и `--allow-tool-override` /
Флаги `--no-allow-tool-override` пропускают запрос на установку по сценарию.
Этот же грант также обеспечивает доступ к `deregister()`: без него плагин не сможет
удалить инструмент, которым он не владеет (что в противном случае было бы способом обойти
переопределить проверку).

### Регистрация нескольких хуков

```python
def register(ctx):
    ctx.register_hook("pre_tool_call", before_any_tool)
    ctx.register_hook("post_tool_call", after_any_tool)
    ctx.register_hook("pre_llm_call", inject_memory)
    ctx.register_hook("on_session_start", on_new_session)
    ctx.register_hook("on_session_end", on_session_end)
```

### Ссылка на крючок

Каждый хук полностью описан в **[Справочнике по хукам событий](/user-guide/features/hooks#plugin-hooks)** — сигнатуры обратного вызова, таблицы параметров, когда именно срабатывает каждый из них, и примеры. Вот резюме:

| Крюк | Срабатывает, когда | Подпись обратного вызова | Возврат |
|------|-----------|-------------------|---------|
| [`pre_tool_call`](/user-guide/features/hooks#pre_tool_call) | Перед выполнением любого инструмента | `tool_name: str, args: dict, task_id: str` | необязательная директива: `{"action": "block", "message": ...}` накладывает вето на вызов; `{"action": "approve", "message": ...}` переходит к шлюзу одобрения человеком |
| [`post_tool_call`](/user-guide/features/hooks#post_tool_call) | После возврата любого инструмента | `tool_name: str, args: dict, result: str, task_id: str, duration_ms: int` | игнорируется |
| [`pre_llm_call`](/user-guide/features/hooks#pre_llm_call) | Один раз за ход, перед циклом вызова инструмента | `session_id: str, user_message: str, conversation_history: list, is_first_turn: bool, model: str, platform: str` | [внедрение контекста](#pre_llm_call-context-injection) |
| [`post_llm_call`](/user-guide/features/hooks#post_llm_call) | Один раз за ход, после цикла вызова инструмента (только успешные ходы) | `session_id: str, user_message: str, assistant_response: str, conversation_history: list, model: str, platform: str` | игнорируется |
| `pre_api_request` | Перед каждым запросом API необработанного провайдера (несколько за ход, когда модель вызывает инструменты) | `session_id: str, model: str, provider: str, base_url: str, api_mode: str, api_call_count: int, message_count: int, tool_count: int, approx_input_tokens: int, max_tokens: int, request: dict` | игнорируется |
| `post_api_request` | После каждого необработанного запроса API провайдера возвращается | `pre_api_request` полей плюс `api_duration: float, finish_reason: str, response_model: str \| None, usage: dict, response: dict, assistant_content_chars: int, assistant_tool_call_count: int` | игнорируется |
| `api_request_error` | Вызов API провайдера | поля корреляции плюс `status_code: int \| None, retry_count: int \| None, max_retries: int \| None, retryable: bool \| None, reason: str \| None, error: dict, request: dict` | игнорируется |
| [`on_session_start`](/user-guide/features/hooks#on_session_start) | Создана новая сессия (только первая очередь) | `session_id: str, model: str, platform: str` | игнорируется |
| [`on_session_end`](/user-guide/features/hooks#on_session_end) | Конец каждого вызова `run_conversation` + выход из CLI | `session_id: str, completed: bool, interrupted: bool, model: str, platform: str` | игнорируется |
| [`on_session_finalize`](/user-guide/features/hooks#on_session_finalize) | CLI/шлюз разрывает активный сеанс | `session_id: str \| None, platform: str` | игнорируется |
| [`on_session_reset`](/user-guide/features/hooks#on_session_reset) | Шлюз заменяет новый сеансовый ключ (`/new`, `/reset`) | `session_id: str, platform: str` | игнорируется |
| [`gateway_platform_event`](/user-guide/features/hooks#gateway_platform_event) | Авторизованное событие платформы нормализуется на границе шлюза (в настоящее время реакции Telegram) | `platform: str, event_type: str, payload: dict` | игнорируется |
| `kanban_task_claimed` | Заявлена ​​задача канбана (процесс диспетчера до появления работника) | `task_id: str, board: str \| None, assignee: str \| None, run_id: int \| None, profile_name: str` | игнорируется |
| `kanban_task_completed` | Задача канбана завершена (рабочий процесс) | `task_id, board, assignee, run_id, profile_name, summary: str \| None` | игнорируется |
| `kanban_task_blocked` | Задача канбана заблокирована (рабочий процесс) | `task_id, board, assignee, run_id, profile_name, reason: str \| None` | игнорируется |

Большинство перехватчиков являются наблюдателями по принципу «выстрелил и забыл» — их возвращаемые значения игнорируются. Исключениями являются `pre_llm_call`, который может добавлять контекст в разговор, и `pre_tool_call`, который может возвращать директиву блокировки/утверждения.

Все обратные вызовы должны принимать `**kwargs` для прямой совместимости. Если обратный вызов перехватчика дает сбой, он регистрируется и пропускается. Другие перехватчики и агент продолжают работать нормально.

Перехватчики жизненного цикла канбана срабатывают **после** фиксации изменений в базе данных доски, поэтому обратный вызов всегда видит устойчивое состояние и никогда не может удерживать блокировку записи SQLite. Поскольку работники канбана выполняются как отдельные подпроцессы `hermes -p <profile> chat -q`, `kanban_task_claimed` срабатывает в процессе **dispatcher**, а `kanban_task_completed` / `kanban_task_blocked` срабатывает в процессе **worker** — подключите диспетчер, чтобы централизованно наблюдать за каждым переходом, или рабочий процесс для контекста каждой задачи в сеансе.

**Перехватчики запросов API** являются наблюдателями за необработанным запросом поставщика, на один уровень ниже пары `pre_llm_call` / `post_llm_call` за ход: один ход, вызывающий инструменты, выполняет несколько запросов API, и эти перехватчики срабатывают вокруг каждого из них. Они существуют для плагинов наблюдения (трассировка, учет затрат, панели мониторинга задержек). Кварги `request` и `response` представляют собой очищенные JSON-представления полезной нагрузки поставщика с ограниченным размером (отредактированы конфиденциальные ключи, усечены длинные строки, нормализованы объекты SDK), а `usage` представляет собой простой словарь сводки токенов. Каждая полезная нагрузка содержит поля корреляции `turn_id`, `api_request_id`, `task_id`, `session_id` и `api_call_count`, поэтому плагин может объединять запросы, вызовы инструментов и повороты вместе. `api_request_error` срабатывает, когда вызов провайдера вызывает и добавляет `status_code`, `retry_count` / `max_retries`, `retryable`, `reason` и `error` dict с `type` и `message`.

### `pre_llm_call` внедрение контекста

Это единственный хук, возвращаемое значение которого имеет значение. Когда обратный вызов `pre_llm_call` возвращает dict с ключом `"context"` (или простую строку), Hermes вставляет этот текст в **сообщение пользователя текущего хода**. Это механизм для плагинов памяти, интеграции RAG, ограждений и любого плагина, который должен предоставить модели дополнительный контекст.

#### Формат возврата

```python
# Dict with context key
return {"context": "Recalled memories:\n- User prefers dark mode\n- Last project: hermes-agent"}

# Plain string (equivalent to the dict form above)
return "Recalled memories:\n- User prefers dark mode"

# Return None or don't return → no injection (observer-only)
return None
```

Любой непустой возврат, отличный от None, с ключом `"context"` (или простой непустой строкой) собирается и добавляется к сообщению пользователя для текущего хода.

#### Разлив слишком большого контекста

По умолчанию контекст каждого соединения ограничен `10,000` символами. Все, что находится выше шапки, записывается в `$HERMES_HOME/hook_outputs/<session_id>/<uuid>.txt` и заменяется предварительным просмотром головы и хвоста, а также сохраненным путем. Модель может прочитать весь контент через `read_file` или `terminal`, если ей это действительно необходимо. Это удерживает неконтролируемый плагин от раздувания подсказки каждого последующего хода и уничтожения префикса кэша подсказок. Подключайтесь к `config.yaml`:

```yaml
hooks:
  output_spill:
    enabled: true          # default: true
    max_chars: 10000       # default; set higher to opt out of spilling
    preview_head: 500      # chars shown at the top of the preview
    preview_tail: 500      # chars shown at the bottom of the preview
    # directory: null      # default: $HERMES_HOME/hook_outputs
```

#### Как работает инъекция

Внедренный контекст добавляется к **сообщению пользователя**, а не к системному приглашению. Это осознанный выбор дизайна:

- **Сохранение кэша подсказок** — системное приглашение остается одинаковым на протяжении всего хода. Anthropic и OpenRouter кэшируют префикс системного приглашения, поэтому поддержание его стабильности экономит более 75% входных токенов в многоходовых диалогах. Если бы плагины изменяли системное приглашение, каждый ход был бы промахом в кэше.
- **Эфемерный** — внедрение происходит только во время вызова API. Исходное сообщение пользователя в истории разговоров никогда не изменяется, и ничего не сохраняется в базе данных сеанса.
- **Системная подсказка является территорией Hermes** — она содержит рекомендации для конкретной модели, правила применения инструментов, персональные инструкции и содержимое кэшированных навыков. Плагины вносят контекст вместе с вводом пользователя, а не изменяют основные инструкции агента.

#### Пример: плагин вызова памяти

```python
"""Memory plugin — recalls relevant context from a vector store."""

import httpx

MEMORY_API = "https://your-memory-api.example.com"

def recall_context(session_id, user_message, is_first_turn, **kwargs):
    """Called before each LLM turn. Returns recalled memories."""
    try:
        resp = httpx.post(f"{MEMORY_API}/recall", json={
            "session_id": session_id,
            "query": user_message,
        }, timeout=3)
        memories = resp.json().get("results", [])
        if not memories:
            return None  # nothing to inject

        text = "Recalled context from previous sessions:\n"
        text += "\n".join(f"- {m['text']}" for m in memories)
        return {"context": text}
    except Exception:
        return None  # fail silently, don't break the agent

def register(ctx):
    ctx.register_hook("pre_llm_call", recall_context)
```

#### Пример: плагин Guardrails

```python
"""Guardrails plugin — enforces content policies."""

POLICY = """You MUST follow these content policies for this session:
- Never generate code that accesses the filesystem outside the working directory
- Always warn before executing destructive operations
- Refuse requests involving personal data extraction"""

def inject_guardrails(**kwargs):
    """Injects policy text into every turn."""
    return {"context": POLICY}

def register(ctx):
    ctx.register_hook("pre_llm_call", inject_guardrails)
```

#### Пример: перехват только для наблюдателя (без внедрения)

```python
"""Analytics plugin — tracks turn metadata without injecting context."""

import logging
logger = logging.getLogger(__name__)

def log_turn(session_id, user_message, model, is_first_turn, **kwargs):
    """Fires before each LLM call. Returns None — no context injected."""
    logger.info("Turn: session=%s model=%s first=%s msg_len=%d",
                session_id, model, is_first_turn, len(user_message or ""))
    # No return → no injection

def register(ctx):
    ctx.register_hook("pre_llm_call", log_turn)
```

#### Несколько плагинов, возвращающих контекст

Когда несколько плагинов возвращают контекст из `pre_llm_call`, их выходные данные объединяются двойными символами новой строки и вместе добавляются к сообщению пользователя. Порядок соответствует порядку обнаружения плагинов (в алфавитном порядке по имени каталога плагинов).

### Промежуточное программное обеспечение: измените то, что происходит

Хуки наблюдают за циклом агента (с несколькими задокументированными формами управления выше). **Промежуточное программное обеспечение меняет то, что происходит**: промежуточное программное обеспечение запроса переписывает эффективную полезную нагрузку до того, как что-либо нижестоящее увидит его, а промежуточное программное обеспечение выполнения оборачивает фактический вызов. Зарегистрируйте его из той же точки входа `register(ctx)`:

```python
def cap_find_output(tool_name, args, **kwargs):
    """Rewrite terminal find commands to cap their output."""
    command = args.get("command", "")
    if tool_name == "terminal" and command.startswith("find "):
        return {
            "args": {**args, "command": command + " | head -100"},
            "source": "my-plugin",
            "reason": "cap find output",
        }
    return None  # leave the call unchanged

def register(ctx):
    ctx.register_middleware("tool_request", cap_find_output)
```

Канонический список видов — `VALID_MIDDLEWARE` в `hermes_cli/middleware.py`:

| Вид | Получает | Договор возврата |
|------|----------|-----------------|
| `tool_request` | `tool_name`, `args`, `original_args`, кварги контекста | Верните `{"args": {...}}`, чтобы заменить действующие аргументы инструмента до того, как их увидят перехватчики, ограждения, утверждения и выполнение. Верните `None`, чтобы оставить вызов без изменений. |
| `llm_request` | `request`, `original_request`, кварги контекста | Верните `{"request": {...}}`, чтобы заменить действующие кварги поставщика перед их отправкой компанией Hermes. |
| `tool_execution` | полезная нагрузка плюс `next_call` | Завершает выполнение инструмента. Вызовите `next_call(payload)` ровно один раз, чтобы запустить нисходящую цепочку (или пропустить ее для короткого замыкания) и вернуть результат. |
| `llm_execution` | полезная нагрузка плюс `next_call` | Та же форма, обертывающая вызов провайдера. |

**Правила, важные на практике:**

- Цепочки промежуточного программного обеспечения запроса: каждый обратный вызов видит полезную нагрузку, переписанную предыдущими обратными вызовами, в то время как `original_args` / `original_request` всегда несет копию до промежуточного программного обеспечения. Полезные данные копируются между обратными вызовами, поэтому их можно свободно изменять.
– В возвращаемый словарь можно включить строки `source`, `reason` и `name`. Они попадают в трассировку промежуточного программного обеспечения, которую последующие перехватчики наблюдателей получают как `middleware_trace` kwarg.
- `next_call` в промежуточном программном обеспечении выполнения является **одноразовым**. Двойной вызов вызывает повышение, поскольку это приведет к повторному запуску поставщика или инструмента.
- Обратный вызов промежуточного программного обеспечения, вызывающий вызов, протоколируется и пропускается; цепочка продолжается. Ошибка нисходящего потока, возникшая после того, как ваш `next_call` распространяется как сам по себе. Промежуточное программное обеспечение никогда не сможет нарушить базовый путь выполнения.
- Полезные нагрузки промежуточного программного обеспечения содержат `middleware_schema_version` (`hermes.middleware.v1`) рядом с полями телеметрии наблюдателя.
- Неизвестные виды регистрируются с предупреждением, а не с ошибкой, поэтому плагин, написанный для более новой версии Hermes, по-прежнему загружается на более старой версии.

### Регистрация команд CLI

Плагины могут добавлять собственное дерево подкоманд `hermes <plugin>`:

```python
def _my_command(args):
    """Handler for hermes my-plugin <subcommand>."""
    sub = getattr(args, "my_command", None)
    if sub == "status":
        print("All good!")
    elif sub == "config":
        print("Current config: ...")
    else:
        print("Usage: hermes my-plugin <status|config>")

def _setup_argparse(subparser):
    """Build the argparse tree for hermes my-plugin."""
    subs = subparser.add_subparsers(dest="my_command")
    subs.add_parser("status", help="Show plugin status")
    subs.add_parser("config", help="Show plugin config")
    subparser.set_defaults(func=_my_command)

def register(ctx):
    ctx.register_tool(...)
    ctx.register_cli_command(
        name="my-plugin",
        help="Manage my plugin",
        setup_fn=_setup_argparse,
        handler_fn=_my_command,
    )
```

После регистрации пользователи могут запускать `hermes my-plugin status`, `hermes my-plugin config` и т. д.

**Плагины поставщика памяти** вместо этого используют подход, основанный на соглашениях: добавьте функцию `register_cli(subparser)` в файл `cli.py` вашего плагина. Система обнаружения плагинов памяти находит его автоматически — вызов `ctx.register_cli_command()` не требуется. Подробности см. в [Руководстве по подключаемым модулям Memory Provider](/developer-guide/memory-provider-plugin#adding-cli-commands).

**Ограничение активного поставщика.** Команды CLI подключаемого модуля памяти появляются только в том случае, если их поставщиком является активный `memory.provider` в конфигурации. Если пользователь не настроил вашего провайдера, ваши команды CLI не будут загромождать вывод справки.

### Регистрация косых команд

Плагины могут регистрировать внутрисессионные слэш-команды — команды, которые пользователи вводят во время разговора (например, `/lcm status` или `/ping`). Они работают как в CLI, так и в шлюзе (Telegram, Discord и т. д.).

```python
def _handle_status(raw_args: str) -> str:
    """Handler for /mystatus — called with everything after the command name."""
    if raw_args.strip() == "help":
        return "Usage: /mystatus [help|check]"
    return "Plugin status: all systems nominal"

def register(ctx):
    ctx.register_command(
        "mystatus",
        handler=_handle_status,
        description="Show plugin status",
    )
```

После регистрации пользователи могут ввести `/mystatus` в любом сеансе. Команда появляется в автозаполнении, выводе `/help` и меню бота Telegram.

**Подпись:** `ctx.register_command(name: str, handler: Callable, description: str = "", args_hint: str = "")`

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `name` | `str` | Имя команды без косой черты (например, `"lcm"`, `"mystatus"`) |
| `handler` | `Callable[[str], str \| None]` | Вызывается с необработанной строкой аргумента. Также может быть `async`. |
| `description` | `str` | Отображается в `/help`, автозаполнении и меню бота Telegram |

**Ключевые отличия от `register_cli_command()`:**

| | `register_command()` | `register_cli_command()` |
|---|---|---|
| Вызывается как | `/name` в сеансе | `hermes name` в терминале |
| Где это работает | Сеансы CLI, Telegram, Discord и т. д. | Только терминал |
| Обработчик получает | Необработанная строка аргументов | argparse `Namespace` |
| Вариант использования | Диагностика, статус, быстрые действия | Сложные деревья подкоманд, мастера настройки |

**Защита от конфликтов.** Если плагин пытается зарегистрировать имя, которое конфликтует со встроенной командой (`help`, `model`, `new` и т. д.), регистрация автоматически отклоняется с предупреждением журнала. Встроенные команды всегда имеют приоритет.

**Асинхронные обработчики.** Диспетчер шлюза автоматически обнаруживает и ожидает асинхронные обработчики, поэтому вы можете использовать как синхронизирующие, так и асинхронные функции:

```python
async def _handle_check(raw_args: str) -> str:
    result = await some_async_operation()
    return f"Check result: {result}"

def register(ctx):
    ctx.register_command("check", handler=_handle_check, description="Run async check")
```

### Инструменты отправки из слэш-команд

Обработчики команд слэша, которым необходимо управлять инструментами (создать субагент через `delegate_task`, вызвать `file_edit` и т. д.), должны использовать `ctx.dispatch_tool()` вместо обращения к внутренним компонентам платформы. Контекст родительского агента (подсказки рабочей области, счетчик, наследование модели) подключается автоматически.

```python
def register(ctx):
    def _handle_deliver(raw_args: str):
        result = ctx.dispatch_tool(
            "delegate_task",
            {
                "goal": raw_args,
                "toolsets": ["terminal", "file", "web"],
            },
        )
        return result

    ctx.register_command(
        "deliver",
        handler=_handle_deliver,
        description="Delegate a goal to a subagent",
    )
```

**Подпись:** `ctx.dispatch_tool(name: str, args: dict, *, parent_agent=None) -> str`

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `name` | `str` | Имя инструмента, зарегистрированное в реестре инструментов (например, `"delegate_task"`, `"file_edit"`) |
| `args` | `dict` | Аргументы инструмента, той же формы, которую отправит модель |
| `parent_agent` | `Agent \| None` | Необязательное переопределение. Если этот параметр опущен, разрешается из текущего агента CLI (или корректно ухудшается в режиме шлюза) |

**Поведение во время выполнения:**

- **Режим CLI:** `parent_agent` разрешается из активного агента CLI, поэтому подсказки рабочей области, счетчик и выбор модели наследуются, как и ожидалось.
- **Режим шлюза:** агент CLI отсутствует, поэтому инструменты постепенно деградируют — рабочая область считывается из настроенного рабочего каталога терминала, а счетчик не отображается.
- **Явное переопределение:** Если вызывающий объект передает `parent_agent=` явно, он учитывается и не перезаписывается.

Это общедоступный стабильный интерфейс для отправки инструментов из команд плагина. Плагины не должны проникать в `ctx._cli_ref.agent` или подобное частное состояние.

### Действовать изнутри хука (профиль + инструменты)

`ctx._cli_ref` заполняется только в сеансе **интерактивного интерфейса командной строки**. Это `None` в шлюзе, в неинтерактивных запусках `hermes chat -q` и в **рабочих сессиях, порожденных канбаном** — поэтому любая логика плагина, которая проходит через `_cli_ref`, молча отключается именно в этих контекстах. Два стабильных, независимых от сеанса API охватывают все, что действительно нужно хукам:

- **`ctx.profile_name`** — имя активного профиля (например, `"default"` или профиль уполномоченного в канбан-воркере). Произведено от `HERMES_HOME`, поэтому работает везде, без зависимости от `_cli_ref`.
- **`ctx.dispatch_tool(name, args)`** — вызвать любой зарегистрированный инструмент (встроенный или подключаемый), включая инструменты `kanban_*`, `delegate_task`, `terminal`, `read_file` и т. д. Работает с обратными вызовами перехватчика независимо от того, в каком процессе срабатывает перехватчик.

Вместе они позволяют хуку жизненного цикла канбана наблюдать за переходом и действовать на доске, не затрагивая внутренние компоненты структуры:

```python
def register(ctx):
    def on_blocked(*, task_id, reason=None, **kw):
        # Runs in the worker process; ctx._cli_ref is None here.
        ctx.dispatch_tool("kanban_comment", {
            "task_id": task_id,
            "comment": f"[{ctx.profile_name}] auto-noted block: {reason}",
        })
    ctx.register_hook("kanban_task_blocked", on_blocked)
```

Для запуска полного `hermes <subcommand>` (например, `hermes kanban show`) используйте инструмент `terminal` через `ctx.dispatch_tool("terminal", {"command": "hermes kanban show ..."})` — внутрипроцессного моста с косыми командами для сеансов безголовых рабочих процессов нет, а инструменты — это поддерживаемый способ вывести Hermes из-под контроля.

### Обработка щелчков кнопок комплекта слабых блоков

Плагины, которые публикуют сообщения Block Kit с интерактивными элементами (кнопками, дополнительными меню, устройствами выбора даты и т. д.), могут регистрировать обработчики кликов непосредственно с помощью адаптера Slack — никаких исправлений для `slack_bolt.AsyncApp` не требуется.

```python
def register(ctx):
    async def _on_approve(ack, body, action):
        # ack within 3 seconds — slack_bolt requirement.
        await ack()
        # body["channel"]["id"], body["user"]["id"], body["message"]["ts"]
        # action["action_id"], action["value"]
        sweep_id = (action.get("value") or "").split("|", 1)[-1]
        # ...do the deterministic work, then post a follow-up.

    ctx.register_slack_action_handler("inbox_sweep_approve", _on_approve)
```

**Подпись:** `ctx.register_slack_action_handler(action_id, callback) -> None`

| Параметр | Тип | Описание |
|-----------|------|-------------|
| `action_id` | `str \| re.Pattern \| dict` | Что бы ни принимал `slack_bolt.App.action()`: литерал `action_id`, скомпилированное регулярное выражение, соответствующее нескольким идентификаторам, или ограничивающий словарь, например `{"action_id": "...", "block_id": "..."}` |
| `callback` | асинхронный вызов | Получает `(ack, body, action)` согласно соглашению slack_bolt |

**Поведение во время выполнения:**

— Обработчик ставится в очередь во время загрузки плагина и подключается к `slack_bolt.AsyncApp` адаптера при подключении платформы Slack.
- Каждый обратный вызов защищен защитной оберткой: если ваш обработчик вызывает рейз, шлюз регистрирует ошибку и максимально подтверждает клик, чтобы Slack прекратил повторные попытки.
- Применяются стандартные правила slack_bolt — `await ack()` в течение 3 секунд, затем выполните более длительную работу.
- При развертывании с несколькими рабочими пространствами обработчик срабатывает при кликах из любого подключенного рабочего пространства; используйте `body["team"]["id"]`, если вам нужно определить поведение.

Это общедоступный способ участия плагинов в интерактивности Slack. Старые плагины могут исправлять `SlackAdapter.connect`; вместо этого предпочитайте этот API.

:::совет
В этом руководстве рассматриваются **общие плагины** (инструменты, хуки, косая черта, команды CLI). В разделах ниже представлен шаблон разработки для каждого специализированного типа плагина; каждая ссылка на полное руководство с практическими рекомендациями и примерами.
:::

## Специализированные типы плагинов

Помимо основной поверхности, у Hermes есть пять специализированных типов плагинов. Каждый из них поставляется в виде каталога `plugins/<category>/<name>/` (в комплекте) или `~/.hermes/plugins/<category>/<name>/` (пользователь). Контракт различается по категориям — выберите тот, который вам нужен, а затем прочитайте его полное руководство.

### Плагины поставщика моделей — добавьте серверную часть LLM

Перетащите профиль в `plugins/model-providers/<name>/`:

```python
# plugins/model-providers/acme/__init__.py
from providers import register_provider
from providers.base import ProviderProfile

register_provider(ProviderProfile(
    name="acme",
    aliases=("acme-inference",),
    display_name="Acme Inference",
    env_vars=("ACME_API_KEY", "ACME_BASE_URL"),
    base_url="https://api.acme.example.com/v1",
    auth_type="api_key",
    default_aux_model="acme-small-fast",
    fallback_models=("acme-large-v3", "acme-medium-v3"),
))
```

```yaml
# plugins/model-providers/acme/plugin.yaml
name: acme-provider
kind: model-provider
version: 1.0.0
description: Acme Inference — OpenAI-compatible direct API
```

Лениво обнаружил первый раз, когда что-либо вызывает `get_provider_profile()` или `list_providers()` — `auth.py`, `config.py`, `doctor.py`, `models.py`, `runtime_provider.py`, и chat_completions автоматически подключаются к нему. Пользовательские плагины переопределяют встроенные по имени.

**Полное руководство:** [Плагины поставщика модели](/developer-guide/model-provider-plugin) — ссылка на поля, переопределяемые перехватчики (`prepare_messages`, `build_extra_body`, `build_api_kwargs_extras`, `fetch_models`), выбор api_mode, типы аутентификации, тестирование.

### Плагины платформы — добавьте канал шлюза

Перетащите адаптер в `plugins/platforms/<name>/`:

```python
# plugins/platforms/myplatform/adapter.py
from gateway.platforms.base import BasePlatformAdapter

class MyPlatformAdapter(BasePlatformAdapter):
    async def connect(self): ...
    async def send(self, chat_id, text): ...
    async def disconnect(self): ...

def check_requirements():
    import os
    return bool(os.environ.get("MYPLATFORM_TOKEN"))

def _env_enablement():
    import os
    tok = os.getenv("MYPLATFORM_TOKEN", "").strip()
    if not tok:
        return None
    return {"token": tok}

def register(ctx):
    ctx.register_platform(
        name="myplatform",
        label="MyPlatform",
        adapter_factory=lambda cfg: MyPlatformAdapter(cfg),
        check_fn=check_requirements,
        required_env=["MYPLATFORM_TOKEN"],
        # Auto-populate PlatformConfig.extra from env so env-only setups
        # show up in `hermes gateway status` without SDK instantiation.
        env_enablement_fn=_env_enablement,
        # Opt in to cron delivery: `deliver=myplatform` routes to this var.
        cron_deliver_env_var="MYPLATFORM_HOME_CHANNEL",
        emoji="💬",
        platform_hint="You are chatting via MyPlatform. Keep responses concise.",
    )
```

```yaml
# plugins/platforms/myplatform/plugin.yaml
name: myplatform-platform
label: MyPlatform
kind: platform
version: 1.0.0
description: MyPlatform gateway adapter
requires_env:
  - name: MYPLATFORM_TOKEN
    description: "Bot token from the MyPlatform console"
    password: true
optional_env:
  - name: MYPLATFORM_HOME_CHANNEL
    description: "Default channel for cron delivery"
    password: false
```

**Полное руководство:** [Добавление адаптеров платформы](/developer-guide/adding-platform-adapters) — полный контракт `BasePlatformAdapter`, маршрутизация сообщений, шлюз аутентификации, интеграция с мастером настройки. Посмотрите `plugins/platforms/irc/` на рабочий пример только для stdlib.

### Плагины поставщика памяти — добавьте серверную часть межсессионных знаний

Перетащите реализацию `MemoryProvider` в `plugins/memory/<name>/`:

```python
# plugins/memory/my-memory/__init__.py
from agent.memory_provider import MemoryProvider

class MyMemoryProvider(MemoryProvider):
    @property
    def name(self) -> str:
        return "my-memory"

    def is_available(self) -> bool:
        import os
        return bool(os.environ.get("MY_MEMORY_API_KEY"))

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id

    def sync_turn(self, user_content, assistant_content, *,
                  session_id="", messages=None) -> None:
        ...

    def prefetch(self, query, *, session_id="") -> str:
        ...

    def get_tool_schemas(self) -> list[dict]:
        return []   # required @abstractmethod — see full guide

def register(ctx):
    ctx.register_memory_provider(MyMemoryProvider())
```

Поставщики памяти выбираются один раз — одновременно активен только один, выбираемый через `memory.provider` в `config.yaml`.

**Полное руководство:** [Плагины поставщика памяти](/developer-guide/memory-provider-plugin) — полное `MemoryProvider` ABC, контракт потоков, изоляция профиля, регистрация команд CLI через `cli.py`.

### Плагины контекстного движка — заменяют компрессор контекста

```python
# plugins/context_engine/my-engine/__init__.py
from agent.context_engine import ContextEngine

class MyContextEngine(ContextEngine):
    @property
    def name(self) -> str:
        return "my-engine"

    def update_from_response(self, usage) -> None: ...
    def should_compress(self, prompt_tokens: int = None) -> bool: ...
    def compress(self, messages, current_tokens=None, focus_topic=None,
                 force=False, memory_context="") -> list: ...

def register(ctx):
    ctx.register_context_engine(MyContextEngine())
```

Механизмы контекста имеют одиночный выбор — выбираются через `context.engine` в `config.yaml`.

**Полное руководство:** [Плагины контекстного движка](/developer-guide/context-engine-plugin).

### Серверы генерации изображений

Перетащите провайдера в `plugins/image_gen/<name>/`:

```python
# plugins/image_gen/my-imggen/__init__.py
from agent.image_gen_provider import ImageGenProvider

class MyImageGenProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "my-imggen"

    def is_available(self) -> bool: ...
    def generate(self, prompt: str, aspect_ratio="landscape", **kwargs) -> dict:
        # returns success_response(...) / error_response(...)
        ...

def register(ctx):
    ctx.register_image_gen_provider(MyImageGenProvider())
```

```yaml
# plugins/image_gen/my-imggen/plugin.yaml
name: my-imggen
kind: backend
version: 1.0.0
description: Custom image generation backend
```

**Полное руководство:** [Плагины поставщиков изображений](/developer-guide/image-gen-provider-plugin) — полные метаданные `ImageGenProvider` ABC, `list_models()` / `get_setup_schema()`, вспомогательные функции `success_response()`/`error_response()`, вывод base64 и URL, пользовательские переопределения, распределение пипсов.

**Справочные примеры:** `plugins/image_gen/openai/` (DALL-E/GPT-изображение через OpenAI SDK), `plugins/image_gen/openai-codex/`, `plugins/image_gen/xai/` (генерация изображения Grok).

## Поверхности расширения, отличные от Python

Hermes также принимает расширения, которые вообще не являются плагинами Python. Они показаны в [таблице подключаемых интерфейсов](/user-guide/features/plugins#pluggable-interfaces--where-to-go-for-each); в разделах ниже кратко описывается каждый авторский стиль.

### Серверы MCP — регистрация внешних инструментов

Серверы протокола контекста модели (MCP) регистрируют свои собственные инструменты в Hermes без какого-либо плагина Python. Объявите их в `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/projects"]
    timeout: 120

  linear:
    url: "https://mcp.linear.app/sse"
    auth:
      type: "oauth"
```

Hermes подключается к каждому серверу при запуске, выводит список его инструментов и регистрирует их вместе со встроенными модулями. LLM рассматривает их точно так же, как и любой другой инструмент. **Полное руководство:** [MCP](/user-guide/features/mcp).

### Перехватчики событий шлюза — срабатывание при событиях жизненного цикла

Поместите манифест + обработчик в `~/.hermes/hooks/<name>/`:

```yaml
# ~/.hermes/hooks/long-task-alert/HOOK.yaml
name: long-task-alert
description: Send a push notification when a long task finishes
events:
  - agent:end
```

```python
# ~/.hermes/hooks/long-task-alert/handler.py
async def handle(event_type: str, context: dict) -> None:
    if context.get("duration_seconds", 0) > 120:
        # send notification …
        pass
```

К событиям относятся `gateway:startup`, `session:start`, `session:end`, `session:reset`, `agent:start`, `agent:step`, `agent:end` и подстановочный знак `command:*`. Ошибки в хуках ловятся и протоколируются — они никогда не блокируют основной конвейер.

**Полное руководство:** [Перехватчики событий шлюза](/user-guide/features/hooks#gateway-event-hooks).

### Перехваты оболочки — запуск команды оболочки при вызове инструмента.

Если вы просто хотите запускать скрипт при запуске инструмента (уведомления, журналы аудита, оповещения на рабочем столе, автоформатирование), используйте перехватчики оболочки в `config.yaml` — Python не требуется:

```yaml
hooks:
  - event: post_tool_call
    command: "notify-send 'Tool ran: {tool_name}'"
    when:
      tools: [terminal, patch, write_file]
```

Поддерживает все те же события, что и перехватчики плагинов Python (`pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_start`, `on_session_end`, `pre_gateway_dispatch`), а также структурированный вывод JSON для решений о блокировке `pre_tool_call`.

**Полное руководство:** [Shell Hooks](/user-guide/features/hooks#shell-hooks).

### Источники навыков — добавьте собственный реестр навыков.

Если вы поддерживаете репозиторий навыков GitHub (или хотите использовать индекс сообщества за пределами встроенных источников), добавьте его как **tap**:

```bash
hermes skills tap add myorg/skills-repo
hermes skills search my-workflow --source myorg/skills-repo
hermes skills install myorg/skills-repo/my-workflow
```

Публикация собственного крана — это всего лишь репозиторий GitHub с каталогами `skills/<skill-name>/SKILL.md` — регистрация на сервере или в реестре не требуется.

**Полные руководства:** [Skills Hub](/user-guide/features/skills#skills-hub) · [Публикация пользовательского крана](/user-guide/features/skills#publishing-a-custom-skill-tap) (макет репозитория, минимальный пример, пути не по умолчанию, уровни доверия).

### TTS/STT через шаблоны команд

Любой интерфейс командной строки, который читает/записывает аудио или текст, можно подключить через `config.yaml` — без кода Python:

```yaml
tts:
  provider: voxcpm
  providers:
    voxcpm:
      type: command
      command: "voxcpm --ref ~/voice.wav --text-file {input_path} --out {output_path}"
      output_format: mp3
      voice_compatible: true
```

Для STT укажите `HERMES_LOCAL_STT_COMMAND` на шаблон с токенами argv. Он работает без неявной интерпретации оболочки; оберните его в `sh -c`, `cmd /c` или PowerShell явно, если доверенная локальная команда требует синтаксиса оболочки. Поддерживаемые заполнители: `{input_path}`, `{output_path}`, `{format}`, `{voice}`, `{model}`, `{speed}` (TTS); `{input_path}`, `{output_dir}`, `{language}`, `{model}` (СТТ). Любой интерфейс командной строки, взаимодействующий с путями, автоматически становится плагином.

**Полные руководства:** [Поставщики пользовательских команд TTS](/user-guide/features/tts#custom-command-providers) · [STT](/user-guide/features/tts#voice-message-transcription-stt).

## Распространение через pip

Чтобы предоставить общий доступ к плагинам, добавьте точку входа в свой пакет Python:

```toml
# pyproject.toml
[project.entry-points."hermes_agent.plugins"]
my-plugin = "my_plugin_package"
```

```bash
pip install hermes-plugin-calculator
# Plugin auto-discovered on next hermes startup
```

## Дистрибутив для NixOS

:::предупреждение Nix больше не поддерживается явно
Nix/NixOS больше не является явно поддерживаемым путем установки (только при максимально возможном уровне) — см. [Установка Nix](/getting-started/nix-setup). Этот раздел предназначен для пользователей, уже развертывающих NixOS.
:::

Пользователи NixOS могут установить ваш плагин декларативно, если вы предоставите `pyproject.toml` с точками входа:

**Плагины точки входа** (рекомендуются к распространению):
```nix
# User's configuration.nix
services.hermes-agent.extraPythonPackages = [
  (pkgs.python312Packages.buildPythonPackage {
    pname = "my-plugin";
    version = "1.0.0";
    src = pkgs.fetchFromGitHub {
      owner = "you";
      repo = "hermes-my-plugin";
      rev = "v1.0.0";
      hash = "sha256-...";  # nix-prefetch-url --unpack
    };
    format = "pyproject";
    build-system = [ pkgs.python312Packages.setuptools ];
  })
];
```

**Плагины каталогов** (`pyproject.toml` не требуется):
```nix
services.hermes-agent.extraPlugins = [
  (pkgs.fetchFromGitHub {
    owner = "you";
    repo = "hermes-my-plugin";
    rev = "v1.0.0";
    hash = "sha256-...";
  })
];
```

См. [Руководство по настройке Nix](/getting-started/nix-setup#plugins) для получения полной документации, включая использование наложения и проверку коллизий.

## Распространенные ошибки

**Обработчик не возвращает строку JSON:**
```python
# Wrong — returns a dict
def handler(args, **kwargs):
    return {"result": 42}

# Right — returns a JSON string
def handler(args, **kwargs):
    return json.dumps({"result": 42})
```

**В сигнатуре обработчика отсутствует `**kwargs`:**
```python
# Wrong — will break if Hermes passes extra context
def handler(args):
    ...

# Right
def handler(args, **kwargs):
    ...
```

**Обработчик вызывает исключения:**
```python
# Wrong — exception propagates, tool call fails
def handler(args, **kwargs):
    result = 1 / int(args["value"])  # ZeroDivisionError!
    return json.dumps({"result": result})

# Right — catch and return error JSON
def handler(args, **kwargs):
    try:
        result = 1 / int(args.get("value", 0))
        return json.dumps({"result": result})
    except Exception as e:
        return json.dumps({"error": str(e)})
```

**Описание схемы слишком расплывчато:**
```python
# Bad — model doesn't know when to use it
"description": "Does stuff"

# Good — model knows exactly when and how
"description": "Evaluate a mathematical expression. Use for arithmetic, trig, logarithms. Supports: +, -, *, /, **, sqrt, sin, cos, log, pi, e."
```