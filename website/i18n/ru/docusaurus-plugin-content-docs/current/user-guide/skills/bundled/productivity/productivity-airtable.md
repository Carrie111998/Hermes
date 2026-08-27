---
title: Airtable — Airtable REST API via curl
sidebar_label: Airtable
description: REST API Airtable через Curl
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Воздушный стол

REST API Airtable через Curl. Записывает CRUD, фильтрует, обновляет.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/productivity/airtable` |
| Версия | `1.1.0` |
| Автор | сообщество |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Airtable`, `Productivity`, `Database`, `API` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Airtable — базы, таблицы и записи

Работайте с REST API Airtable напрямую через `curl` с помощью инструмента `terminal`. Никакого сервера MCP, никакого потока OAuth, никакого Python SDK — только `curl` и личный токен доступа.

## Предварительные условия

1. Создайте **Токен личного доступа (PAT)** на странице https://airtable.com/create/tokens (токены начинаются с `pat...`).
2. Предоставьте следующие области (минимум):
   - `data.records:read` — прочитать строки
   - `data.records:write` — создать/обновить/удалить строки
   - `schema.bases:read` — список баз и таблиц
3. **Важно!** в том же пользовательском интерфейсе токена добавьте каждую базу, к которой вы хотите получить доступ, в список **Доступ** токена. Область действия PAT определена для каждой базы: действительный токен на неправильной базе возвращает `403`.
4. Сохраните токен в `${HERMES_HOME:-~/.hermes}/.env` (или через `hermes setup`):
   ```
   AIRTABLE_API_KEY=pat_your_token_here
   ```

> Примечание. Устаревшие ключи API `key...` устарели в феврале 2024 года. Сейчас работают только токены PAT и OAuth.

## Основы API

- **Конечная точка:** `https://api.airtable.com/v0`
- **Заголовок аутентификации:** `Authorization: Bearer $AIRTABLE_API_KEY`
– **Все запросы** используют JSON (`Content-Type: application/json` для любого тела POST/PATCH/PUT).
- **Идентификаторы объектов:** базы `app...`, таблицы `tbl...`, записи `rec...`, поля `fld...`. Идентификаторы никогда не меняются; имена могут. Предпочитайте идентификаторы в автоматизации.
- **Ограничение скорости:** 5 запросов/сек/база. `429` → отступить. Всплеск на одной базе будет ограничен.

Базовый узор завитка:
```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?maxRecords=5" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

`-s` подавляет индикатор выполнения Curl — оставляйте его установленным для каждого вызова, чтобы выходные данные инструмента оставались чистыми для Hermes. Передавайте через `python3 -m json.tool` (всегда присутствует) или `jq` (если установлен) для читаемого JSON.

## Типы полей (формы тела запроса)

| Тип поля | Написать форму |
|---|---|
| Однострочный текст | `"Name": "hello"` |
| Длинный текст | `"Notes": "multi\nline"` |
| Номер | `"Score": 42` |
| Флажок | `"Done": true` |
| Одиночный выбор | `"Status": "Todo"` (имя должно уже существовать, кроме `typecast: true`) |
| Множественный выбор | `"Tags": ["urgent", "bug"]` |
| Дата | `"Due": "2026-04-01"` |
| Дата и время (UTC) | `"At": "2026-04-01T14:30:00.000Z"` |
| URL-адрес / электронная почта / телефон | `"Link": "https://…"` |
| Вложение | `"Files": [{"url": "https://…"}]` (загрузка Airtable + повторный хостинг) |
| Связанная запись | `"Owner": ["recXXXXXXXXXXXXXX"]` (массив идентификаторов записей) |
| Пользователь | `"AssignedTo": {"id": "usrXXXXXXXXXXXXXX"}` |

Передайте `"typecast": true` на верхнем уровне тела создания/обновления, чтобы позволить Airtable автоматически приводить значения (например, создать новую опцию выбора на лету, преобразовать `"42"` → `42`).

## Общие запросы

### Список баз, которые может видеть токен
```bash
curl -s "https://api.airtable.com/v0/meta/bases" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

### Список таблиц + схема для базы
```bash
curl -s "https://api.airtable.com/v0/meta/bases/$BASE_ID/tables" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```
Используйте это ПЕРЕД изменением — подтверждает точные имена и идентификаторы полей, отображает `options.choices` для выбранных полей и показывает имена основных полей.

