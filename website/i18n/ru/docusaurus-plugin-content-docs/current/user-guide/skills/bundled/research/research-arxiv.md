---
title: Arxiv — поиск статей arXiv по ключевому слову, автору, категории или идентификатору.
sidebar_label: Arxiv
description: Поиск статей arXiv по ключевому слову, автору, категории или идентификатору
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Арксив

Выполняйте поиск статей arXiv по ключевому слову, автору, категории или идентификатору.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/research/arxiv` |
| Версия | `1.0.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Research`, `Arxiv`, `Papers`, `Academic`, `Science`, `API` |
| Сопутствующие навыки | [`ocr-and-documents`](/docs/user-guide/skills/bundled/productivity/productivity-ocr-and-documents) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# исследование arXiv

Ищите и получайте научные статьи из arXiv через бесплатный REST API. Никакого API-ключа, никаких зависимостей — просто завивайте.

## Краткий справочник

| Действие | Команда |
|--------|---------|
| Поиск документов | `curl "https://export.arxiv.org/api/query?search_query=all:QUERY&max_results=5"` |
| Получите конкретную статью | `curl "https://export.arxiv.org/api/query?id_list=2402.03300"` |
| Читать аннотацию (Интернет) | `web_extract(urls=["https://arxiv.org/abs/2402.03300"])` |
| Читать статью целиком (PDF) | `web_extract(urls=["https://arxiv.org/pdf/2402.03300"])` |

## Поиск документов

API возвращает Atom XML. Выполните анализ с помощью `grep`/`sed` или пропустите через `python3` для получения чистого результата.

### Базовый поиск

```bash
curl -s "https://export.arxiv.org/api/query?search_query=all:GRPO+reinforcement+learning&max_results=5"
```

### Чистый вывод (преобразование XML в читаемый формат)

```bash
curl -s "https://export.arxiv.org/api/query?search_query=all:GRPO+reinforcement+learning&max_results=5&sortBy=submittedDate&sortOrder=descending" | python3 -c "
import sys, xml.etree.ElementTree as ET
ns = {'a': 'http://www.w3.org/2005/Atom'}
root = ET.parse(sys.stdin).getroot()
for i, entry in enumerate(root.findall('a:entry', ns)):
    title = entry.find('a:title', ns).text.strip().replace('\n', ' ')
    arxiv_id = entry.find('a:id', ns).text.strip().split('/abs/')[-1]
    published = entry.find('a:published', ns).text[:10]
    authors = ', '.join(a.find('a:name', ns).text for a in entry.findall('a:author', ns))
    summary = entry.find('a:summary', ns).text.strip()[:200]
    cats = ', '.join(c.get('term') for c in entry.findall('a:category', ns))
    print(f'{i+1}. [{arxiv_id}] {title}')
    print(f'   Authors: {authors}')
    print(f'   Published: {published} | Categories: {cats}')
    print(f'   Abstract: {summary}...')
    print(f'   PDF: https://arxiv.org/pdf/{arxiv_id}')
    print()
"
```

## Синтаксис поискового запроса

| Префикс | Поиски | Пример |
|--------|----------|---------|
| `all:` | Все поля | `all:transformer+attention` |
| `ti:` | Название | `ti:large+language+models` |
| `au:` | Автор | `au:vaswani` |
| `abs:` | Аннотация | `abs:reinforcement+learning` |
| `cat:` | Категория | `cat:cs.AI` |
| `co:` | Комментарий | `co:accepted+NeurIPS` |

### Булевы операторы

```
# AND (default when using +)
search_query=all:transformer+attention

# OR
search_query=all:GPT+OR+all:BERT

# AND NOT
search_query=all:language+model+ANDNOT+all:vision

# Exact phrase
search_query=ti:"chain+of+thought"

# Combined
search_query=au:hinton+AND+cat:cs.LG
```

## Сортировка и нумерация страниц

| Параметр | Опции |
|-----------|---------|
| `sortBy` | `relevance`, `lastUpdatedDate`, `submittedDate` |
| `sortOrder` | `ascending`, `descending` |
| `start` | Смещение результата (отсчет от 0) |
| `max_results` | Количество результатов (по умолчанию 10, максимум 30 000) |

```bash
# Latest 10 papers in cs.AI
curl -s "https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=10"
```

## Получение определенных документов

```bash
# By arXiv ID
curl -s "https://export.arxiv.org/api/query?id_list=2402.03300"

