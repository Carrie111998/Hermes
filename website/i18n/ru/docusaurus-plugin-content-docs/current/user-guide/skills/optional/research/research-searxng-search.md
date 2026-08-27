---
title: Searxng Search — бесплатный метапоиск без ключа, объединяющий более 70 систем.
sidebar_label: Searxng Search
description: Бесплатный метапоиск без ключа, объединяющий более 70 систем
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Поиск Поиск

Бесплатный метапоиск без ключа, объединяющий более 70 систем.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/research/searxng-search` |
| Путь | `optional-skills/research/searxng-search` |
| Версия | `1.0.1` |
| Автор | Гермес-агент |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS |
| Теги | `search`, `searxng`, `meta-search`, `self-hosted`, `free`, `fallback` |
| Сопутствующие навыки | [`duckduckgo-search`](/docs/user-guide/skills/optional/research/research-duckduckgo-search), [`domain-intel`](/docs/user-guide/skills/optional/research/research-domain-intel) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# ИскатьXNG Поиск

Бесплатный мета-поиск с использованием [SearXNG](https://searxng.org/) — автономного поискового агрегатора, уважающего конфиденциальность, который одновременно запрашивает более 70 поисковых систем.

**Ключ API не требуется** при использовании общедоступного экземпляра. Также может быть размещен самостоятельно для полного контроля. Автоматически появляется в качестве запасного варианта, если основной набор инструментов веб-поиска (`FIRECRAWL_API_KEY`) не настроен.

## Конфигурация

Для SearXNG требуется переменная среды `SEARXNG_URL`, указывающая на ваш экземпляр SearXNG:

```bash
# Public instances (no setup required)
SEARXNG_URL=https://searxng.example.com

# Self-hosted SearXNG
SEARXNG_URL=http://localhost:8888
```

Если ни один экземпляр не настроен, этот навык недоступен, и агент возвращается к другим параметрам поиска.

## Порядок обнаружения

Прежде чем выбирать подход, проверьте, что действительно доступно:

```bash
# Check if SEARXNG_URL is set and the instance is reachable
curl -s --max-time 5 "${SEARXNG_URL}/search?q=test&format=json" | head -c 200
```

Дерево решений:
1. Если `SEARXNG_URL` установлен и экземпляр отвечает, используйте SearXNG.
2. Если `SEARXNG_URL` не установлен или недоступен, вернитесь к другим доступным инструментам поиска.
3. Если пользователю нужен именно SearXNG, помогите ему настроить экземпляр или найти общедоступный.

## Способ 1: CLI через Curl (предпочтительно)

Используйте `curl` через `terminal` для вызова API SearXNG JSON. Это позволяет избежать предположения, что установлен какой-либо конкретный пакет Python.

```bash
# Text search (JSON output)
curl -s --max-time 10 \
  "${SEARXNG_URL}/search?q=python+async+programming&format=json&engines=google,bing&limit=10"

# With Safesearch off
curl -s --max-time 10 \
  "${SEARXNG_URL}/search?q=example&format=json&safesearch=0"

# Specific categories (general, news, science, etc.)
curl -s --max-time 10 \
  "${SEARXNG_URL}/search?q=AI+news&format=json&categories=news"
```

### Общие флаги CLI

| Флаг | Описание | Пример |
|------|-------------|---------|
| `q` | Строка запроса (в URL-кодировке) | `q=python+async` |
| `format` | Формат вывода: `json`, `csv`, `rss` | `format=json` |
| `engines` | Названия двигателей, разделенные запятыми | `engines=google,bing,ddg` |
| `limit` | Максимальное количество результатов для каждого механизма (по умолчанию 10) | `limit=5` |
| `categories` | Фильтровать по категориям | `categories=news,science` |
| `safesearch` | 0 = нет, 1 = умеренный, 2 = строгий | `safesearch=0` |
| `time_range` | Фильтр: `day`, `week`, `month`, `year` | `time_range=week` |

### Анализ результатов JSON

```bash
# Extract titles and URLs from JSON
curl -s --max-time 10 "${SEARXNG_URL}/search?q=fastapi&format=json&limit=5" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in data.get('results', []):
    print(r.get('title',''))
    print(r.get('url',''))
    print(r.get('content','')[:200])
    print()
"
```

Возвращает результат: `title`, `url`, `content` (фрагмент), `engine`, `parsed_url`, `img_src`, `thumbnail`, `author`, `published_date`.

## Способ 2: API Python через `requests`

Используйте REST API SearXNG непосредственно из Python с библиотекой `requests`:

```python
import os, requests, urllib.parse