### Список записей (первые 10)
```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?maxRecords=10" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

### Получить одну запись
```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE/$RECORD_ID" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

### Фильтровать записи (filterByFormula)
Формулы Airtable должны быть закодированы в URL-адресе. Позвольте Python stdlib сделать это — никогда не кодируйте вручную:
```bash
FORMULA="{Status}='Todo'"
ENC=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$FORMULA")
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?filterByFormula=$ENC&maxRecords=20" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

Полезные шаблоны формул:
- Точное совпадение: `{Email}='user@example.com'`
- Содержит: `FIND('bug', LOWER({Title}))`
- Несколько условий: `AND({Status}='Todo', {Priority}='High')`
- Или: `OR({Owner}='alice', {Owner}='bob')`
- Не пусто: `NOT({Assignee}='')`
- Сравнение дат: `IS_AFTER({Due}, TODAY())`

### Сортировка + выбор определенных полей
```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?sort%5B0%5D%5Bfield%5D=Priority&sort%5B0%5D%5Bdirection%5D=asc&fields%5B%5D=Name&fields%5B%5D=Status" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```
Квадратные скобки в параметрах запроса ДОЛЖНЫ быть закодированы в URL-адресе (`%5B` / `%5D`).

### Используйте именованное представление
```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?view=Grid%20view&maxRecords=50" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```
Представления применяют сохраненный фильтр + сортировку на стороне сервера.

## Распространенные мутации

### Создать запись
```bash
curl -s -X POST "https://api.airtable.com/v0/$BASE_ID/$TABLE" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"fields":{"Name":"New task","Status":"Todo","Priority":"High"}}' | python3 -m json.tool
```

### Создание до 10 записей за один вызов
```bash
curl -s -X POST "https://api.airtable.com/v0/$BASE_ID/$TABLE" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "typecast": true,
    "records": [
      {"fields": {"Name": "Task A", "Status": "Todo"}},
      {"fields": {"Name": "Task B", "Status": "In progress"}}
    ]
  }' | python3 -m json.tool
```
Конечные точки пакетной обработки ограничены **10 записями на запрос**. Для более крупных вставок запускайте циклы по 10 операций с коротким интервалом ожидания, чтобы обеспечить скорость 5 запросов/сек/база.

### Обновить запись (PATCH — объединяет, сохраняет поля неизмененными)
```bash
curl -s -X PATCH "https://api.airtable.com/v0/$BASE_ID/$TABLE/$RECORD_ID" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"fields":{"Status":"Done"}}' | python3 -m json.tool
```

### Обновление с помощью поля слияния (идентификатор не требуется)
```bash
curl -s -X PATCH "https://api.airtable.com/v0/$BASE_ID/$TABLE" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "performUpsert": {"fieldsToMergeOn": ["Email"]},
    "records": [
      {"fields": {"Email": "user@example.com", "Status": "Active"}}
    ]
  }' | python3 -m json.tool
```
`performUpsert` создает записи, значения полей слияния которых являются новыми, исправляет записи, значения полей слияния которых уже существуют. Отлично подходит для идемпотентной синхронизации.

### Удалить запись
```bash
curl -s -X DELETE "https://api.airtable.com/v0/$BASE_ID/$TABLE/$RECORD_ID" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

### Удаление до 10 записей за один вызов
```bash
curl -s -X DELETE "https://api.airtable.com/v0/$BASE_ID/$TABLE?records%5B%5D=rec1&records%5B%5D=rec2" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

## Пагинация

Конечные точки списка возвращают не более **100 записей на страницу**. Если ответ включает `"offset": "..."`, передайте его обратно при следующем вызове. Цикл до тех пор, пока поле не исчезнет:

```bash
OFFSET=""
while :; do
  URL="https://api.airtable.com/v0/$BASE_ID/$TABLE?pageSize=100"
  [ -n "$OFFSET" ] && URL="$URL&offset=$OFFSET"
  RESP=$(curl -s "$URL" -H "Authorization: Bearer $AIRTABLE_API_KEY")
  echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(r["id"], r["fields"].get("Name","")) for r in d["records"]]'
  OFFSET=$(echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("offset",""))')
  [ -z "$OFFSET" ] && break
done
```

## Типичный рабочий процесс Hermes

