---
sidebar_position: 12
title: Плагины поставщика генерации видео
description: Как создать серверный плагин для создания видео для агента Hermes
---

# Создание плагина поставщика генерации видео

Плагины поставщика видеогенерации регистрируют серверную часть, которая обслуживает каждый вызов инструмента `video_generate`. Встроенные поставщики (xAI, FAL, DeepInfra) поставляются в виде плагинов. Добавьте новый или переопределите связанный, перетащив каталог в `plugins/video_gen/<name>/`.

:::совет
Генерация видео отражает [Плагины провайдера генерации изображений](/developer-guide/image-gen-provider-plugin) почти построчно — если вы создали серверную часть генерации изображений, вы уже знаете форму. Основные различия: метод `capabilities()`, рекламирующий модальности/соотношения сторон/длительность, а также соглашение о маршрутизации (передайте `image_url` для использования изображения в видео, опустите его для использования преобразования текста в видео — провайдер самостоятельно выбирает правильную конечную точку).
:::

## Единая поверхность (один инструмент, две модальности)

Инструмент `video_generate` предоставляет две модальности через один параметр:

- **Преобразование текста в видео** — звоните только с `prompt`. Поставщик направляется к своей конечной точке преобразования текста в видео.
- **Преобразование изображения в видео** — вызов с помощью `prompt` + `image_url`. Поставщик направляется к своей конечной точке преобразования изображения в видео.

Редактирование и расширение намеренно выходят за рамки. Большинство бэкэндов их не поддерживают, и из-за несоответствия в описание инструмента агента будет включено описание каждого бэкэнда.

## Как работает обнаружение

Hermes сканирует серверы видеогенерации в трех местах:

1. **В комплекте** — `<repo>/plugins/video_gen/<name>/` (автозагрузка с помощью `kind: backend`)
2. **Пользователь** — `~/.hermes/plugins/video_gen/<name>/` (подтвердите свое согласие через `plugins.enabled`).
3. **Pip** — пакеты, объявляющие точку входа `hermes_agent.plugins`.

Функция `register(ctx)` каждого плагина вызывает `ctx.register_video_gen_provider(...)`. Активного поставщика выбирает `video_gen.provider` в `config.yaml`; `hermes tools` → Video Generation помогает пользователям сделать выбор. В отличие от `image_generate`, здесь нет устаревшего бэкэнда внутри дерева — каждый провайдер представляет собой плагин.

## Структура каталогов

```
plugins/video_gen/my-backend/
├── __init__.py      # VideoGenProvider subclass + register()
└── plugin.yaml      # Manifest with kind: backend
```

## Азбука VideoGenProvider

Подкласс `agent.video_gen_provider.VideoGenProvider`. Обязательно: свойство `name` и метод `generate()`.

```python
# plugins/video_gen/my-backend/__init__.py
from typing import Any, Dict, List, Optional
import os

from agent.video_gen_provider import (
    VideoGenProvider,
    error_response,
    success_response,
)


class MyVideoGenProvider(VideoGenProvider):
    @property
    def name(self) -> str:
        return "my-backend"

    @property
    def display_name(self) -> str:
        return "My Backend"

    def is_available(self) -> bool:
        return bool(os.environ.get("MY_API_KEY"))

    def list_models(self) -> List[Dict[str, Any]]:
        # Each entry is a model FAMILY — a name the user picks once.
        # Your provider's generate() routes within the family based on
        # whether image_url was passed.
        return [
            {
                "id": "fast",
                "display": "Fast",
                "speed": "~30s",
                "strengths": "Cheapest tier",
                "price": "$0.05/s",
                "modalities": ["text", "image"],  # advisory
            },
        ]

    def default_model(self) -> Optional[str]:
        return "fast"

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text", "image"],
            "aspect_ratios": ["16:9", "9:16"],
            "resolutions": ["720p", "1080p"],
            "min_duration": 1,
            "max_duration": 10,
            "supports_audio": False,
            "supports_negative_prompt": True,
            "max_reference_images": 0,
        }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "My Backend",
            "badge": "paid",
            "tag": "Short description shown in `hermes tools`",
            "env_vars": [
                {
                    "key": "MY_API_KEY",
                    "prompt": "My Backend API key",
                    "url": "https://mybackend.example.com/keys",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,  # always ignore unknown kwargs for forward-compat
    ) -> Dict[str, Any]:
        # ROUTE: image_url presence picks the endpoint.
        if image_url:
            endpoint = "my-backend/image-to-video"
            modality_used = "image"
        else:
            endpoint = "my-backend/text-to-video"
            modality_used = "text"

        # ... call your API ...

        return success_response(
            video="https://your-cdn/output.mp4",
            model=model or "fast",
            prompt=prompt,
            modality=modality_used,
            aspect_ratio=aspect_ratio,
            duration=duration or 5,
            provider=self.name,
        )


def register(ctx) -> None:
    ctx.register_video_gen_provider(MyVideoGenProvider())
```

