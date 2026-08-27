---
title: Google Workspace — Gmail, Календарь, Диск, Документы, Таблицы через GWS CLI
  или Python
sidebar_label: Google Workspace
description: Gmail, Календарь, Диск, Документы, Таблицы через GWS CLI или Python
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Рабочая область Google

Gmail, Календарь, Диск, Документы, Таблицы через интерфейс командной строки gws или Python.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/productivity/google-workspace` |
| Версия | `1.2.0` |
| Автор | Ноус Исследования |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Google`, `Gmail`, `Calendar`, `Drive`, `Sheets`, `Docs`, `Contacts`, `Email`, `OAuth` |
| Сопутствующие навыки | [`himalaya`](/docs/user-guide/skills/bundled/email/email-himalaya) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Рабочая область Google

Gmail, Календарь, Диск, Контакты, Таблицы и Документы — через OAuth под управлением Hermes и тонкую оболочку CLI. Если `gws` установлен, навык использует его в качестве серверной части выполнения для более широкого охвата Google Workspace; в противном случае он возвращается к встроенной реализации клиента Python.

## Ссылки

- `references/gmail-search-syntax.md` — операторы поиска Gmail (is:unread, from:, newer_than: и т. д.)
- `references/daily-brief.md` — ежедневная/утренняя краткая процедура: расписание + конфликты + подготовка к встрече + срочная почта из Gmail и Календаря. Загрузите его, когда пользователь запрашивает утреннюю сводку, подготовку к встрече или «что у меня в календаре и какое электронное письмо требует внимания».

## Скрипты

- `scripts/setup.py` — настройка OAuth2 (запустите один раз для авторизации)
- `scripts/google_api.py` — оболочка совместимости CLI. Он предпочитает `gws` для операций, когда он доступен, сохраняя при этом существующий контракт вывода JSON Hermes.

## Первоначальная настройка

Настройка полностью неинтерактивна — вы выполняете ее шаг за шагом, чтобы она работала.
в CLI, Telegram, Discord или на любой платформе.

Сначала определите сокращение:

```bash
GSETUP="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/setup.py"
```

### Шаг 0. Проверьте, настроено ли оно уже

```bash
$GSETUP --check
```

Если он печатает `AUTHENTICATED`, перейдите к разделу «Использование» — настройка уже завершена.

### Шаг 1. Сортировка — спросите пользователя, что ему нужно.

Прежде чем приступить к настройке OAuth, задайте пользователю ДВА вопроса:

**Вопрос 1: «Какие сервисы Google вам нужны? Просто электронная почта или еще
Календарь/Диск/Таблицы/Документы?"**

- **Только электронная почта** → Им вообще не нужен этот навык. Используйте навык `himalaya`
  вместо этого он работает с паролем приложения Gmail (Настройки → Безопасность → Приложение).
  Пароли) и занимает 2 минуты на настройку. Никакой проект Google Cloud не требуется.
  Загрузите навык «Гималаи» и следуйте инструкциям по его настройке.

- **Электронная почта + Календарь** → Продолжайте использовать этот навык, но используйте
  `--services email,calendar` во время аутентификации, поэтому на экране согласия запрашивается только
  области, которые им действительно нужны.

- **Только Календарь/Диск/Таблицы/Документы** → Продолжайте осваивать этот навык и используйте
  более узкий `--services`, установленный как `calendar,drive,sheets,docs`.

- **Полный доступ к рабочему пространству** → Продолжайте использовать этот навык и используйте значение по умолчанию.
  `all` сервисный набор.

**Вопрос 2: «Использует ли ваша учетная запись Google Дополнительную защиту (аппаратную
ключи безопасности, необходимые для входа)? Если вы не уверены, возможно, нет.
— это то, на что вы бы явно подписались».**

- **Нет/Не уверен** → Обычная настройка. Продолжить ниже.
- **Да** → Администратор рабочей области должен добавить идентификатор клиента OAuth в адрес организации.
  Список разрешенных приложений перед шагом 4 будет работать. Сообщите им об этом заранее.

### Шаг 2. Создайте учетные данные OAuth (однократно, ~5 минут).

Сообщите пользователю:

> Вам нужен клиент Google Cloud OAuth. Это одноразовая установка:
>
> 1. Создайте или выберите проект:
> https://console.cloud.google.com/projectselector2/home/dashboard
> 2. Включите необходимые API из библиотеки API:
> https://console.cloud.google.com/apis/library
> Включить: API Gmail, API Календаря Google, API Google Диска,
> API Google Таблиц, API Документов Google, API людей
> 3. Создайте клиент OAuth здесь:
> https://console.cloud.google.com/apis/credentials
> Учетные данные → Создать учетные данные → Идентификатор клиента OAuth 2.0
> 4. Тип приложения: «Настольное приложение» → Создать.
> 5. Если приложение все еще находится на стадии тестирования, добавьте учетную запись Google пользователя в качестве тестового пользователя здесь:
> https://console.cloud.google.com/auth/audience
> Аудитория → Тестовые пользователи → Добавить пользователей
> 6. Загрузите файл JSON и сообщите мне путь к нему.
>
> Важное примечание Hermes CLI: если путь к файлу начинается с `/`, НЕ отправляйте только пустой путь как отдельное сообщение в CLI, поскольку его можно принять за косую черту. Вместо этого отправьте его в предложении, например:
> `The JSON file path is: ~/Downloads/client_secret_....json`

Как только они предоставят путь:

```bash
$GSETUP --client-secret /path/to/client_secret.json
```

Если они вставят необработанные значения идентификатора клиента/секрета клиента вместо пути к файлу,
напишите для них действительный JSON-файл Desktop OAuth и сохраните его где-нибудь.
явный (например, `~/Downloads/hermes-google-client-secret.json`), затем запустите
`--client-secret` против этого файла.

### Шаг 3. Получите URL-адрес авторизации

Используйте набор услуг, выбранный на шаге 1. Примеры:

```bash
$GSETUP --auth-url --services email,calendar --format json
$GSETUP --auth-url --services calendar,drive,sheets,docs --format json
$GSETUP --auth-url --services all --format json
```

Это вернет JSON с полем `auth_url`, а также сохранит точный URL-адрес в
`~/.hermes/google_oauth_last_url.txt`.

Правила агента для этого шага:
– Извлеките поле `auth_url` и отправьте этот точный URL-адрес пользователю в виде одной строки.
– Сообщите пользователю, что браузер, скорее всего, выйдет из строя `http://localhost:1` после одобрения и что это ожидаемо.
– Попросите их скопировать ВЕСЬ перенаправленный URL-адрес из адресной строки браузера.
– Если пользователь получает `Error 403: access_denied`, отправьте его непосредственно на `https://console.cloud.google.com/auth/audience`, чтобы добавить себя в качестве тестового пользователя.

### Шаг 4: Обменяйте код

Пользователь вставит обратно URL-адрес типа `http://localhost:1/?code=4/0A...&scope=...`.
или просто строка кода. Либо работает. Шаг `--auth-url` сохраняет временный
ожидающий сеанс OAuth локально, чтобы `--auth-code` мог завершить обмен PKCE
позже, даже в безголовых системах:

```bash
$GSETUP --auth-code "THE_URL_OR_CODE_THE_USER_PASTED" --format json
```

Если `--auth-code` не удается выполнить, поскольку срок действия кода истек, он уже использовался или получен из
старая вкладка браузера, теперь она возвращает новый `fresh_auth_url`. В этом случае
немедленно отправьте новый URL-адрес пользователю и попросите его повторить попытку с новейшим
только перенаправление браузера.

### Шаг 5. Проверьте

```bash
$GSETUP --check
```

Должно быть напечатано `AUTHENTICATED`. Настройка завершена — с этого момента токен обновляется автоматически.

### Примечания

– Токен хранится по адресу `~/.hermes/google_token.json` и автоматически обновляется.
– Состояние/верификатор ожидающего сеанса OAuth временно сохраняются по адресу `~/.hermes/google_oauth_pending.json` до завершения обмена.
- Если установлен `gws`, `google_api.py` указывает на тот же файл учетных данных `~/.hermes/google_token.json`. Пользователям не нужно запускать отдельный поток `gws auth login`.
- Для отзыва: `$GSETUP --revoke`

