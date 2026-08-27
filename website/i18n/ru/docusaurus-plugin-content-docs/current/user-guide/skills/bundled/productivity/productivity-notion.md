---
title: 'Notion — Notion API + ntn CLI: страницы, базы данных, уценка, воркеры'
sidebar_label: Notion
description: 'Notion API + ntn CLI: страницы, базы данных, уценка, воркеры'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Понятие

Notion API + ntn CLI: страницы, базы данных, уценка, Workers.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/productivity/notion` |
| Версия | `2.0.0` |
| Автор | сообщество |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Notion`, `Productivity`, `Notes`, `Database`, `API`, `CLI`, `Workers` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Понятие

Поговорите с Notion двумя способами. Один и тот же токен интеграции работает для обоих — выбирайте из того, что доступно.

◆ **`ntn` CLI** — официальный интерфейс командной строки Notion. Более короткий синтаксис, загрузка файлов в одну строку, необходимая для Workers. macOS + Linux только с мая 2026 г. (поддержка Windows появится «скоро»). **По умолчанию при установке.**
◆ **HTTP + Curl** — работает везде, включая Windows. **Резервный вариант по умолчанию**, если `ntn` не установлен.

## Настройка

### 1. Получите токен интеграции (требуется для обоих путей)

1. Создайте интеграцию на странице https://notion.so/my-integrations.
2. Скопируйте ключ API (начинается с `ntn_` или `secret_`).
3. Сохраните в `${HERMES_HOME:-~/.hermes}/.env`:
   ```
   NOTION_API_KEY=ntn_your_key_here
   ```
4. **Поделитесь целевыми страницами/базами данных с интеграцией** в Notion: меню страницы `...` → `Connect to` → имя вашей интеграции. Без этого API возвращает 404 для этой страницы, даже если она существует.

### 2. Установите `ntn` (предпочтительный путь в macOS/Linux)

```bash
# Recommended
curl -fsSL https://ntn.dev | bash

# Or via npm (needs Node 22+, npm 10+)
npm install --global ntn

ntn --version    # verify
```

**Пропустите `ntn login` — вместо этого используйте токен интеграции.** Это работает без головы, браузер не требуется:
```bash
export NOTION_API_TOKEN=$NOTION_API_KEY      # ntn reads NOTION_API_TOKEN
export NOTION_KEYRING=0                       # don't try to use the OS keychain
```

Добавьте эти экспорты в свой профиль оболочки (или в `${HERMES_HOME:-~/.hermes}/.env`), чтобы каждый сеанс наследовал их.

### 3. Выбор пути во время выполнения

```bash
if command -v ntn >/dev/null 2>&1; then
  # use ntn
else
  # fall back to curl
fi
```

Пользователи Windows: полностью пропустите шаг 2, пока не будет выпущен собственный `ntn` — путь B работает нормально. Если вам нужна эргономика CLI сейчас, установите `ntn` внутри WSL2.

## Основы API

`Notion-Version: 2025-09-03` требуется для всех HTTP-запросов. `ntn` сделает это за вас. В этой версии то, что пользователи называют «базами данных», в API называется **источниками данных**.

## Путь A — `ntn` CLI (предпочтительно, macOS/Linux)

### Необработанные вызовы API (сокращение от Curl)
```bash
ntn api v1/users                                  # GET
ntn api v1/pages parent[page_id]=abc123 \         # POST with inline body
  properties[title][0][text][content]="Notes"
ntn api v1/pages/abc123 -X PATCH archived:=true   # PATCH; := is non-string (bool/num/null)
```

Синтаксические примечания:
- `key=value` — строковые поля
- `key[nested]=value` — поля вложенных объектов.
- `key:=value` — типизированное присваивание (логические значения, числа, ноль, массивы)

### Поиск
```bash
ntn api v1/search query="page title"
```

### Чтение метаданных страницы
```bash
ntn api v1/pages/{page_id}
```

### Читать страницу как Markdown (удобно для агентов)
```bash
ntn api v1/pages/{page_id}/markdown
```