# Multiple papers
curl -s "https://export.arxiv.org/api/query?id_list=2402.03300,2401.12345,2403.00001"
```

## Генерация BibTeX

После получения метаданных для статьи создайте запись BibTeX:

&#123;% сырой %&#125;
```bash
curl -s "https://export.arxiv.org/api/query?id_list=1706.03762" | python3 -c "
import sys, xml.etree.ElementTree as ET
ns = {'a': 'http://www.w3.org/2005/Atom', 'arxiv': 'http://arxiv.org/schemas/atom'}
root = ET.parse(sys.stdin).getroot()
entry = root.find('a:entry', ns)
if entry is None: sys.exit('Paper not found')
title = entry.find('a:title', ns).text.strip().replace('\n', ' ')
authors = ' and '.join(a.find('a:name', ns).text for a in entry.findall('a:author', ns))
year = entry.find('a:published', ns).text[:4]
raw_id = entry.find('a:id', ns).text.strip().split('/abs/')[-1]
cat = entry.find('arxiv:primary_category', ns)
primary = cat.get('term') if cat is not None else 'cs.LG'
last_name = entry.find('a:author', ns).find('a:name', ns).text.split()[-1]
print(f'@article{{{last_name}{year}_{raw_id.replace(\".\", \"\")},')
print(f'  title     = {{{title}}},')
print(f'  author    = {{{authors}}},')
print(f'  year      = {{{year}}},')
print(f'  eprint    = {{{raw_id}}},')
print(f'  archivePrefix = {{arXiv}},')
print(f'  primaryClass  = {{{primary}}},')
print(f'  url       = {{https://arxiv.org/abs/{raw_id}}}')
print('}')
"
```
&#123;% endraw %&#125;

## Чтение содержания бумаги

Найдя бумагу, прочитайте ее:

```
# Abstract page (fast, metadata + abstract)
web_extract(urls=["https://arxiv.org/abs/2402.03300"])

# Full paper (PDF → markdown via Firecrawl)
web_extract(urls=["https://arxiv.org/pdf/2402.03300"])
```

Для локальной обработки PDF см. навык `ocr-and-documents`.

## Общие категории

| Категория | Поле |
|----------|-------|
| `cs.AI` | Искусственный интеллект |
| `cs.CL` | Вычисления и язык (НЛП) |
| `cs.CV` | Компьютерное зрение |
| `cs.LG` | Машинное обучение |
| `cs.CR` | Криптография и безопасность |
| `stat.ML` | Машинное обучение (статистика) |
| `math.OC` | Оптимизация и контроль |
| `physics.comp-ph` | Вычислительная физика |

Полный список: https://arxiv.org/category_taxonomy.

## Вспомогательный скрипт

Скрипт `scripts/search_arxiv.py` обрабатывает синтаксический анализ XML и обеспечивает чистый вывод:

```bash
python scripts/search_arxiv.py "GRPO reinforcement learning"
python scripts/search_arxiv.py "transformer attention" --max 10 --sort date
python scripts/search_arxiv.py --author "Yann LeCun" --max 5
python scripts/search_arxiv.py --category cs.AI --sort date
python scripts/search_arxiv.py --id 2402.03300
python scripts/search_arxiv.py --id 2402.03300,2401.12345
```

Никаких зависимостей — используется только стандартная библиотека Python.

---

## Семантический ученый (цитаты, статьи по теме, профили авторов)

arXiv не предоставляет данные цитирования или рекомендации. Для этого используйте **Semantic Scholar API** — бесплатно, для базового использования ключ не требуется (1 запрос в секунду), возвращает JSON.

### Получите подробную информацию о статье и цитаты

```bash
# By arXiv ID
curl -s "https://api.semanticscholar.org/graph/v1/paper/arXiv:2402.03300?fields=title,authors,citationCount,referenceCount,influentialCitationCount,year,abstract" | python3 -m json.tool

