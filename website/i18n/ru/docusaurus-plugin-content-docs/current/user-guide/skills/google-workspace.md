---
sidebar_position: 2
sidebar_label: Google Workspace
title: Google Workspace — Gmail, Календарь, Диск, Таблицы и Документы
description: Отправляйте электронную почту, управляйте событиями календаря, осуществляйте
  поиск на Диске, читайте и записывайте Таблицы и получайте доступ к Документам —
  и все это через API Google с проверкой подлинности OAuth2.
---

# Навыки работы с Google Workspace

Интеграция Gmail, Календаря, Диска, Контактов, Таблиц и Документов для Hermes. Использует OAuth2 с автоматическим обновлением токена. Предпочитает [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli), если он доступен для более широкого охвата, в противном случае возвращается к клиентским библиотекам Google Python.

**Путь навыка:** `skills/productivity/google-workspace/`

## Настройка

Настройка полностью осуществляется агентом — попросите Hermes настроить Google Workspace, и он проведет вас через каждый шаг. Поток:

1. **Создайте проект Google Cloud** и включите необходимые API (Gmail, Календарь, Диск, Таблицы, Документы, Люди).
2. **Создайте учетные данные OAuth 2.0** (тип настольного приложения) и загрузите секретный код клиента в формате JSON.
3. **Авторизация** — Hermes генерирует URL-адрес авторизации, вы подтверждаете его в браузере, вставляете обратно URL-адрес перенаправления.
4. **Готово** — с этого момента токен автоматически обновляется.

:::tip Пользователи, использующие только электронную почту
Если вам нужна только электронная почта (без Календаря, Диска и Таблиц), используйте вместо этого навык **himalaya** — он работает с паролем приложения Gmail и занимает 2 минуты. Никакой проект Google Cloud не требуется.
:::

## Gmail

### Поиск

```bash
$GAPI gmail search "is:unread" --max 10
$GAPI gmail search "from:boss@company.com newer_than:1d"
$GAPI gmail search "has:attachment filename:pdf newer_than:7d"
```

Возвращает JSON с `id`, `from`, `subject`, `date`, `snippet` и `labels` для каждого сообщения.

### Чтение

```bash
$GAPI gmail get MESSAGE_ID
```

Возвращает полное тело сообщения в виде текста (предпочитает обычный текст, возвращается к HTML).

### Отправка

```bash
# Basic send
$GAPI gmail send --to user@example.com --subject "Hello" --body "Message text"

# HTML email
$GAPI gmail send --to user@example.com --subject "Report" \
  --body "<h1>Q4 Results</h1><p>Details here</p>" --html

# Custom From header (display name + email)
$GAPI gmail send --to user@example.com --subject "Hello" \
  --from '"Research Agent" <user@example.com>' --body "Message text"

# With CC
$GAPI gmail send --to user@example.com --cc "team@example.com" \
  --subject "Update" --body "FYI"
```

### Пользовательский из заголовка

Флаг `--from` позволяет настроить отображаемое имя отправителя в исходящих электронных письмах. Это полезно, когда несколько агентов используют одну и ту же учетную запись Gmail, но вы хотите, чтобы получатели видели разные имена:

```bash
# Agent 1
$GAPI gmail send --to client@co.com --subject "Research Summary" \
  --from '"Research Agent" <shared@company.com>' --body "..."

# Agent 2  
$GAPI gmail send --to client@co.com --subject "Code Review" \
  --from '"Code Assistant" <shared@company.com>' --body "..."
```

**Как это работает.** Значение `--from` устанавливается в качестве заголовка RFC 5322 `From` в сообщении MIME. Gmail позволяет настроить отображаемое имя на вашем собственном проверенном адресе электронной почты без какой-либо дополнительной настройки. Получатели видят собственное отображаемое имя (например, «Агент по исследованиям»), а адрес электронной почты остается прежним.