## Использование

Все команды проходят через скрипт API. Установите `GAPI` в качестве сокращения:

```bash
GAPI="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/google_api.py"
```

### Gmail

```bash
# Search (returns JSON array with id, from, subject, date, snippet)
$GAPI gmail search "is:unread" --max 10
$GAPI gmail search "from:boss@company.com newer_than:1d"
$GAPI gmail search "has:attachment filename:pdf newer_than:7d"

# Read full message (returns JSON with body text)
$GAPI gmail get MESSAGE_ID

# Send
$GAPI gmail send --to user@example.com --subject "Hello" --body "Message text"
$GAPI gmail send --to user@example.com --subject "Report" --body "<h1>Q4</h1><p>Details...</p>" --html
$GAPI gmail send --to user@example.com --subject "Hello" --from '"Research Agent" <user@example.com>' --body "Message text"

# Reply (automatically threads and sets In-Reply-To)
$GAPI gmail reply MESSAGE_ID --body "Thanks, that works for me."
$GAPI gmail reply MESSAGE_ID --from '"Support Bot" <user@example.com>' --body "Thanks"

# Labels
$GAPI gmail labels
$GAPI gmail modify MESSAGE_ID --add-labels LABEL_ID
$GAPI gmail modify MESSAGE_ID --remove-labels UNREAD
```

### Календарь

```bash
# List events (defaults to next 7 days)
$GAPI calendar list
$GAPI calendar list --start 2026-03-01T00:00:00Z --end 2026-03-07T23:59:59Z

# Create event (ISO 8601 with timezone required)
$GAPI calendar create --summary "Team Standup" --start 2026-03-01T10:00:00-06:00 --end 2026-03-01T10:30:00-06:00
$GAPI calendar create --summary "Lunch" --start 2026-03-01T12:00:00Z --end 2026-03-01T13:00:00Z --location "Cafe"
$GAPI calendar create --summary "Review" --start 2026-03-01T14:00:00Z --end 2026-03-01T15:00:00Z --attendees "alice@co.com,bob@co.com"

# Delete event
$GAPI calendar delete EVENT_ID
```

### Диск

```bash
# Search existing files
$GAPI drive search "quarterly report" --max 10
$GAPI drive search "mimeType='application/pdf'" --raw-query --max 5

# Get metadata for a single file
$GAPI drive get FILE_ID

# Upload a local file (auto-detects MIME type)
$GAPI drive upload /path/to/report.pdf
$GAPI drive upload /path/to/image.png --name "Logo.png" --parent FOLDER_ID

# Download (binary files download as-is; Google-native files export to a
# sensible default — Docs→pdf, Sheets→csv, Slides→pdf, Drawings→png)
$GAPI drive download FILE_ID
$GAPI drive download DOC_ID --output ~/doc.pdf
$GAPI drive download DOC_ID --export-mime text/plain --output ~/doc.txt

# Create a folder
$GAPI drive create-folder "Reports"
$GAPI drive create-folder "Q4" --parent FOLDER_ID

# Share
$GAPI drive share FILE_ID --email alice@example.com --role reader
$GAPI drive share FILE_ID --email alice@example.com --role writer --notify
$GAPI drive share FILE_ID --type anyone --role reader        # anyone with link
$GAPI drive share FILE_ID --type domain --domain example.com --role reader

# Delete — defaults to trash (reversible). Use --permanent to skip the trash.
$GAPI drive delete FILE_ID
$GAPI drive delete FILE_ID --permanent
```

### Контакты

```bash
$GAPI contacts list --max 20
```

### Листы

```bash
# Create a new spreadsheet
$GAPI sheets create --title "Q4 Budget"
$GAPI sheets create --title "Inventory" --sheet-name "Stock"

# Read
$GAPI sheets get SHEET_ID "Sheet1!A1:D10"

# Write
$GAPI sheets update SHEET_ID "Sheet1!A1:B2" --values '[["Name","Score"],["Alice","95"]]'

# Append rows
$GAPI sheets append SHEET_ID "Sheet1!A:C" --values '[["new","row","data"]]'
```