# By Semantic Scholar paper ID or DOI
curl -s "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1234/example?fields=title,citationCount"
```

### Получить цитаты из статьи (кто ее цитировал)

```bash
curl -s "https://api.semanticscholar.org/graph/v1/paper/arXiv:2402.03300/citations?fields=title,authors,year,citationCount&limit=10" | python3 -m json.tool
```

### Получить ссылки ИЗ статьи (то, что в ней цитируется)

```bash
curl -s "https://api.semanticscholar.org/graph/v1/paper/arXiv:2402.03300/references?fields=title,authors,year,citationCount&limit=10" | python3 -m json.tool
```

### Поиск документов (альтернатива поиску в arXiv, возвращает JSON)

```bash
curl -s "https://api.semanticscholar.org/graph/v1/paper/search?query=GRPO+reinforcement+learning&limit=5&fields=title,authors,year,citationCount,externalIds" | python3 -m json.tool
```

### Получите рекомендации по работе с бумагами

```bash
curl -s -X POST "https://api.semanticscholar.org/recommendations/v1/papers/" \
  -H "Content-Type: application/json" \
  -d '{"positivePaperIds": ["arXiv:2402.03300"], "negativePaperIds": []}' | python3 -m json.tool
```

### Профиль автора

```bash
curl -s "https://api.semanticscholar.org/graph/v1/author/search?query=Yann+LeCun&fields=name,hIndex,citationCount,paperCount" | python3 -m json.tool
```

### Полезные поля Semantic Scholar

`title`, `authors`, `year`, `abstract`, `citationCount`, `referenceCount`, `influentialCitationCount`, `isOpenAccess`, `openAccessPdf`, `fieldsOfStudy`, `publicationVenue`, `externalIds` (содержит arXiv ID, DOI и т. д.)

---

## Полный рабочий процесс исследования

1. **Откройте для себя**: `python scripts/search_arxiv.py "your topic" --sort date --max 10`
2. **Оценить воздействие**: `curl -s "https://api.semanticscholar.org/graph/v1/paper/arXiv:ID?fields=citationCount,influentialCitationCount"`
3. **Читать аннотацию**: `web_extract(urls=["https://arxiv.org/abs/ID"])`
4. **Читать статью**: `web_extract(urls=["https://arxiv.org/pdf/ID"])`
5. **Найти похожую работу**: `curl -s "https://api.semanticscholar.org/graph/v1/paper/arXiv:ID/references?fields=title,citationCount&limit=20"`
6. **Получить рекомендации**: конечная точка POST для рекомендаций Semantic Scholar.
7. **Отслеживать авторов**: `curl -s "https://api.semanticscholar.org/graph/v1/author/search?query=NAME"`

## Ограничения скорости

| API | Оценить | Авторизация |
|-----|------|------|
| arXiv | ~1 запрос/3 секунды | Ничего не нужно |
| Семантический ученый | 1 запрос в секунду | Нет (100/сек с ключом API) |

## Примечания

- arXiv возвращает Atom XML — используйте вспомогательный скрипт или фрагмент синтаксического анализа для получения чистого вывода.
— Semantic Scholar возвращает JSON — пропустите через `python3 -m json.tool` для удобства чтения.
- Идентификаторы arXiv: старый формат (`hep-th/0601001`) и новый (`2402.03300`)
- PDF: `https://arxiv.org/pdf/{id}` — Аннотация: `https://arxiv.org/abs/{id}`
– HTML (если доступен): `https://arxiv.org/html/{id}`
– Для локальной обработки PDF см. навык `ocr-and-documents`.

## Управление версиями идентификатора

- `arxiv.org/abs/1706.03762` всегда разрешается до **последней** версии.
- `arxiv.org/abs/1706.03762v1` указывает на **конкретную** неизменяемую версию.
- При создании цитат сохраняйте суффикс версии, которую вы фактически прочитали, чтобы предотвратить дрейф цитирования (более поздняя версия может существенно изменить содержание).
– Поле API `<id>` возвращает URL с версией (например, `http://arxiv.org/abs/1706.03762v7`).

## Отозванные документы

После подачи документы могут быть отозваны. Когда это произойдет:
– Поле `<summary>` содержит уведомление об отзыве (ищите «отозвано» или «отозвано»).
- Поля метаданных могут быть неполными.
- Всегда проверяйте сводку, прежде чем рассматривать результат как действительный документ.