### Чтение содержимого страницы в виде блоков
```bash
ntn api v1/blocks/{page_id}/children
```

### Создать страницу из Markdown
```bash
ntn api v1/pages \
  parent[page_id]=xxx \
  properties[title][0][text][content]="Notes from meeting" \
  markdown="# Agenda

- Q3 roadmap
- Hiring"
```

### Исправьте страницу с помощью Markdown
```bash
ntn api v1/pages/{page_id}/markdown -X PATCH \
  markdown="## Update

Shipped the prototype."
```

### Запрос к базе данных (источнику данных)
```bash
ntn api v1/data_sources/{data_source_id}/query -X POST \
  filter[property]=Status filter[select][equals]=Active
```

Для сложных запросов с `sorts`, несколькими предложениями фильтра или составной логикой передавайте JSON по каналу:
```bash
echo '{"filter": {"property": "Status", "select": {"equals": "Active"}}, "sorts": [{"property": "Date", "direction": "descending"}]}' | \
  ntn api v1/data_sources/{data_source_id}/query -X POST --json -
```

### Загрузка файлов (однострочная — самая большая победа в CLI)
```bash
ntn files create < photo.png
ntn files create --external-url https://example.com/photo.png
ntn files list
```

Сравните с трехэтапным потоком HTTP (создать загрузку → PUT байты → ссылка).

### Полезные переменные окружения
| Вар | Эффект |
|---|---|
| `NOTION_API_TOKEN` | Токен аутентификации (переопределяет связку ключей) — установите для этого токена интеграции |
| `NOTION_KEYRING=0` | Файловые учетные данные по адресу `~/.config/notion/auth.json` вместо цепочки ключей ОС |
| `NOTION_WORKSPACE_ID` | Пропустить подсказку выбора рабочего пространства |

## Путь B — HTTP + Curl (кроссплатформенный, по умолчанию в Windows)

Все запросы имеют следующий шаблон:

```bash
curl -s -X GET "https://api.notion.com/v1/..." \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json"
```

В Windows `curl`, поставляемый с Windows 10+, работает как есть. Пользователи PowerShell также могут использовать `Invoke-RestMethod`.

### Поиск
```bash
curl -s -X POST "https://api.notion.com/v1/search" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"query": "page title"}'
```

### Чтение метаданных страницы
```bash
curl -s "https://api.notion.com/v1/pages/{page_id}" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03"
```

### Читать страницу как Markdown (удобно для агентов)

Легче передать модель, чем блокировать JSON.

```bash
curl -s "https://api.notion.com/v1/pages/{page_id}/markdown" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03"
```

### Чтение содержимого страницы в виде блоков (когда вам нужна структура)
```bash
curl -s "https://api.notion.com/v1/blocks/{page_id}/children" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03"
```

### Создать страницу из Markdown

`POST /v1/pages` принимает параметр тела `markdown`.

```bash
curl -s -X POST "https://api.notion.com/v1/pages" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "parent": {"page_id": "xxx"},
    "properties": {"title": [{"text": {"content": "Notes from meeting"}}]},
    "markdown": "# Agenda\n\n- Q3 roadmap\n- Hiring\n\n## Decisions\n- Ship MVP Friday"
  }'
```

### Исправьте страницу с помощью Markdown
```bash
curl -s -X PATCH "https://api.notion.com/v1/pages/{page_id}/markdown" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "## Update\n\nShipped the prototype."}'
```

### Создать страницу в базе данных (типизированные свойства)
```bash
curl -s -X POST "https://api.notion.com/v1/pages" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "parent": {"database_id": "xxx"},
    "properties": {
      "Name": {"title": [{"text": {"content": "New Item"}}]},
      "Status": {"select": {"name": "Todo"}}
    }
  }'
```

### Запрос к базе данных (источнику данных)
```bash
curl -s -X POST "https://api.notion.com/v1/data_sources/{data_source_id}/query" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "filter": {"property": "Status", "select": {"equals": "Active"}},
    "sorts": [{"property": "Date", "direction": "descending"}]
  }'
```