## Манифест плагина

```yaml
# plugins/video_gen/my-backend/plugin.yaml
name: my-backend
version: 1.0.0
description: "My video generation backend"
author: Your Name
kind: backend
requires_env:
  - MY_API_KEY
```

## Схема `video_generate`

Инструмент предоставляет одну схему для каждого бэкэнда. Провайдеры игнорируют параметры, которые они не поддерживают.

| Параметр | Что он делает |
|---|---|
| `prompt` | Текстовая инструкция (обязательно) |
| `image_url` | Если установлено → изображение в видео; если опущено → преобразование текста в видео |
| `reference_image_urls` | Ссылки на стили/символы (зависят от поставщика) |
| `duration` | Секунды — провайдерские зажимы |
| `aspect_ratio` | `"16:9"`, `"9:16"`, `"1:1"`, ... — зажимы провайдера |
| `resolution` | `"480p"` / `"540p"` / `"720p"` / `"1080p"` — зажимы провайдера |
| `negative_prompt` | Контент, которого следует избегать (только Pixverse/Kling) |
| `audio` | Встроенное аудио (ценовая категория Veo3/Pixverse) |
| `seed` | Воспроизводимость |
| `model` | Переопределить активную модель/семейство |

`capabilities()` провайдера сообщает, какие из них соблюдаются. Агент видит возможности активной серверной части в описании инструмента, которое динамически перестраивается, когда пользователь меняет серверную часть через `hermes tools`.

## Семейства моделей и маршрутизация конечных точек (шаблон FAL)

Если ваш сервер имеет несколько конечных точек для каждой «модели» — например, FAL, где каждое семейство (Veo 3.1, Pixverse v6, Kling O3) имеет URL-адреса `/text-to-video` и `/image-to-video` — представляет каждое **семейство** как одну запись каталога. Ваш `generate()` выбирает правильную конечную точку в зависимости от того, был ли передан `image_url`:

```python
FAMILIES = {
    "veo3.1": {
        "text_endpoint": "fal-ai/veo3.1",
        "image_endpoint": "fal-ai/veo3.1/image-to-video",
        # ... family-specific capability flags ...
    },
}

def generate(self, prompt, *, image_url=None, model=None, **kwargs):
    family_id, family = _resolve_family(model)
    endpoint = family["image_endpoint"] if image_url else family["text_endpoint"]
    # ... build payload from family's declared capability flags, call endpoint ...
```

Пользователь выбирает `veo3.1` один раз в `hermes tools`. Агент никогда не думает о конечных точках — он просто проходит (или не проходит) `image_url`.

## Приоритет выбора

Для регуляторов модели для каждого экземпляра (см. `plugins/video_gen/fal/__init__.py`):

1. Ключевое слово `model=` из вызова инструмента.
2. `<PROVIDER>_VIDEO_MODEL` переменная окружения
3. `video_gen.<provider>.model` в `config.yaml`
4. `video_gen.model` в `config.yaml` (если это один из ваших идентификаторов)
5. `default_model()` поставщика услуг

## Форма ответа

`success_response()` и `error_response()` создают форму dict при каждом возврате серверной части. Используйте их — не прокручивайте диктовку вручную.

Ключи успеха: `success`, `video` (URL-адрес или абсолютный путь), `model`, `prompt`, `modality` (`"text"` или `"image"`), `aspect_ratio`, `duration`, `provider` и `extra`.

Ключи ошибок: `success`, `video` (нет), `error`, `error_type`, `model`, `prompt`, `aspect_ratio`, `provider`.

## Куда сохранять артефакты

Если ваш сервер возвращает base64, используйте `save_b64_video()` для записи под `$HERMES_HOME/cache/videos/`. Для необработанных байтов из последующей выборки HTTP используйте `save_bytes_video()`. В противном случае верните восходящий URL-адрес напрямую — шлюз разрешает удаленные URL-адреса при доставке.

## Тестирование

Проведите дымовой тест под `tests/plugins/video_gen/test_<name>_plugin.py`. Тесты xAI и FAL показывают шаблон — зарегистрируйтесь, проверьте каталог, выполните маршрутизацию как с `image_url`, так и без него, подтвердите чистые ответы об ошибках при отсутствии аутентификации.