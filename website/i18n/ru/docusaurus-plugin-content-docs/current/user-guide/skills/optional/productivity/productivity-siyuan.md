---
title: Сиюань — Запрос и редактирование базы знаний Сиюань через API.
sidebar_label: Siyuan
description: Запрашивайте и редактируйте базу знаний SiYuan через API.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Сиюань

Запрашивайте и редактируйте базу знаний SiYuan через API.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/productivity/siyuan` |
| Путь | `optional-skills/productivity/siyuan` |
| Версия | `1.0.0` |
| Автор | ФЕАЗЮР |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `SiYuan`, `Notes`, `Knowledge Base`, `PKM`, `API` |
| Сопутствующие навыки | [`obsidian`](/docs/user-guide/skills/bundled/booking/productivity-obsidian), [`notion`](/docs/user-guide/skills/bundled/productivity/productivity-notion) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# API заметок СиЮань

Используйте API ядра [SiYuan](https://github.com/siyuan-note/siyuan) через Curl для поиска, чтения, создания, обновления и удаления блоков и документов в локальной базе знаний. Никаких дополнительных инструментов не требуется — просто Curl и токен API.

## Предварительные условия

1. Установите и запустите SiYuan (рабочий стол или Docker)
2. Получите свой токен API: **Настройки > О программе > Токен API**
3. Сохраните его в `${HERMES_HOME:-~/.hermes}/.env`:
   ```
   SIYUAN_TOKEN=your_token_here
   SIYUAN_URL=http://127.0.0.1:6806
   ```
   `SIYUAN_URL` по умолчанию имеет значение `http://127.0.0.1:6806`, если не установлено.

## Основы API

Все вызовы API SiYuan выполняются **POST с телом JSON**. Каждый запрос следует этому шаблону:

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/..." \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"param": "value"}'
```

Ответы представляют собой JSON со следующей структурой:
```json
{"code": 0, "msg": "", "data": { ... }}
```
`code: 0` означает успех. Любое другое значение является ошибкой. Подробности см. в `msg`.

**Формат идентификатора:** Идентификаторы SiYuan выглядят как `20210808180117-6v0mkxr` (14-значная отметка времени + 7 буквенно-цифровых символов).

## Краткий справочник

| Операция | Конечная точка |
|-----------|----------|
| Полнотекстовый поиск | `/api/search/fullTextSearchBlock` |
| SQL-запрос | `/api/query/sql` |
| Читать блок | `/api/block/getBlockKramdown` |
| Читать детям | `/api/block/getChildBlocks` |
| Получить путь | `/api/filetree/getHPathByID` |
| Получить атрибуты | `/api/attr/getBlockAttrs` |
| Список ноутбуков | `/api/notebook/lsNotebooks` |
| Список документов | `/api/filetree/listDocsByPath` |
| Создать блокнот | `/api/notebook/createNotebook` |
| Создать документ | `/api/filetree/createDocWithMd` |
| Добавить блок | `/api/block/appendBlock` |
| Обновление блока | `/api/block/updateBlock` |
| Переименовать документ | `/api/filetree/renameDocByID` |
| Установить атрибуты | `/api/attr/setBlockAttrs` |
| Удалить блок | `/api/block/deleteBlock` |
| Удалить документ | `/api/filetree/removeDocByID` |
| Экспортировать как Markdown | `/api/export/exportMdContent` |

## Общие операции

### Поиск (полнотекстовый)

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/search/fullTextSearchBlock" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "meeting notes", "page": 0}' | jq '.data.blocks[:5]'
```

### Поиск (SQL)

Запросите базу данных блоков напрямую. Только операторы SELECT безопасны.

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/query/sql" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"stmt": "SELECT id, content, type, box FROM blocks WHERE content LIKE '\''%keyword%'\'' AND type='\''p'\'' LIMIT 20"}' | jq '.data'
```

Полезные столбцы: `id`, `parent_id`, `root_id`, `box` (идентификатор блокнота), `path`, `content`, `type`, `subtype`, `created`, `updated`.

### Чтение содержимого блока

Возвращает содержимое блока в формате Kramdown (подобном Markdown).

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/block/getBlockKramdown" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id": "20210808180117-6v0mkxr"}' | jq '.data.kramdown'
```