base_url = os.environ.get("SEARXNG_URL", "")
if not base_url:
    raise RuntimeError("SEARXNG_URL is not set")

query = "fastapi deployment guide"
params = {
    "q": query,
    "format": "json",
    "limit": 5,
    "engines": "google,bing",
}

resp = requests.get(f"{base_url}/search", params=params, timeout=10)
resp.raise_for_status()
data = resp.json()

for r in data.get("results", []):
    print(r["title"])
    print(r["url"])
    print(r.get("content", "")[:200])
    print()
```

## Самостоятельный хостинг SearXNG

Чтобы запустить собственный экземпляр SearXNG:

```bash
# Using Docker
docker run -d -p 8888:8080 \
  -v $(pwd)/searxng:/etc/searxng \
  searxng/searxng:latest

# Then set
SEARXNG_URL=http://localhost:8888
```

Или установите через pip:
```bash
pip install searxng
# Edit /etc/searxng/settings.yml
searxng-run
```

Публичные экземпляры SearXNG доступны по адресу:
- `https://searxng.example.com` (замените любым общедоступным экземпляром)

## Рабочий процесс: поиск и извлечение

SearXNG возвращает заголовки, URL-адреса и фрагменты, а не полное содержимое страницы. Чтобы получить полное содержимое страницы, сначала выполните поиск, а затем извлеките наиболее релевантный URL-адрес с помощью `web_extract`, инструментов браузера или `curl`.

```bash
# Search for relevant pages
curl -s "${SEARXNG_URL}/search?q=fastapi+deployment&format=json&limit=3"
# Output: list of results with titles and URLs

# Then extract the best URL with web_extract
```

## Ограничения

- **Доступность экземпляра**: если экземпляр SearXNG не работает или недоступен, поиск завершается неудачно. Всегда проверяйте, что `SEARXNG_URL` установлен и экземпляр доступен.
- **Без извлечения контента**: SearXNG возвращает фрагменты, а не полное содержимое страницы. Используйте `web_extract`, инструменты браузера или `curl` для просмотра полных статей.
- **Ограничение скорости**: некоторые общедоступные экземпляры ограничивают количество запросов. Самостоятельный хостинг позволяет избежать этого.
- **Охват механизма**: доступные механизмы зависят от конфигурации экземпляра SearXNG. Некоторые двигатели могут быть отключены.
- **Свежесть результатов**: метапоиск агрегирует внешние системы — свежесть результатов зависит от этих систем.

## Устранение неполадок

| Проблема | Вероятная причина | Что делать |
|---------|--------------|------------|
| `SEARXNG_URL` не установлен | Экземпляр не настроен | Используйте общедоступный экземпляр SearXNG или создайте свой собственный |
| В соединении отказано | Экземпляр не запущен или неправильный URL | Убедитесь, что URL-адрес правильный и экземпляр запущен |
| Пустые результаты | Экземпляр блокирует запрос | Попробуйте другой экземпляр или самостоятельный хостинг |
| Медленные ответы | Публичный экземпляр под нагрузкой | Самостоятельное размещение или использование менее загруженного общедоступного экземпляра |
| Формат `json` не поддерживается | Старая версия SearXNG | Попробуйте `format=rss` или обновите SearXNG |

## Подводные камни

- **Всегда устанавливайте `SEARXNG_URL`**: без него навык не сможет работать.
- **Запросы с URL-кодированием**. Пробелы и специальные символы должны быть закодированы в URL-адресе в Curl или использовать `urllib.parse.quote()` в Python.
- **Используйте `format=json`**: формат по умолчанию может быть несовместим с машинным чтением. Всегда запрашивайте JSON явно.
- **Установите тайм-аут**: всегда используйте `--max-time` или `timeout=`, чтобы избежать зависания на недоступных экземплярах.
- **Лучше всего использовать самостоятельный хостинг**: общедоступные экземпляры могут отключаться, ограничиваться скоростью или блокироваться. Самостоятельный экземпляр надежен.

## Обнаружение экземпляра

Если `SEARXNG_URL` не установлен и пользователь спрашивает о SearXNG, помогите ему:
1. Найдите общедоступный экземпляр SearXNG (найдите «публичный экземпляр searchxng»).
2. Настройте самостоятельно с помощью Docker или pip.

Публичные экземпляры перечислены по адресу: https://searxng.org/.