---
sidebar_position: 12
title: Плагины поставщика веб-поиска
description: Как создать серверный плагин веб-поиска/извлечения/сканирования для агента
  Hermes
---

# Создание плагина поставщика веб-поиска

Плагины поставщика веб-поиска регистрируют серверную часть, которая обслуживает `web_search`, `web_extract` и (необязательно) вызовы инструментов глубокого сканирования. Встроенные поставщики — Firecrawl, SearXNG, Tavily, Exa, Parallel, Brave Search (уровень бесплатного пользования), xAI и DDGS — все поставляются в виде плагинов под `plugins/web/<name>/`. Вы можете добавить новый или переопределить связанный, поместив каталог рядом с ним.

:::совет
Веб-поиск — один из нескольких **бэкэнд-плагинов**, поддерживаемых Hermes. Остальные (со своими собственными ABC): [Плагины поставщика изображений](/developer-guide/image-gen-provider-plugin), [Плагины поставщика видеогенерации](/developer-guide/video-gen-provider-plugin), [Плагины поставщика памяти](/developer-guide/memory-provider-plugin), [Плагины контекстного движка](/developer-guide/context-engine-plugin) и [Model Плагины поставщика](/developer-guide/model-provider-plugin). Общие плагины инструментов/хуков/CLI находятся в папке [Создать плагин Hermes](/developer-guide/plugins).
:::

## Как работает обнаружение

Hermes сканирует серверы веб-поиска в трех местах:

1. **В комплекте** — `<repo>/plugins/web/<name>/` (автоматически загружается вместе с `kind: backend`, всегда доступен)
2. **Пользователь** — `~/.hermes/plugins/web/<name>/` (подтвердите свое согласие через `plugins.enabled` или `hermes plugins enable <name>`).
3. **Pip** — пакеты, объявляющие точку входа `hermes_agent.plugins`.

Функция `register(ctx)` каждого плагина вызывает `ctx.register_web_search_provider(...)` — это помещает экземпляр в реестр в `agent/web_search_registry.py`. Активный поставщик для каждой возможности выбирается конфигурацией:

| Возможность | Конфигурационный ключ | Возвращается к |
|---|---|---|
| `web_search` | `web.search_backend` | `web.backend` |
| `web_extract` | `web.extract_backend` | `web.backend` |
| Режимы глубокого сканирования внутри `web_extract` | `web.extract_backend` | `web.backend` |

Если ни один из ключей не установлен, Hermes автоматически определяет серверную часть по любому ключу API или URL-адресу, присутствующему в среде. `hermes tools` помогает пользователям сделать выбор.

## Структура каталогов

```
plugins/web/my-backend/
├── __init__.py     # register() entry point
├── provider.py     # WebSearchProvider subclass
└── plugin.yaml     # Manifest with kind: backend and provides_web_providers
```

`brave_free/` и `ddgs/` — это наименьшие ссылки в дереве: `brave_free` для поставщика, предназначенного только для поиска с использованием ключа API, `ddgs` для поставщика без ключа, который лениво устанавливает свой SDK.

## Азбука WebSearchProvider

Подкласс `agent.web_search_provider.WebSearchProvider`. Единственными обязательными членами являются `name`, `is_available()` и любой из `search()`/`extract()`, который вы реализуете. (Глубокое сканирование — это не отдельный метод, а режим `extract()`.)

