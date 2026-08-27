---
sidebar_position: 11
title: Плагины поставщика генерации изображений
description: Как создать серверный плагин для создания изображений для агента Hermes
---

# Создание плагина поставщика генерации изображений

Плагины поставщика изображений регистрируют серверную часть, которая обслуживает каждый вызов инструмента `image_generate` — DALL·E, gpt-image, Grok, Flux, Imagen, Stable Diffusion, fal, Replication, локальную установку ComfyUI и что угодно. Все встроенные провайдеры (OpenAI, OpenAI-Codex, xAI, FAL, Krea, DeepInfra, OpenRouter) поставляются в виде плагинов. Вы можете добавить новый или переопределить связанный, перетащив каталог в `plugins/image_gen/<name>/`.

:::совет
Image-gen — один из нескольких **бэкэнд-плагинов**, поддерживаемых Hermes. Остальные (с более специализированными ABC) — это [Плагины поставщика памяти](/developer-guide/memory-provider-plugin), [Плагины контекстного механизма](/developer-guide/context-engine-plugin) и [Плагины поставщика модели](/developer-guide/model-provider-plugin). Общие плагины инструментов/хуков/CLI находятся в папке [Создать плагин Hermes](/developer-guide/plugins).
:::

## Как работает обнаружение

Hermes сканирует серверы генерации изображений в трех местах:

1. **В комплекте** — `<repo>/plugins/image_gen/<name>/` (автоматически загружается вместе с `kind: backend`, всегда доступен)
2. **Пользователь** — `~/.hermes/plugins/image_gen/<name>/` (подтвердите свое согласие через `plugins.enabled`)
3. **Pip** — пакеты, объявляющие точку входа `hermes_agent.plugins`.

Функция `register(ctx)` каждого плагина вызывает `ctx.register_image_gen_provider(...)` — она помещается в реестр в `agent/image_gen_registry.py`. Активного поставщика выбирает `image_gen.provider` в `config.yaml`; `hermes tools` помогает пользователям сделать выбор.

Оболочка инструмента `image_generate` запрашивает в реестре активный поставщик и отправляет его туда. Если ни один поставщик не зарегистрирован, инструмент выдает полезную ошибку с номером `hermes tools`.

## Структура каталогов

```
plugins/image_gen/my-backend/
├── __init__.py      # ImageGenProvider subclass + register()
└── plugin.yaml      # Manifest with kind: backend
```

На данный момент встроенный плагин готов. Пользовательские плагины в `~/.hermes/plugins/image_gen/<name>/` необходимо добавить в `plugins.enabled` в `config.yaml` (или запустить `hermes plugins enable <name>`).

## ABC ImageGenProvider

Подкласс `agent.image_gen_provider.ImageGenProvider`. Единственными обязательными членами являются свойство `name` и метод `generate()`, все остальное имеет разумные значения по умолчанию:

```python
# plugins/image_gen/my-backend/__init__.py
from typing import Any, Dict, List, Optional
import os

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)


class MyBackendImageGenProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        # Stable id used in image_gen.provider config. Lowercase, no spaces.
        return "my-backend"

    @property
    def display_name(self) -> str:
        # Human label shown in `hermes tools`. Defaults to name.title() if omitted.
        return "My Backend"

    def is_available(self) -> bool:
        # Return False if credentials or deps are missing.
        # The tool's availability gate calls this before dispatch.
        if not os.environ.get("MY_BACKEND_API_KEY"):
            return False
        try:
            import my_backend_sdk  # noqa: F401
        except ImportError:
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        # Catalog shown in `hermes tools` model picker.
        return [
            {
                "id": "my-model-fast",
                "display": "My Model (Fast)",
                "speed": "~5s",
                "strengths": "Quick iteration",
                "price": "$0.01/image",
            },
            {
                "id": "my-model-hq",
                "display": "My Model (HQ)",
                "speed": "~30s",
                "strengths": "Highest fidelity",
                "price": "$0.04/image",
            },
        ]

    def default_model(self) -> Optional[str]:
        return "my-model-fast"

    def get_setup_schema(self) -> Dict[str, Any]:
        # Metadata for the `hermes tools` picker — keys to prompt for at setup.
        return {
            "name": "My Backend",
            "badge": "paid",        # optional; shown as a short tag in the picker
            "tag": "One-line description shown under the name",
            "env_vars": [
                {
                    "key": "MY_BACKEND_API_KEY",
                    "prompt": "My Backend API key",
                    "url": "https://my-backend.example.com/api-keys",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        # Declare whether this backend supports image-to-image / editing.
        # The tool layer surfaces this in the dynamic schema so the model
        # knows when `image_url` is honored. Default (if you omit this) is
        # text-only: {"modalities": ["text"], "max_reference_images": 0}.
        return {"modalities": ["text", "image"], "max_reference_images": 4}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect_ratio = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required",
                error_type="invalid_input",
                provider=self.name,
                prompt="",
                aspect_ratio=aspect_ratio,
            )

        # Routing: if image_url (or reference_image_urls) is set, the call is
        # an image-to-image / edit request; otherwise text-to-image. Report
        # which path you took via the `modality` field of success_response.
        sources = []
        if image_url:
            sources.append(image_url)
        sources.extend(normalize_reference_images(reference_image_urls) or [])
        modality = "image" if sources else "text"

        # Model selection precedence: env var → config → default. The helper
        # _resolve_model() in the built-in openai plugin is a good reference.
        model_id = kwargs.get("model") or self.default_model() or "my-model-fast"

        try:
            import my_backend_sdk
            client = my_backend_sdk.Client(api_key=os.environ["MY_BACKEND_API_KEY"])
            if modality == "image":
                result = client.edit(
                    prompt=prompt,
                    model=model_id,
                    image_urls=sources,
                )
            else:
                result = client.generate(
                    prompt=prompt,
                    model=model_id,
                    aspect_ratio=aspect_ratio,
                )

            # Two shapes supported:
            #   - URL string: return it as `image`
            #   - base64 data: save under $HERMES_HOME/cache/images/ via save_b64_image()
            if result.get("image_b64"):
                path = save_b64_image(
                    result["image_b64"],
                    prefix=self.name,
                    extension="png",
                )
                image = str(path)
            else:
                image = result["image_url"]

            return success_response(
                image=image,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                provider=self.name,
                modality=modality,
            )
        except Exception as exc:
            return error_response(
                error=str(exc),
                error_type=type(exc).__name__,
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )


def register(ctx) -> None:
    """Plugin entry point — called once at load time."""
    ctx.register_image_gen_provider(MyBackendImageGenProvider())
```

## плагин.yaml

```yaml
name: my-backend
version: 1.0.0
description: My image backend — text-to-image via My Backend SDK
author: Your Name
kind: backend
requires_env:
  - MY_BACKEND_API_KEY
```

`kind: backend` — это то, что направляет плагин по пути регистрации image-gen. `requires_env` запрашивается во время `hermes plugins install`.

## Ссылка ABC

Полный контракт в `agent/image_gen_provider.py`. Методы, которые вы обычно переопределяете:

| Член | Требуется | По умолчанию | Цель |
|---|---|---|---|
| `name` | ✅ | — | Стабильный идентификатор, используемый в конфигурации `image_gen.provider` |
| `display_name` | — | `name.title()` | Этикетка показана в `hermes tools` |
| `is_available()` | — | `True` | Ворота для недостающих кредитов/депозитов |
| `list_models()` | — | `[]` | Каталог для средства выбора модели `hermes tools` |
| `default_model()` | — | впервые от `list_models()` | Резервный вариант, когда модель не настроена |
| `get_setup_schema()` | — | минимальный | Метаданные средства выбора + приглашения env-var |
| `generate(prompt, aspect_ratio, **kwargs)` | ✅ | — | Звонок |

## Формат ответа