### Создайте базу данных
```bash
curl -s -X POST "https://api.notion.com/v1/data_sources" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "parent": {"page_id": "xxx"},
    "title": [{"text": {"content": "My Database"}}],
    "properties": {
      "Name": {"title": {}},
      "Status": {"select": {"options": [{"name": "Todo"}, {"name": "Done"}]}},
      "Date": {"date": {}}
    }
  }'
```

### Обновить свойства страницы
```bash
curl -s -X PATCH "https://api.notion.com/v1/pages/{page_id}" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"properties": {"Status": {"select": {"name": "Done"}}}}'
```

### Добавляем блоки на страницу
```bash
curl -s -X PATCH "https://api.notion.com/v1/blocks/{page_id}/children" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "children": [
      {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "Hello from Hermes!"}}]}}
    ]
  }'
```

### Загрузка файлов (3-этапный процесс)
```bash
# 1. Create upload
curl -s -X POST "https://api.notion.com/v1/file_uploads" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"filename": "photo.png", "content_type": "image/png"}'

# 2. PUT bytes to the upload_url returned above
curl -s -X PUT "{upload_url}" --data-binary @photo.png

# 3. Reference {file_upload_id} in a page/block payload
```

## Типы свойств

Общие форматы свойств для элементов базы данных:

- **Название:** `{"title": [{"text": {"content": "..."}}]}`
- **Форматированный текст:** `{"rich_text": [{"text": {"content": "..."}}]}`
- **Выбрать:** `{"select": {"name": "Option"}}`
- **Множественный выбор:** `{"multi_select": [{"name": "A"}, {"name": "B"}]}`
- **Дата:** `{"date": {"start": "2026-01-15", "end": "2026-01-16"}}`
- **Флажок:** `{"checkbox": true}`
- **Номер:** `{"number": 42}`
- **URL:** `{"url": "https://..."}`
- **Электронная почта:** `{"email": "user@example.com"}`
- **Связь:** `{"relation": [{"id": "page_id"}]}`

## Версия API 2025-09-03 — Базы данных и источники данных

– **Базы данных стали источниками данных.** Используйте конечные точки `/data_sources/` для запросов и извлечения данных.
- **Два идентификатора на базу данных:** `database_id` и `data_source_id`.
  - `database_id` при создании страниц: `parent: {"database_id": "..."}`
  - `data_source_id` при запросе: `POST /v1/data_sources/{id}/query`
- Поиск возвращает базы данных как `"object": "data_source"` с полем `data_source_id`.

## Notion Workers (продвинутый уровень, требуется `ntn`)

Workers — это хосты Notion для программ TypeScript. Один работник может предоставить любую комбинацию:
- **Синхронизация** — извлечение данных из внешних API в базу данных Notion по расписанию (по умолчанию 30 минут).
- **Инструменты** — отображаются как вызываемые инструменты внутри пользовательских агентов Notion.
- **Вебхуки** — получают HTTP-события от внешних сервисов (GitHub, Stripe и т. д.) и действуют в Notion.

**План/ворота платформы:**
- CLI работает на всех планах. **Для развертывания Workers требуется Business или Enterprise.**
- `ntn` доступен только для macOS/Linux по состоянию на май 2026 г. Пользователям Windows необходим WSL2 или нужно дождаться встроенной поддержки.
- Бесплатно до 11 августа 2026 г.; после этого рассчитывается по кредитам Notion.

### Минимальный рабочий

```bash
ntn workers new my-worker      # scaffold
cd my-worker
# Edit src/index.ts
ntn workers deploy --name my-worker
```

`src/index.ts`:
```typescript
import { Worker } from "@notionhq/workers";

const worker = new Worker();
export default worker;

worker.tool("greet", {
  title: "Greet a User",
  description: "Returns a friendly greeting",
  inputSchema: { type: "object", properties: { name: { type: "string" } }, required: ["name"] },
  execute: async ({ name }) => `Hello, ${name}!`,
});
```

### Возможность вебхука

