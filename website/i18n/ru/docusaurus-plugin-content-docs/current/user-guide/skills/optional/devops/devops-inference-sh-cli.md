---
title: Inference Sh Cli — запускайте более 150 приложений искусственного интеллекта
  (изображения, видео, LLM) через интерфейс командной строки inference.sh.
sidebar_label: Inference Sh Cli
description: Запускайте более 150 приложений искусственного интеллекта (изображения,
  видео, LLM) через интерфейс командной строки inference.sh
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Вывод Sh Cli

Запускайте более 150 приложений искусственного интеллекта (изображения, видео, LLM) через интерфейс командной строки inference.sh.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/devops/inference-sh-cli` |
| Путь | `optional-skills/devops/inference-sh-cli` |
| Версия | `1.0.0` |
| Автор | окарис |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `AI`, `image-generation`, `video`, `LLM`, `search`, `inference`, `FLUX`, `Veo`, `Claude` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# inference.sh CLI

Запускайте более 150 приложений искусственного интеллекта в облаке с помощью простого интерфейса командной строки. Графический процессор не требуется.

Все команды используют **инструмент терминала** для запуска `infsh` команд.

## Когда использовать

- Пользователь просит сгенерировать изображения (FLUX, Reve, Seedream, Grok, Gemini image)
- Пользователь просит сгенерировать видео (Veo, Wan, Seedance, OmniHuman)
- Пользователь спрашивает о inference.sh или infsh.
- Пользователь хочет запускать приложения AI без управления API отдельных поставщиков.
- Пользователь запрашивает поиск с помощью искусственного интеллекта (Тавили, Экса)
- Пользователю необходимо создать аватар/синхронизацию губ.

## Предварительные условия

Интерфейс командной строки `infsh` должен быть установлен и проверен. Проверьте с:

```bash
infsh me
```

Если не установлено:

```bash
curl -fsSL https://cli.inference.sh | sh
infsh login
```

Подробные сведения о настройке см. в разделе `references/authentication.md`.

## Рабочий процесс

### 1. Всегда ищите первым

Никогда не угадывайте названия приложений — всегда ищите правильный идентификатор приложения:

```bash
infsh app list --search flux
infsh app list --search video
infsh app list --search image
```

### 2. Запустите приложение

Используйте точный идентификатор приложения из результатов поиска. Всегда используйте `--json` для машиночитаемого вывода:

```bash
infsh app run <app-id> --input '{"prompt": "your prompt here"}' --json
```

### 3. Анализ вывода

Выходные данные JSON содержат URL-адреса сгенерированных медиафайлов. Предоставьте их пользователю с помощью `MEDIA:<url>` для встроенного отображения.

## Общие команды

### Генерация изображений

```bash
# Search for image apps
infsh app list --search image

# FLUX Dev with LoRA
infsh app run falai/flux-dev-lora --input '{"prompt": "sunset over mountains", "num_images": 1}' --json

# Gemini image generation
infsh app run google/gemini-2-5-flash-image --input '{"prompt": "futuristic city", "num_images": 1}' --json

# Seedream (ByteDance)
infsh app run bytedance/seedream-5-lite --input '{"prompt": "nature scene"}' --json

# Grok Imagine (xAI)
infsh app run xai/grok-imagine-image --input '{"prompt": "abstract art"}' --json
```

### Генерация видео

```bash
# Search for video apps
infsh app list --search video

# Veo 3.1 (Google)
infsh app run google/veo-3-1-fast --input '{"prompt": "drone shot of coastline"}' --json

# Seedance (ByteDance)
infsh app run bytedance/seedance-1-5-pro --input '{"prompt": "dancing figure", "resolution": "1080p"}' --json

# Wan 2.5
infsh app run falai/wan-2-5 --input '{"prompt": "person walking through city"}' --json
```

### Загрузка локальных файлов

CLI автоматически загружает локальные файлы, когда вы указываете путь:

```bash
# Upscale a local image
infsh app run falai/topaz-image-upscaler --input '{"image": "/path/to/photo.jpg", "upscale_factor": 2}' --json

# Image-to-video from local file
infsh app run falai/wan-2-5-i2v --input '{"image": "/path/to/image.png", "prompt": "make it move"}' --json

# Avatar with audio
infsh app run bytedance/omnihuman-1-5 --input '{"audio": "/path/to/audio.mp3", "image": "/path/to/face.jpg"}' --json
```

### Поиск и исследования

```bash
infsh app list --search search
infsh app run tavily/tavily-search --input '{"query": "latest AI news"}' --json
infsh app run exa/exa-search --input '{"query": "machine learning papers"}' --json
```

### Другие категории

```bash
# 3D generation
infsh app list --search 3d

# Audio / TTS
infsh app list --search tts

# Twitter/X automation
infsh app list --search twitter
```

## Подводные камни

1. **Никогда не угадывайте идентификаторы приложений** — всегда сначала запускайте `infsh app list --search <term>`. Идентификаторы приложений меняются, и новые приложения часто добавляются.
2. **Всегда используйте `--json`** — необработанный вывод сложно проанализировать. Флаг `--json` обеспечивает структурированный вывод с URL-адресами.
3. **Проверка аутентификации** — если команды завершаются с ошибками аутентификации, запустите `infsh login` или убедитесь, что установлен `INFSH_API_KEY`.
4. **Долгоработающие приложения** — создание видео может занять 30–120 секунд. Тайм-аут инструмента терминала должен быть достаточным, но предупредите пользователя, что это может занять некоторое время.
5. **Формат ввода** — флаг `--input` принимает строку JSON. Убедитесь, что кавычки правильно экранированы.

## Справочная документация

- `references/authentication.md` — Настройка, вход в систему, ключи API
- `references/app-discovery.md` — Поиск и просмотр каталога приложений.
- `references/running-apps.md` — Запуск приложений, форматы ввода, обработка вывода.
- `references/cli-reference.md` — Полный справочник команд CLI.