```python
# plugins/web/my-backend/provider.py
from __future__ import annotations

import os
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider


class MyBackendWebSearchProvider(WebSearchProvider):
    """Minimal search-only provider against the My Backend HTTP API."""

    @property
    def name(self) -> str:
        # Stable id used in web.search_backend / web.extract_backend / web.backend
        # config keys. Lowercase, no spaces; hyphens permitted.
        return "my-backend"

    @property
    def display_name(self) -> str:
        # Human label shown in `hermes tools`. Defaults to `name`.
        return "My Backend"

    def is_available(self) -> bool:
        # Cheap check — env var present, optional dep importable, etc.
        # MUST NOT make network calls (runs on every `hermes tools` paint).
        return bool(os.getenv("MY_BACKEND_API_KEY", "").strip())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        import httpx

        api_key = os.environ["MY_BACKEND_API_KEY"]
        try:
            resp = httpx.get(
                "https://api.example.com/search",
                params={"q": query, "count": max(1, min(int(limit), 20))},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            return {"success": False, "error": str(exc)}

        # Response shape is fixed — see "Response shape" below.
        return {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "description": item.get("snippet", ""),
                        "position": idx + 1,
                    }
                    for idx, item in enumerate(data.get("results", []))
                ],
            },
        }
```

```python
# plugins/web/my-backend/__init__.py
from plugins.web.my_backend.provider import MyBackendWebSearchProvider


def register(ctx) -> None:
    """Plugin entry point — called once at load time."""
    ctx.register_web_search_provider(MyBackendWebSearchProvider())
```

## плагин.yaml

```yaml
name: web-my-backend
version: 1.0.0
description: "My Backend web search — Bearer-auth REST API"
author: Your Name
kind: backend
provides_web_providers:
  - my-backend
requires_env:
  - MY_BACKEND_API_KEY
```