1. **Подтвердить авторизацию** `curl -s -o /dev/null -w "%{http_code}\n" https://api.airtable.com/v0/meta/bases -H "Authorization: Bearer $AIRTABLE_API_KEY"` — ожидать `200`.
2. **Найдите базу.** Перечислите базы (шаг выше) ИЛИ запросите у пользователя идентификатор `app...` напрямую, если в токене отсутствует `schema.bases:read`.
3. **Проверьте схему.** `GET /v0/meta/bases/$BASE_ID/tables` — кэшируйте точные имена полей и имя основного поля локально в сеансе, прежде чем что-либо изменять.
4. **Прочтите, прежде чем писать.** Для «обновления X, где Y» сначала `filterByFormula` для разрешения идентификатора `rec...`, затем `PATCH /v0/$BASE_ID/$TABLE/$RECORD_ID`. Никогда не угадывайте идентификаторы записей.
5. **Пакетная запись.** Объедините связанные создания в одну POST из 10 записей, чтобы не выйти за рамки бюджета 5 запросов в секунду.
6. **Деструктивные операции.** Удаление невозможно отменить через API. Если пользователь говорит «удалить все X», повторите счетчик фильтра + записи и подтвердите перед запуском.

## Подводные камни

- **`filterByFormula` ДОЛЖЕН иметь URL-кодировку.** Имена полей с пробелами или не в формате ASCII также нуждаются в кодировке (`{My Field}` → `%7BMy%20Field%7D`). Используйте Python stdlib (шаблон выше) — никогда не переключайтесь вручную.
- **Пустые поля не включаются в ответы.** Отсутствие ключа `"Assignee"` не означает, что поля не существует — это означает, что значение этой записи пусто. Прежде чем прийти к выводу, что поле отсутствует, проверьте схему (шаг 3).
- **PATCH vs PUT.** `PATCH` объединяет предоставленные поля в запись. `PUT` полностью заменяет запись и очищает все поля, которые вы не включили. По умолчанию `PATCH`.
- **Должны существовать параметры с одним выбором.** Написание `"Status": "Shipping"`, когда `Shipping` отсутствует в списке параметров поля, приводит к ошибкам с `INVALID_MULTIPLE_CHOICE_OPTIONS`, если только вы не передадите `"typecast": true` (который автоматически создает параметр).
- **Область действия токена для каждой базы.** `403` на одной базе, в то время как другая работает, означает, что список доступа токена не включает эту базу — это не проблема области действия или аутентификации. Отправьте пользователя на https://airtable.com/create/tokens, чтобы предоставить его.
- **Ограничения скорости указаны для каждой базы, а не для токена.** 5 запросов в секунду на `baseA` и 5 запросов в секунду на `baseB` — это нормально; 6 запросов в секунду только на `baseA` будут регулироваться. Отслеживайте заголовок `Retry-After` на `429`.

## Важные примечания для Гермеса

- **Всегда используйте инструмент `terminal` с `curl`.** НЕ используйте `web_extract` (он не может отправлять заголовки аутентификации) или `browser_navigate` (требуется аутентификация пользовательского интерфейса и он работает медленно).
- **`AIRTABLE_API_KEY` автоматически переходит из `${HERMES_HOME:-~/.hermes}/.env` в подпроцесс** при загрузке этого навыка — нет необходимости повторно экспортировать его перед каждым вызовом `curl`.
- **Осторожно используйте фигурные скобки в формулах.** В теле документа `{Status}` является буквальным. В аргументе оболочки `{Status}` безопасен вне контекста раскрытия скобок `{...}`, но передает динамические строки через `python3 urllib.parse.quote` перед объединением в URL-адрес.
- **Красочная печать с помощью `python3 -m json.tool`** (всегда присутствует), а не `jq` (необязательно). Используйте `jq` только тогда, когда вам нужна фильтрация/проецирование.
- **Разбивка на страницы осуществляется постранично, а не глобально.** Ограничение в 100 записей в Airtable — это жесткий предел; нет возможности его сбить. Цикл с `offset` до тех пор, пока поле не исчезнет.
- **Прочитайте массив `errors`** для ответов, отличных от 2xx. Airtable возвращает структурированные коды ошибок, такие как `AUTHENTICATION_REQUIRED`, `INVALID_PERMISSIONS`, `MODEL_ID_NOT_FOUND`, `INVALID_MULTIPLE_CHOICE_OPTIONS`, которые точно подскажут вам, что не так.