```typescript
worker.webhook("onGithubPush", {
  title: "GitHub Push Handler",
  execute: async (events, { notion }) => {
    for (const event of events) {
      // event.body, event.rawBody (for signature verification), event.headers
      console.log("got delivery", event.deliveryId);
    }
  },
});
```

После развертывания: `ntn workers webhooks list` показывает URL-адрес, созданный Notion. Считайте этот URL-адрес секретом — любой, у кого он есть, может отправлять события POST, если вы не добавите проверку подписи.

### Команды жизненного цикла работника

```bash
ntn workers deploy
ntn workers list
ntn workers exec <capability-key> -d '{"name": "world"}'
ntn workers sync trigger <key>            # run a sync now
ntn workers sync pause <key>
ntn workers env set GITHUB_WEBHOOK_SECRET=...
ntn workers runs list                     # recent invocations
ntn workers runs logs <run-id>
ntn workers webhooks list
```

Когда вас попросят создать Worker, создайте каркас с помощью `ntn workers new`, напишите код в `src/index.ts`, установите все секреты с помощью `ntn workers env set` и разверните. Документация Notion по адресу https://developers.notion.com/workers охватывает всю поверхность API.

## Markdown со вкусом Notion (используется конечными точками `/markdown`)

Стандартный CommonMark плюс XML-подобные теги для блоков, специфичных для Notion. Используйте **табуляции** для отступов.

**Блокировки за пределами CommonMark:**
```
<callout icon="🎯" color="blue_bg">
	Ship the MVP by **Friday**.
</callout>

<details color="gray">
<summary>Toggle title</summary>
	Children indented one tab
</details>

<columns>
	<column>Left side</column>
	<column>Right side</column>
</columns>

<table_of_contents color="gray"/>
```

**Встроенное:**
- Упоминания: `<mention-user url="..."/>`, `<mention-page url="...">Title</mention-page>`, `<mention-date start="2026-05-15"/>`.
- Подчеркивание: `<span underline="true">text</span>`
– Цвет: `<span color="blue">text</span>` или `{color="blue"}` уровня блока в первой строке.
– Математика: встроенная `$x^2$`, блок `$$ ... $$`.
- Цитаты: `[^https://example.com]`

**Цвета**: `gray brown orange yellow green blue purple pink red`, а также `*_bg` вариантов фона.

Заголовки 5/6 сворачиваются до H4. Несколько строк `>` отображаются как отдельные блоки кавычек — используйте `<br>` внутри одного `>` для многострочных кавычек.

## Выбор правильного пути

| Задача | Mac/Линукс | Окна |
|---|---|---|
| Чтение/запись страниц, поиск, запросы к базам данных | `ntn api ...` | локон |
| Прочтите страницу, чтобы агент подвел итоги | `ntn api v1/pages/{id}/markdown` | конечная точка завитка `/markdown` |
| Загрузить файл | `ntn files create < file` | 3-этапный HTTP-поток |
| Разовое исследование API | `ntn api ...` | локон |
| Создайте инструмент синхронизации/вебхука/агента, размещенный на Notion | `ntn workers ...` | WSL2 + `ntn workers ...` |

## Примечания

— Идентификаторы страниц/базы данных — это UUID (с дефисами или без них — оба принимаются).
- Ограничение скорости: в среднем ~3 запроса в секунду. CLI не обходит это.
- API не может устанавливать фильтры **представления** базы данных — это только пользовательский интерфейс.
– Используйте `"is_inline": true` при создании источников данных для их внедрения на страницу.
- Всегда передавайте `-s` в команду Curl, чтобы подавить индикаторы выполнения (вывод средства очистки).
– Передавайте JSON через `jq` при чтении: `... | jq '.results[0].properties'`.
- Notion также теперь поставляется с сервером MCP (`Notion MCP`, на ~91 % эффективнее использование токенов при операциях с БД, чем в предыдущей версии) — подключите его через поддержку MCP Hermes, если вы хотите осуществлять потоковый доступ к Notion изнутри сеанса, но указанных выше путей достаточно для большинства одноразовых задач.