**Важно!** Если вы используете *другой адрес электронной почты* в `--from` (не аутентифицированную учетную запись), Gmail требует, чтобы этот адрес был настроен как [Псевдоним «Отправить как]» (https://support.google.com/mail/answer/22370) в настройках Gmail → Учетные записи → Отправлять почту как.

Флаг `--from` работает как на `send`, так и на `reply`:

```bash
$GAPI gmail reply MESSAGE_ID \
  --from '"Support Bot" <shared@company.com>' --body "We're on it"
```

### Отвечаю

```bash
$GAPI gmail reply MESSAGE_ID --body "Thanks, that works for me."
```

Автоматически объединяет ответ (устанавливает заголовки `In-Reply-To` и `References`) и использует идентификатор потока исходного сообщения.

### Ярлыки

```bash
# List all labels
$GAPI gmail labels

# Add/remove labels
$GAPI gmail modify MESSAGE_ID --add-labels LABEL_ID
$GAPI gmail modify MESSAGE_ID --remove-labels UNREAD
```

## Календарь

```bash
# List events (defaults to next 7 days)
$GAPI calendar list
$GAPI calendar list --start 2026-03-01T00:00:00Z --end 2026-03-07T23:59:59Z

# Create event (timezone required)
$GAPI calendar create --summary "Team Standup" \
  --start 2026-03-01T10:00:00-07:00 --end 2026-03-01T10:30:00-07:00

# With location and attendees
$GAPI calendar create --summary "Lunch" \
  --start 2026-03-01T12:00:00Z --end 2026-03-01T13:00:00Z \
  --location "Cafe" --attendees "alice@co.com,bob@co.com"

# Delete event
$GAPI calendar delete EVENT_ID
```

:::предупреждение
Календарное время **должно** включать смещение часового пояса (например, `-07:00`) или использовать время в формате UTC (`Z`). Голые даты и время, такие как `2026-03-01T10:00:00`, неоднозначны и будут рассматриваться как UTC.
:::

## Диск

```bash
$GAPI drive search "quarterly report" --max 10
$GAPI drive search "mimeType='application/pdf'" --raw-query --max 5
```

## Листов

```bash
# Read a range
$GAPI sheets get SHEET_ID "Sheet1!A1:D10"

# Write to a range
$GAPI sheets update SHEET_ID "Sheet1!A1:B2" --values '[["Name","Score"],["Alice","95"]]'

# Append rows
$GAPI sheets append SHEET_ID "Sheet1!A:C" --values '[["new","row","data"]]'
```

## Документы

```bash
$GAPI docs get DOC_ID
```

Возвращает заголовок документа и полнотекстовое содержимое.

## Контакты

```bash
$GAPI contacts list --max 20
```

## Формат вывода

Все команды возвращают JSON. Ключевые поля для каждой услуги:

| Команда | Поля |
|---------|--------|
| `gmail search` | `id`, `threadId`, `from`, `to`, `subject`, `date`, `snippet`, `labels` |
| `gmail get` | `id`, `threadId`, `from`, `to`, `subject`, `date`, `labels`, `body` |
| `gmail send/reply` | `status`, `id`, `threadId` |
| `calendar list` | `id`, `summary`, `start`, `end`, `location`, `description`, `htmlLink` |
| `calendar create` | `status`, `id`, `summary`, `htmlLink` |
| `drive search` | `id`, `name`, `mimeType`, `modifiedTime`, `webViewLink` |
| `contacts list` | `name`, `emails`, `phones` |
| `sheets get` | 2D-массив значений ячеек |

## Поиск неисправностей

| Проблема | Исправить |
|---------|-----|
| `NOT_AUTHENTICATED` | Запустите настройку (попросите Hermes настроить Google Workspace) |
| `REFRESH_FAILED` | Токен отозван — повторите шаги авторизации |
| `HttpError 403: Insufficient Permission` | Отсутствует область действия — отмените и повторно авторизуйте с помощью нужных сервисов |
| `HttpError 403: Access Not Configured` | API не включен в Google Cloud Console |
| `ModuleNotFoundError` | Запустите сценарий установки с помощью `--install-deps` |