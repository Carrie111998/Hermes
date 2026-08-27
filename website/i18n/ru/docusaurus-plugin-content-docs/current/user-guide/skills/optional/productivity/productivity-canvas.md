---
title: Canvas — Fetch Canvas LMS courses and assignments via API token
sidebar_label: Canvas
description: Fetch Canvas LMS courses and assignments via API token
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Холст

Получайте курсы и задания Canvas LMS через токен API.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/productivity/canvas` |
| Путь | `optional-skills/productivity/canvas` |
| Версия | `1.0.0` |
| Автор | сообщество |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Canvas`, `LMS`, `Education`, `Courses`, `Assignments` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Canvas LMS — доступ к курсам и заданиям

Доступ только для чтения к Canvas LMS для получения списка курсов и заданий.

## Скрипты

- `scripts/canvas_api.py` — Python CLI для вызовов Canvas API.

## Настройка

1. Войдите в свой экземпляр Canvas в браузере.
2. Перейдите в **Аккаунт → Настройки** (нажмите значок своего профиля, затем Настройки).
3. Прокрутите до пункта **Одобренные интеграции** и нажмите **+ Новый токен доступа**.
4. Назовите токен (например, «Агент Гермеса»), установите необязательный срок действия и нажмите **Создать токен**.
5. Скопируйте токен и добавьте в `${HERMES_HOME:-~/.hermes}/.env`:

```
CANVAS_API_TOKEN=your_token_here
CANVAS_BASE_URL=https://yourschool.instructure.com
```

Базовый URL-адрес — это то, что отображается в вашем браузере при входе в Canvas (без косой черты в конце).

## Использование

```bash
CANVAS="python $HERMES_HOME/skills/productivity/canvas/scripts/canvas_api.py"

# List all active courses
$CANVAS list_courses --enrollment-state active

# List all courses (any state)
$CANVAS list_courses

# List assignments for a specific course
$CANVAS list_assignments 12345

# List assignments ordered by due date
$CANVAS list_assignments 12345 --order-by due_at
```

## Формат вывода

**list_courses** возвращает:
```json
[{"id": 12345, "name": "Intro to CS", "course_code": "CS101", "workflow_state": "available", "start_at": "...", "end_at": "..."}]
```

**list_assignments** возвращает:
```json
[{"id": 67890, "name": "Homework 1", "due_at": "2025-02-15T23:59:00Z", "points_possible": 100, "submission_types": ["online_upload"], "html_url": "...", "description": "...", "course_id": 12345}]
```

Примечание. Описания назначений сокращаются до 500 символов. Поле `html_url` ссылается на полную страницу задания в Canvas.

## Справочник по API (curl)

```bash
# List courses
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_BASE_URL/api/v1/courses?enrollment_state=active&per_page=10"

# List assignments for a course
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_BASE_URL/api/v1/courses/COURSE_ID/assignments?per_page=10&order_by=due_at"
```

Canvas использует заголовки `Link` для нумерации страниц. Скрипт Python автоматически обрабатывает нумерацию страниц.

## Правила

- Этот навык доступен **только для чтения** — он только извлекает данные и никогда не изменяет курсы или задания.
– При первом использовании подтвердите авторизацию, запустив `$CANVAS list_courses`. Если ошибка 401, проведите пользователя через настройку.
- Ограничение скорости Canvas до ~700 запросов в 10 минут; проверьте заголовок `X-Rate-Limit-Remaining`, если достигнуты ограничения

## Устранение неполадок

| Проблема | Исправить |
|---------|-----|
| 401 Несанкционированный | Токен недействителен или срок его действия истек. Создайте его заново в настройках холста |
| 403 Запрещено | У токена нет разрешения на этот курс |
| Пустой список курсов | Попробуйте `--enrollment-state active` или опустите флаг, чтобы увидеть все состояния |
| Неправильное учреждение | Убедитесь, что `CANVAS_BASE_URL` соответствует URL-адресу в вашем браузере |
| Ошибки тайм-аута | Проверьте сетевое подключение к вашему экземпляру Canvas |