### Документы

```bash
# Read
$GAPI docs get DOC_ID

# Create a new Doc (optionally seeded with body text)
$GAPI docs create --title "Meeting Notes"
$GAPI docs create --title "Draft" --body "First paragraph..."

# Append text to the end of an existing Doc
$GAPI docs append DOC_ID --text "Additional content to append"
```

## Формат вывода

Все команды возвращают JSON. Разберите с помощью `jq` или прочитайте напрямую. Ключевые поля:

– **Поиск в Gmail**: `[{id, threadId, from, to, subject, date, snippet, labels}]`
- **Получение Gmail**: `{id, threadId, from, to, subject, date, labels, body}`
– **Отправка/ответ Gmail**: `{status: "sent", id, threadId}`
- **Список календаря**: `[{id, summary, start, end, location, description, htmlLink}]`
- **Создание календаря**: `{status: "created", id, summary, htmlLink}`
- **Поиск на диске**: `[{id, name, mimeType, modifiedTime, webViewLink}]`
- **Получить**: `{id, name, mimeType, modifiedTime, size, webViewLink, parents, owners}`
- **Загрузка на диск**: `{status: "uploaded", id, name, mimeType, webViewLink}`
- **Загрузка с диска**: `{status: "downloaded", id, name, path, mimeType}`
- **Папка создания диска**: `{status: "created", id, name, webViewLink}`
- **Общий доступ к диску**: `{status: "shared", permissionId, fileId, role, type}`
- **Удаление диска**: `{status: "trashed" | "deleted", fileId, permanent}`
- **Список контактов**: `[{name, emails: [...], phones: [...]}]`
- **Листы получают**: `[[cell, cell, ...], ...]`
- **Создание листов**: `{status: "created", spreadsheetId, title, spreadsheetUrl}`
- **Создание документов**: `{status: "created", documentId, title, url}`
- **Документация добавлена**: `{status: "appended", documentId, inserted_at, characters}`

## Правила

1. **Никогда не отправляйте электронную почту, не создавайте и не удаляйте события календаря, не удаляйте файлы на Диске, не делитесь файлами и не изменяйте документы и таблицы без предварительного подтверждения у пользователя.** Покажите, что будет сделано (получатели, идентификаторы файлов, контент, роль общего доступа) и запросите одобрение. Для `drive delete` отдайте предпочтение корзине по умолчанию (обратимой), а не `--permanent`.
2. **Проверьте авторизацию перед первым использованием** — запустите `setup.py --check`. Если это не помогло, проведите пользователя через настройку.
3. **Используйте справочник по синтаксису поиска Gmail** для сложных запросов — загрузите его с помощью `skill_view("google-workspace", file_path="references/gmail-search-syntax.md")`.
4. **Календарное время должно включать часовой пояс** — всегда используйте ISO 8601 со смещением (например, `2026-03-01T10:00:00-06:00`) или UTC (`Z`).
5. **Соблюдайте ограничения по скорости** — избегайте быстрых последовательных вызовов API. Пакетное чтение, когда это возможно.

## Устранение неполадок

| Проблема | Исправить |
|---------|-----|
| `NOT_AUTHENTICATED` | Запустите шаги установки 2–5 выше |
| `REFRESH_FAILED` | Токен отозван или срок его действия истек. Повторите шаги 3–5 |
| `HttpError 403: Insufficient Permission` | Отсутствует область API — `$GSETUP --revoke`, затем повторите шаги 3–5 |
| `AUTHENTICATED (partial)` или «Области действия токена отсутствуют» | Новые возможности записи (запись/удаление на Диске, создание/редактирование документов) требуют повторной авторизации. `$GSETUP --revoke`, затем повторите шаги 3–5, чтобы предоставить обновленные области. |
| `HttpError 403: Access Not Configured` | API не включен — пользователю необходимо включить его в Google Cloud Console |
| `ModuleNotFoundError` | Запустите `$GSETUP --install-deps` |
| Дополнительная защита блокирует авторизацию | Администратор рабочей области должен добавить идентификатор клиента OAuth в белый список |

## Отмена доступа

```bash
$GSETUP --revoke
```