`generate()` должен вернуть словарь, созданный с помощью `success_response()` или `error_response()`. Оба живут в `agent/image_gen_provider.py`.

**Успех:**
```python
success_response(
    image=<url-or-absolute-path>,
    model=<model-id>,
    prompt=<echoed-prompt>,
    aspect_ratio="landscape" | "square" | "portrait",
    provider=<your-provider-name>,
    extra={...},  # optional backend-specific fields
)
```

**Ошибка:**
```python
error_response(
    error="human-readable message",
    error_type="provider_error" | "invalid_input" | "<exception class name>",
    provider=<your-provider-name>,
    model=<model-id>,
    prompt=<prompt>,
    aspect_ratio=<resolved aspect>,
)
```

Оболочка инструмента JSON-сериализует словарь и передает его LLM. Ошибки появляются в результате работы инструмента; LLM решает, как объяснить их пользователю.

## Обработка вывода base64 и URL-адреса

Некоторые бэкэнды возвращают URL-адреса изображений (fal, Replication); другие возвращают полезные данные base64 (OpenAI gpt-image-2). Для случая base64 используйте `save_b64_image()` — он записывает в `$HERMES_HOME/cache/images/<prefix>_<timestamp>_<uuid>.<ext>` и возвращает абсолютное значение `Path`. Передайте этот путь (как `str`) как `image=` в `success_response()`. Доставка через шлюз (фото-пузырь Telegram, вложение Discord) распознает как URL-адреса, так и абсолютные пути.

## Пользовательские переопределения

Перетащите пользовательский плагин в `~/.hermes/plugins/image_gen/<name>/` с тем же свойством `name`, что и у встроенного, и включите его через `hermes plugins enable <name>` — в реестре побеждает последняя запись, поэтому ваша версия заменяет встроенную. Полезно для указания плагина `openai` частного прокси или замены каталога пользовательских моделей.

## Тестирование

```bash
export HERMES_HOME=/tmp/hermes-imggen-test
mkdir -p $HERMES_HOME/plugins/image_gen/my-backend
# …copy __init__.py + plugin.yaml into that dir…

export MY_BACKEND_API_KEY=your-test-key
hermes plugins enable my-backend

# Pick it as the active provider
echo "image_gen:" >> $HERMES_HOME/config.yaml
echo "  provider: my-backend" >> $HERMES_HOME/config.yaml

# Exercise it
hermes -z "Generate an image of a corgi in a spacesuit"
```

Или в интерактивном режиме: `hermes tools` → «Создание изображения» → выберите `my-backend` → введите ключ API, если будет предложено.

## Эталонные реализации

- **`plugins/image_gen/openai/__init__.py`** — gpt-image-2 на низком, среднем и высоком уровнях в виде трех идентификаторов виртуальной модели, совместно использующих одну модель API с разными параметрами `quality`. Хороший пример многоуровневых моделей в рамках одной цепочки приоритетов бэкенд + config.yaml.
- **`plugins/image_gen/xai/__init__.py`** — Грок Imagine через xAI. Другая форма (вывод URL, упрощенный каталог).
- **`plugins/image_gen/openai-codex/__init__.py`** — вариант API ответов в стиле Кодекса, повторно использующий OpenAI SDK с другим базовым URL-адресом маршрутизации.

## Распространение через pip

```toml
# pyproject.toml
[project.entry-points."hermes_agent.plugins"]
my-backend-imggen = "my_backend_imggen_package"
```

`my_backend_imggen_package` должен предоставлять функцию `register` верхнего уровня. См. [Распространение через pip](/developer-guide/plugins#distribute-via-pip) в общем руководстве по плагинам для полной настройки.

## Похожие страницы

- [Генерация изображений](/user-guide/features/image-generation) — документация по функциям, ориентированная на пользователя.
- [Обзор плагинов](/user-guide/features/plugins) — краткий обзор всех типов плагинов.
- [Создать плагин Hermes](/developer-guide/plugins) — руководство по общим инструментам/хукам/командам слэша.