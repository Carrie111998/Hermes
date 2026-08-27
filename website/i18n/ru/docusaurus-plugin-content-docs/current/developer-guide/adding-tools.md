---
sidebar_position: 2
title: Добавление инструментов
description: Как добавить новый инструмент в Hermes Agent — схемы, обработчики, регистрация
  и наборы инструментов
---

# Добавление инструментов

Прежде чем писать инструмент, спросите себя: **должен ли это быть [skill](creating-skills.md)?**

:::warning Только встроенные основные инструменты
Эта страница предназначена для добавления **встроенного инструмента Hermes** в сам репозиторий.
Если вам нужен личный, локальный для проекта или другой специальный инструмент без
модифицируя ядро Hermes, вместо этого используйте маршрут плагина:

- [Плагины](/руководство пользователя/функции/плагины)
- [Создать плагин Hermes](/developer-guide/plugins)

По умолчанию используются плагины для создания большинства пользовательских инструментов. Следуйте этой странице только тогда, когда
вы явно хотите выпустить новый встроенный инструмент в `tools/` и `toolsets.py`.
:::

Сделайте это **навыком**, когда способность может быть выражена в виде инструкций + команд оболочки + существующих инструментов (поиск в arXiv, рабочие процессы git, управление Docker, обработка PDF).

Превратите его в **инструмент**, когда требуется сквозная интеграция с ключами API, пользовательской логикой обработки, обработкой двоичных данных или потоковой передачей (автоматизация браузера, TTS, анализ изображения).

## Обзор

Добавление инструмента затрагивает **2 файла**:

1. **`tools/your_tool.py`** — обработчик, схема, функция проверки, вызов `registry.register()`
2. **`toolsets.py`** — добавьте имя инструмента в `_HERMES_CORE_TOOLS` (или конкретный набор инструментов)

Любой файл `tools/*.py` с вызовом `registry.register()` верхнего уровня обнаруживается автоматически при запуске — список импорта вручную не требуется.

## Шаг 1. Создайте встроенный файл инструмента

Каждый файл инструмента имеет одну и ту же структуру:

```python
# tools/weather_tool.py
"""Weather Tool -- look up current weather for a location."""

import json
import os
import logging

logger = logging.getLogger(__name__)


# --- Availability check ---

def check_weather_requirements() -> bool:
    """Return True if the tool's dependencies are available."""
    return bool(os.getenv("WEATHER_API_KEY"))


# --- Handler ---

def weather_tool(location: str, units: str = "metric") -> str:
    """Fetch weather for a location. Returns JSON string."""
    api_key = os.getenv("WEATHER_API_KEY")
    if not api_key:
        return json.dumps({"error": "WEATHER_API_KEY not configured"})
    try:
        # ... call weather API ...
        return json.dumps({"location": location, "temp": 22, "units": units})
    except Exception as e:
        return json.dumps({"error": str(e)})


# --- Schema ---

WEATHER_SCHEMA = {
    "name": "weather",
    "description": "Get current weather for a location.",
    "parameters": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "City name or coordinates (e.g. 'London' or '51.5,-0.1')"
            },
            "units": {
                "type": "string",
                "enum": ["metric", "imperial"],
                "description": "Temperature units (default: metric)",
                "default": "metric"
            }
        },
        "required": ["location"]
    }
}


# --- Registration ---

from tools.registry import registry

registry.register(
    name="weather",
    toolset="weather",
    schema=WEATHER_SCHEMA,
    handler=lambda args, **kw: weather_tool(
        location=args.get("location", ""),
        units=args.get("units", "metric")),
    check_fn=check_weather_requirements,
    requires_env=["WEATHER_API_KEY"],
)
```

### Ключевые правила

:::опасность Важно
– Обработчики **ДОЛЖНЫ** возвращать строку JSON (через `json.dumps()`), а не необработанные диктовки.
– Ошибки **ДОЛЖНЫ** возвращаться как `{"error": "message"}` и никогда не вызываться как исключения.
- `check_fn` вызывается при создании определений инструментов — если он возвращает `False`, инструмент автоматически исключается.
- `handler` получает `(args: dict, **kwargs)`, где `args` — аргументы вызова инструмента LLM.
:::

## Шаг 2. Добавьте встроенный инструмент в набор инструментов

В `toolsets.py` добавьте имя инструмента:

```python
# If it should be available on all platforms (CLI + messaging):
_HERMES_CORE_TOOLS = [
    ...
    "weather",  # <-- add here
]

# Or create a new standalone toolset:
"weather": {
    "description": "Weather lookup tools",
    "tools": ["weather"],
    "includes": []
},
```

## ~~Шаг 3. Добавьте импорт Discovery~~ (больше не требуется)

Модули инструментов с вызовом `registry.register()` верхнего уровня автоматически обнаруживаются `discover_builtin_tools()` в `tools/registry.py`. Не нужно поддерживать список импорта вручную — просто создайте файл в `tools/`, и он будет выбран при запуске.

## Асинхронные обработчики

Если вашему обработчику нужен асинхронный код, отметьте его `is_async=True`:

```python
async def weather_tool_async(location: str) -> str:
    async with aiohttp.ClientSession() as session:
        ...
    return json.dumps(result)

registry.register(
    name="weather",
    toolset="weather",
    schema=WEATHER_SCHEMA,
    handler=lambda args, **kw: weather_tool_async(args.get("location", "")),
    check_fn=check_weather_requirements,
    is_async=True,  # registry calls _run_async() automatically
)
```

Реестр прозрачно обрабатывает асинхронное мостовое соединение — вы никогда не вызываете `asyncio.run()` самостоятельно.

## Обработчики, которым нужен идентификатор Task_id

Инструменты, управляющие состоянием каждого сеанса, получают `task_id` через `**kwargs`:

```python
def _handle_weather(args, **kw):
    task_id = kw.get("task_id")
    return weather_tool(args.get("location", ""), task_id=task_id)

registry.register(
    name="weather",
    ...
    handler=_handle_weather,
)
```

## Инструменты, перехваченные циклом агента

Некоторым инструментам (`todo`, `memory`, `session_search`, `delegate_task`) требуется доступ к состоянию агента по сеансам. Они перехватываются `run_agent.py` до того, как попадают в реестр. В реестре по-прежнему хранятся их схемы, но `dispatch()` возвращает резервную ошибку, если перехват обойден.

## Необязательно: интеграция с мастером настройки

Если вашему инструменту требуется ключ API, добавьте его в `hermes_cli/config.py`:

```python
OPTIONAL_ENV_VARS = {
    ...
    "WEATHER_API_KEY": {
        "description": "Weather API key for weather lookup",
        "prompt": "Weather API key",
        "url": "https://weatherapi.com/",
        "tools": ["weather"],
        "password": True,
    },
}
```

## Контрольный список

- [ ] Файл инструмента, созданный с обработчиком, схемой, функцией проверки и регистрацией.
- [ ] Добавлено в соответствующий набор инструментов в `toolsets.py`.
- [ ] Подтверждено, что это действительно должен быть встроенный/основной инструмент, а не плагин.
- [ ] Обработчик возвращает строки JSON, ошибки возвращаются как `{"error": "..."}`
- [ ] Необязательно: ключ API добавлен в `OPTIONAL_ENV_VARS` в `hermes_cli/config.py`.
- [ ] Необязательно: добавлено в `toolset_distributions.py` для пакетной обработки.
- [ ] Протестировано с `hermes chat -q "Use the weather tool for London"`