### Чтение дочерних блоков

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/block/getChildBlocks" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id": "20210808180117-6v0mkxr"}' | jq '.data'
```

### Получить удобочитаемый путь

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/filetree/getHPathByID" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id": "20210808180117-6v0mkxr"}' | jq '.data'
```

### Получить атрибуты блока

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/attr/getBlockAttrs" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id": "20210808180117-6v0mkxr"}' | jq '.data'
```

### Список блокнотов

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/notebook/lsNotebooks" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' | jq '.data.notebooks[] | {id, name, closed}'
```

### Список документов в блокноте

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/filetree/listDocsByPath" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"notebook": "NOTEBOOK_ID", "path": "/"}' | jq '.data.files[] | {id, name}'
```

### Создать документ

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/filetree/createDocWithMd" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "notebook": "NOTEBOOK_ID",
    "path": "/Meeting Notes/2026-03-22",
    "markdown": "# Meeting Notes\n\n- Discussed project timeline\n- Assigned tasks"
  }' | jq '.data'
```

### Создать блокнот

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/notebook/createNotebook" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "My New Notebook"}' | jq '.data.notebook.id'
```

### Добавить блок в документ

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/block/appendBlock" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "parentID": "DOCUMENT_OR_BLOCK_ID",
    "data": "New paragraph added at the end.",
    "dataType": "markdown"
  }' | jq '.data'
```

Также доступны: `/api/block/prependBlock` (те же параметры, вставки в начале) и `/api/block/insertBlock` (используется `previousID` вместо `parentID` для вставки после определенного блока).

### Обновить содержимое блока

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/block/updateBlock" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "BLOCK_ID",
    "data": "Updated content here.",
    "dataType": "markdown"
  }' | jq '.data'
```

### Переименование документа

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/filetree/renameDocByID" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id": "DOCUMENT_ID", "title": "New Title"}'
```

### Установка атрибутов блока

Пользовательские атрибуты должны иметь префикс `custom-`:

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/attr/setBlockAttrs" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "BLOCK_ID",
    "attrs": {
      "custom-status": "reviewed",
      "custom-priority": "high"
    }
  }'
```

### Удалить блок

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/block/deleteBlock" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id": "BLOCK_ID"}'
```

Чтобы удалить весь документ: используйте `/api/filetree/removeDocByID` с `{"id": "DOC_ID"}`.
Чтобы удалить блокнот: используйте `/api/notebook/removeNotebook` с `{"notebook": "NOTEBOOK_ID"}`.

### Экспорт документа в формате Markdown

```bash
curl -s -X POST "${SIYUAN_URL:-http://127.0.0.1:6806}/api/export/exportMdContent" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id": "DOCUMENT_ID"}' | jq -r '.data.content'
```

## Типы блоков

Общие значения `type` в запросах SQL:

| Тип | Описание |
|------|-------------|
| `d` | Документ (корневой блок) |
| `p` | Параграф |
| `h` | Рубрика |
| `l` | Список |
| `i` | Элемент списка |
| `c` | Кодовый блок |
| `m` | Математический блок |
| `t` | Стол |
| `b` | Цитата |
| `s` | Суперблок |
| `html` | HTML-блок |

## Подводные камни

- **Все конечные точки являются POST**, даже операции только для чтения. Не используйте GET.
- **Безопасность SQL**: используйте только запросы SELECT. INSERT/UPDATE/DELETE/DROP опасны и никогда не должны отправляться.
- **Проверка идентификатора**: идентификаторы соответствуют шаблону `YYYYMMDDHHmmss-xxxxxxx`. Отклоняйте что-либо еще.
- **Ответы об ошибках**: всегда проверяйте `code != 0` в ответах перед обработкой `data`.
- **Большие документы**: содержимое блоков и результаты экспорта могут быть очень большими. Используйте `LIMIT` в SQL и пропустите через `jq`, чтобы извлечь только то, что вам нужно.
- **Идентификаторы ноутбуков**: при работе с конкретным блокнотом сначала получите его идентификатор через `lsNotebooks`.

## Альтернатива: MCP-сервер

Если вы предпочитаете встроенную интеграцию вместо Curl, установите сервер SiYuan MCP:

```yaml
# In ~/.hermes/config.yaml under mcp_servers:
mcp_servers:
  siyuan:
    command: npx
    args: ["-y", "@porkll/siyuan-mcp"]
    env:
      SIYUAN_TOKEN: "your_token"
      SIYUAN_URL: "http://127.0.0.1:6806"
```