| Ключ | Цель |
|---|---|
| `kind: backend` | Направляет плагин через путь загрузки серверной части |
| `provides_web_providers` | Список провайдеров `name`, которые регистрирует этот плагин — используется загрузчиком для рекламы плагина в `hermes tools` даже до запуска `register()` |
| `requires_env` | Интерактивный запрос учетных данных во время `hermes plugins install` (см. [Создание плагина Hermes](/developer-guide/plugins#gate-on-environment-variables) для расширенного формата) |

## Ссылка ABC

Полный контракт в `agent/web_search_provider.py`. Методы, которые вы можете переопределить:

| Член | Требуется | По умолчанию | Цель |
|---|---|---|---|
| `name` | ✅ | — | Стабильный идентификатор, используемый в конфигурации `web.*_backend` |
| `display_name` | — | `name` | Этикетка показана в `hermes tools` |
| `is_available()` | ✅ | — | Дешевый шлюз доступности — переменные env, дополнительные параметры |
| `supports_search()` | — | `True` | Флаг возможности для маршрутизации `web_search` |
| `supports_extract()` | — | `False` | Флаг возможности для маршрутизации `web_extract` |
| `search(query, limit)` | условный | поднимает | Требуется, когда `supports_search()` возвращает `True` |
| `extract(urls, **kwargs)` | условный | поднимает | Требуется, когда `supports_extract()` возвращает `True` |

Провайдеры могут рекламировать несколько возможностей одного класса — Firecrawl, Tavily, Exa и Parallel реализуют как поиск, так и извлечение. Brave Search и DDGS предназначены только для поиска; SearXNG предназначен только для поиска и использует документированный рабочий процесс «соедини меня с поставщиком извлечения».

## Форма ответа

Оболочка инструмента ожидает фиксированного конверта, поэтому ему не нужно переводить между серверами.

**Успешный поиск:**

```python
{
    "success": True,
    "data": {
        "web": [
            {"title": str, "url": str, "description": str, "position": int},
            ...
        ],
    },
}
```

**Извлечение успеха:**

```python
{
    "success": True,
    "data": [
        {
            "url": str,
            "title": str,
            "content": str,
            "raw_content": str,
            "metadata": dict,    # optional
            "error": str,        # optional, only on per-URL failure
        },
        ...
    ],
}
```

**Любая возможность, в случае сбоя:**

```python
{"success": False, "error": "human-readable message"}
```

И `search()`, и `extract()` могут быть `async def` — диспетчер обнаруживает сопрограммные функции через `inspect.iscoroutinefunction` и соответственно ожидает. Реализации синхронизации, которые блокируют ввод-вывод (HTTP, вызовы SDK), подходят для небольших серверов; диспетчер обрабатывает потоки.

## Флаги возможностей

Hermes направляет вызовы нужному провайдеру на основе флагов `supports_*`. Обычная настройка нескольких провайдеров:

```yaml
# ~/.hermes/config.yaml
web:
  search_backend: "brave-free"     # search-only, fast, free 2k/mo
  extract_backend: "firecrawl"     # extract + crawl, paid quota
```

Если `web.search_backend` или `web.extract_backend` не установлены, оба переходят на `web.backend`. Если этот параметр также не установлен, Hermes выбирает первого доступного поставщика, который поддерживает запрошенную возможность, на основе присутствия env-var.

Если ваш провайдер поддерживает только одну возможность, оставьте для остальных флагов значения по умолчанию (`False`), и реестр пропустит их для этого инструмента — пользователи не увидят вводящие в заблуждение ошибки «ошибка провайдера X», когда они используют X только для поиска и просят агента извлечь данные.

## Как Hermes подключает его к инструментам

Инструменты `web_search` и `web_extract` находятся в `tools/web_tools.py`. Во время звонка они:

1. Прочтите соответствующий ключ конфигурации (`web.search_backend` для `web_search`, `web.extract_backend` для `web_extract`).
2. Запросите в реестре провайдера с этим `name`.
3. Проверьте `is_available()` и соответствующий флаг `supports_*()`.
4. Отправка в `search()`/`extract()` (глубокое сканирование выполняется как режим внутри `extract()`), ожидая, является ли метод сопрограммой.
5. Сериализуйте конверт ответа в формате JSON и передайте его LLM.

Ошибки появляются в результате работы инструмента; LLM решает, как их объяснить. Если ни один поставщик не зарегистрирован (или каждый доступный поставщик не прошел проверку возможностей), инструмент возвращает полезную ошибку, указывающую на `hermes tools`.

## Отложенная установка дополнительных зависимостей

Если ваш провайдер использует сторонний SDK (как это делает DDGS с пакетом `ddgs`), не используйте `import` на верхнем уровне модуля. Используйте `tools.lazy_deps.ensure(...)` внутри `is_available()` или `search()` — Hermes установит пакет при первом использовании, контролируемый `security.allow_lazy_installs`. См. [Создание плагина Hermes → Lazy-install](/developer-guide/plugins#lazy-install-optional-python-зависимости) для модели безопасности.

## Эталонные реализации

- **`plugins/web/brave_free/`** — небольшой HTTP-провайдер с ключом API и только для поиска. Хороший стартовый шаблон.
- **`plugins/web/ddgs/`** — поставщик без ключей, который лениво устанавливает свой SDK. Полезный шаблон для бэкэндов, обертывающих пакет Python.
- **`plugins/web/firecrawl/`** — многофункциональный поставщик (поиск + извлечение + сканирование) с несколькими режимами форматирования.
- **`plugins/web/searxng/`** — автономный сервер с настройкой URL-адреса без аутентификации.
- **`plugins/web/xai/`** — поиск на основе LLM с помощью серверного инструмента Grok `web_search`. Показывает, как повторно использовать существующую поверхность учетных данных OAuth/env-var (`tools/xai_http.py`) без добавления новых переменных среды и как написать дешевый `is_available()`, который соблюдает контракт без сети.

## Распространение через pip

```toml
# pyproject.toml
[project.entry-points."hermes_agent.plugins"]
my-backend-web = "my_backend_web_package"
```

`my_backend_web_package` должен предоставлять функцию `register` верхнего уровня. См. [Распространение через pip](/developer-guide/plugins#distribute-via-pip) в общем руководстве по плагинам для полной настройки.

## Похожие страницы

- [Веб-поиск](/user-guide/features/web-search) — документация по функциям, ориентированная на пользователя, и конфигурация для каждого серверного компонента.
- [Обзор плагинов](/user-guide/features/plugins) — краткий обзор всех типов плагинов.
- [Создать плагин Hermes](/developer-guide/plugins) — руководство по общим инструментам/хукам/командам слэша.