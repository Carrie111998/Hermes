---
sidebar_position: 13
title: Плагины провайдера браузера
description: Как создать серверный плагин облачного браузера для агента Hermes
---

# Создание плагина провайдера браузера

Плагины поставщика браузера регистрируют **серверную часть облачного браузера**, которая обслуживает вызовы инструментов `browser_*` в облачном режиме (навигация, щелчок, снимок экрана и т. д.). Встроенные поставщики — Browserbase, Browser Use и Firecrawl — все поставляются в виде плагинов под `plugins/browser/<name>/`. Вы можете добавить новый или переопределить связанный, поместив каталог рядом с ним.

:::совет
Серверные модули браузера — один из нескольких **подключаемых модулей**, поддерживаемых Hermes. Остальные (со своими собственными ABC) — это [Плагины поставщика веб-поиска](/developer-guide/web-search-provider-plugin) (которые этот ABC намеренно отражает), [Генерация изображений](/developer-guide/image-gen-provider-plugin), [Генерация видео](/developer-guide/video-gen-provider-plugin), [Поставщики памяти](/developer-guide/memory-provider-plugin), [Контекстные механизмы](/developer-guide/context-engine-plugin), [Секретные источники](/developer-guide/secret-source-plugin) и [Поставщики моделей](/developer-guide/model-provider-plugin). Общие плагины инструментов/хуков/CLI находятся в папке [Создать плагин Hermes](/developer-guide/plugins).
:::

## Как это сочетается друг с другом

Поставщик браузера **не** реализует просмотр. Он реализует **жизненный цикл сеанса**: создает сеанс удаленного браузера, возвращает URL-адрес веб-сокета CDP и разрывает сеанс. Собственный стек браузера Hermes (`agent-browser` + `tools/browser_tool.py`) подключается к любому URL-адресу CDP, который вы возвращаете, и управляет страницей оттуда — каждый провайдер получает полный набор инструментов `browser_*` бесплатно.

Активный поставщик выбран `browser.cloud_provider` в `config.yaml`; диспетчер в `tools/browser_tool.py` представляет собой чистый поиск в реестре без каких-либо условий для каждого поставщика.

## Открытие

Hermes сканирует серверную часть браузера в трех местах:

1. **В комплекте** — `<repo>/plugins/browser/<name>/` (автозагрузка с помощью `kind: backend`)
2. **Пользователь** — `~/.hermes/plugins/browser/<name>/` (подтвердите свое согласие через `plugins.enabled` или `hermes plugins enable <name>`).
3. **Pip** — пакеты, объявляющие точку входа `hermes_agent.plugins`.

`register(ctx)` каждого плагина вызывает `ctx.register_browser_provider(...)`, который помещает экземпляр в реестр в `agent/browser_registry.py`.

## Структура каталогов

```
plugins/browser/my-backend/
├── __init__.py     # register() entry point
├── provider.py     # BrowserProvider subclass
└── plugin.yaml     # Manifest with kind: backend and provides_browser_providers
```

`plugin.yaml`:

```yaml
name: browser-my-backend
version: 1.0.0
description: "My cloud browser backend. Requires MY_BACKEND_API_KEY."
author: you
kind: backend
provides_browser_providers:
  - my-backend
```

`__init__.py`:

```python
from plugins.browser.my_backend.provider import MyBackendProvider


def register(ctx) -> None:
    ctx.register_browser_provider(MyBackendProvider())
```

## ABC BrowserProvider

Внедрите `agent.browser_provider.BrowserProvider`. Три метода жизненного цикла плюс удостоверение:

```python
from agent.browser_provider import BrowserProvider


class MyBackendProvider(BrowserProvider):
    @property
    def name(self) -> str:
        return "my-backend"          # the browser.cloud_provider config value

    @property
    def display_name(self) -> str:
        return "My Backend"          # shown in `hermes tools`

    def is_available(self) -> bool:
        """Cheap check only — env var present, dep importable.
        NO network calls: runs at tool-registration time and on every
        `hermes tools` paint."""
        return bool(os.environ.get("MY_BACKEND_API_KEY"))

    def create_session(self, task_id: str) -> dict:
        """Create a remote browser session; return the session-metadata contract."""
        session = my_api.create_browser(...)
        return {
            "session_name": f"my-backend-{task_id}",  # unique agent-browser session name
            "bb_session_id": session.id,              # provider session ID (for cleanup)
            "cdp_url": session.cdp_ws_url,            # CDP websocket URL
            "features": {"stealth": True},            # feature flags you enabled
        }

    def close_session(self, session_id: str) -> bool:
        """Terminate by provider session ID. Log-and-return-False on error —
        never raise, so the dispatcher's cleanup loop keeps moving."""
        ...

    def emergency_cleanup(self, session_id: str) -> None:
        """Best-effort teardown from atexit/signal handlers. Must not raise."""
        ...
```

### Контракт метаданных сеанса

`create_session()` должен возвращать как минимум `session_name`, `bb_session_id`, `cdp_url` и `features`. Две особенности, которые стоит знать:

- **`bb_session_id` — это устаревшее имя ключа**, сохраненное дословно для обратной совместимости с `tools/browser_tool.py` — оно содержит идентификатор сеанса *вашего* провайдера независимо от поставщика. Не переименовывайте его.
- `create_session()` **может поднять** — `ValueError` при отсутствии учетных данных, `RuntimeError` при сбоях сети/API. Диспетчер передает их пользователю. Это отличается от `close_session`/`emergency_cleanup`, которые никогда не должны повышать ставку.

Дополнительный ключ `external_call_id` поддерживает выставление счетов через управляемый шлюз.

### `get_setup_schema()` — строка выбора `hermes tools`

Переопределите это, чтобы оно отображалось как первоклассный вариант в средстве выбора автоматизации браузера с подсказками API-ключа и крючком установки:

```python
def get_setup_schema(self) -> dict:
    return {
        "name": "My Backend",
        "badge": "paid",
        "tag": "Cloud browser with stealth and proxies",
        "env_vars": [
            {"key": "MY_BACKEND_API_KEY",
             "prompt": "My Backend API key",
             "url": "https://mybackend.example"},
        ],
        "post_setup": "agent_browser",   # ensures local Chromium is installed (agent-browser itself resolves via npx)
    }
```

Согласно стандарту проекта для бэкэндов инструментов: если бэкэнд не может быть выбран и настроен через `hermes tools`, это не делается — «установить эту переменную окружения вручную» не является интеграцией.

## Пользователи настраивают его

```yaml
browser:
  cloud_provider: my-backend
```

## Эталонные реализации

Три связанных поставщика под `plugins/browser/` являются каноническими примерами с возрастающей сложностью: `firecrawl` (самый простой), `browser_use` и `browserbase` (флаги функций скрытности/прокси/поддержки активности с плавным возвратом в случае недоступности платных функций). Скопируйте ближайший.

## Контрольный список

- [ ] `name` — строчные буквы и стабильный (это значение конфигурации, которое пишут пользователи)
- [ ] `is_available()` не совершает сетевых вызовов
- [ ] `create_session()` возвращает полный контракт метаданных (имя ключа `bb_session_id` не повреждено)
- [ ] `close_session()` / `emergency_cleanup()` никогда не повышает ставку
- [ ] `get_setup_schema()` предоставляет доступ к вашим переменным окружения, чтобы `hermes tools` мог настроить серверную часть
- [ ] `plugin.yaml` объявляет `kind: backend` + `provides_browser_providers`