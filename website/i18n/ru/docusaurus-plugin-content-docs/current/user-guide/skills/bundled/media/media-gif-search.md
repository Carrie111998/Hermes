---
title: Поиск GIF — поиск/загрузка GIF-файлов с Tenor через Curl + jq
sidebar_label: Gif Search
description: Найдите/загрузите GIF-файлы Tenor через Curl + jq
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Поиск GIF

Найдите/загрузите GIF-файлы Tenor с помощью Curl + JQ.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/media/gif-search` |
| Версия | `1.1.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `GIF`, `Media`, `Search`, `Tenor`, `API` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Поиск GIF (Tenor API)

Ищите и загружайте GIF-файлы напрямую через Tenor API с помощью Curl. Никаких дополнительных инструментов не требуется.

## Когда использовать

Полезно для поиска GIF-изображений с реакциями, создания визуального контента и отправки GIF-файлов в чат.

## Настройка

Установите ключ Tenor API в своей среде (добавьте в `${HERMES_HOME:-~/.hermes}/.env`):

```bash
TENOR_API_KEY=your_key_here
```

Получите бесплатный ключ API по адресу https://developers.google.com/tenor/guides/quickstart — ключ API Tenor консоли Google Cloud бесплатен и имеет щедрые ограничения по скорости.

## Предварительные условия

- `curl` и `jq` (оба являются стандартными для macOS/Linux)
- `TENOR_API_KEY` переменная среды

## Поиск GIF-файлов

```bash
# Search and get GIF URLs
curl -s "https://tenor.googleapis.com/v2/search?q=thumbs+up&limit=5&key=${TENOR_API_KEY}" | jq -r '.results[].media_formats.gif.url'

# Get smaller/preview versions
curl -s "https://tenor.googleapis.com/v2/search?q=nice+work&limit=3&key=${TENOR_API_KEY}" | jq -r '.results[].media_formats.tinygif.url'
```

## Загрузить GIF

```bash
# Search and download the top result
URL=$(curl -s "https://tenor.googleapis.com/v2/search?q=celebration&limit=1&key=${TENOR_API_KEY}" | jq -r '.results[0].media_formats.gif.url')
curl -sL "$URL" -o celebration.gif
```

## Получить полные метаданные

```bash
curl -s "https://tenor.googleapis.com/v2/search?q=cat&limit=3&key=${TENOR_API_KEY}" | jq '.results[] | {title: .title, url: .media_formats.gif.url, preview: .media_formats.tinygif.url, dimensions: .media_formats.gif.dims}'
```

## Параметры API

| Параметр | Описание |
|-----------|-------------|
| `q` | Поисковый запрос (пробелы в URL-адресе кодируются как `+`) |
| `limit` | Максимальное количество результатов (1–50, по умолчанию 20) |
| `key` | Ключ API (из `$TENOR_API_KEY` env var) |
| `media_filter` | Форматы фильтров: `gif`, `tinygif`, `mp4`, `tinymp4`, `webm` |
| `contentfilter` | Безопасность: `off`, `low`, `medium`, `high` |
| `locale` | Язык: `en_US`, `es`, `fr` и т. д. |

## Доступные форматы мультимедиа

Каждый результат имеет несколько форматов под `.media_formats`:

| Формат | Вариант использования |
|--------|----------|
| `gif` | GIF-изображения в полном качестве |
| `tinygif` | Небольшой превью GIF |
| `mp4` | Видеоверсия (меньший размер файла) |
| `tinymp4` | Небольшой превью-видео |
| `webm` | ВебМ-видео |
| `nanogif` | Маленькая миниатюра |

## Примечания

– URL-кодирование запроса: пробелы как `+`, специальные символы как `%XX`.
– Для отправки в чат `tinygif` URL имеют более легкий вес.
- URL-адреса GIF можно использовать непосредственно в уценке: `![alt](https://github.com/NousResearch/hermes-agent/blob/main/skills/media/gif-search/url)`.