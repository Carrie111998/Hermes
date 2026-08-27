---
sidebar_position: 11
sidebar_label: GitHub PR Reviews via Webhook
title: Автоматизированные PR-комментарии GitHub с помощью веб-хуков
description: Подключите Hermes к GitHub, чтобы он автоматически получал PR-дифференциалы,
  просматривал изменения кода и публиковал комментарии — инициируемые веб-перехватчиками,
  без ручного запроса.
---

# Автоматические PR-комментарии GitHub с помощью веб-хуков

В этом руководстве рассказывается, как подключить агент Hermes к GitHub, чтобы он автоматически получал разницу запроса на включение, анализировал изменения кода и публиковал комментарий, инициируемый событием веб-перехватчика, без ручного запроса.

Когда PR открывается или обновляется, GitHub отправляет POST веб-перехватчика на ваш экземпляр Hermes. Hermes запускает агент с приглашением, которое инструктирует его получить разницу через интерфейс командной строки `gh`, и ответ отправляется обратно в поток PR.

:::tip Хотите более простую настройку без общедоступной конечной точки?
Если у вас нет общедоступного URL-адреса или вы просто хотите быстро приступить к работе, ознакомьтесь с [Создание агента PR-ревью GitHub](./github-pr-review-agent.md) — использует задания cron для опроса PR по расписанию, работает за NAT и брандмауэрами.
:::

:::info Справочная документация
Полный справочник по платформе веб-перехватчиков (все параметры конфигурации, типы доставки, динамические подписки, модель безопасности) см. в разделе [Веб-перехватчики](/user-guide/messaging/webhooks).
:::

:::предупреждение: риск быстрого внедрения
Полезные данные вебхука содержат данные, контролируемые злоумышленниками: PR-заголовки, сообщения о фиксации и описания могут содержать вредоносные инструкции. Когда конечная точка вашего веб-перехватчика доступна в Интернете, запустите шлюз в изолированной среде (Docker, серверная часть SSH). См. раздел [раздел безопасности](#security-notes) ниже.
:::

---

## Предварительные условия

- Агент Hermes установлен и работает (`hermes gateway`)
- [`gh` CLI](https://cli.github.com/) установлен и прошел проверку подлинности на узле шлюза (`gh auth login`).
- Общедоступный URL-адрес вашего экземпляра Hermes (см. [Локальное тестирование с помощью ngrok](#local-testing-with-ngrok), если выполняется локально)
- Доступ администратора к репозиторию GitHub (требуется для управления веб-перехватчиками).

---

## Шаг 1. Включите платформу веб-перехватчиков

Добавьте в свой `~/.hermes/config.yaml` следующее:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644          # default; change if another service occupies this port
      rate_limit: 30      # max requests per minute per route (not a global cap)

      routes:
        github-pr-review:
          secret: "your-webhook-secret-here"   # must match the GitHub webhook secret exactly
          events:
            - pull_request

          # The agent is instructed to fetch the actual diff before reviewing.
          # {number} and {repository.full_name} are resolved from the GitHub payload.
          prompt: |
            A pull request event was received (action: {action}).

            PR #{number}: {pull_request.title}
            Author: {pull_request.user.login}
            Branch: {pull_request.head.ref} → {pull_request.base.ref}
            Description: {pull_request.body}
            URL: {pull_request.html_url}

            If the action is "closed" or "labeled", stop here and do not post a comment.

            Otherwise:
            1. Run: gh pr diff {number} --repo {repository.full_name}
            2. Review the code changes for correctness, security issues, and clarity.
            3. Write a concise, actionable review comment and post it.

          deliver: github_comment
          deliver_extra:
            repo: "{repository.full_name}"
            pr_number: "{number}"
```

**Ключевые поля:**

| Поле | Описание |
|---|---|
| `secret` (уровень маршрута) | Секрет HMAC для этого маршрута. Возвращается к глобальному значению `extra.secret`, если оно опущено. |
| `events` | Список значений заголовка `X-GitHub-Event`, которые можно принять. Пустой список = принять все. |
| `prompt` | Шаблон; `{field}` и `{nested.field}` разрешаются из полезных данных GitHub. |
| `deliver` | `github_comment` публикует сообщения через `gh pr comment`. `log` просто записывает в журнал шлюза. |
| `deliver_extra.repo` | Решает, например, `org/repo` из полезной нагрузки. |
| `deliver_extra.pr_number` | Преобразуется в номер PR из полезной нагрузки. |

:::note Полезная нагрузка не содержит кода
Полезная нагрузка веб-перехватчика GitHub включает метаданные PR (заголовок, описание, названия ветвей, URL-адреса), но **не различия**. Приведенная выше подсказка предписывает агенту запустить `gh pr diff` для получения фактических изменений. Набор инструментов `hermes-webhook` по умолчанию намеренно ограничен (веб-поиск/извлечение, видение, уточнение — **нет терминала**), поскольку полезные данные веб-перехватчика могут переносить ненадежный контент. Чтобы этот маршрут выполнял `gh`, добавьте разрешение набора инструментов для каждого маршрута: `toolsets: ["terminal", "web"]` в конфигурации маршрута — см. [Наборы инструментов для каждого маршрута](/docs/user-guide/messaging/webhooks#per-route-toolsets).
:::

---

## Шаг 2 — Запустите шлюз

```bash
hermes gateway
```

Вы должны увидеть:

```
[webhook] Listening on 0.0.0.0:8644 — routes: github-pr-review
```

Убедитесь, что он работает:

```bash
curl http://localhost:8644/health
# {"status": "ok", "platform": "webhook"}
```

---

## Шаг 3 — Зарегистрируйте вебхук на GitHub

1. Перейдите в свой репозиторий → **Настройки** → **Вебхуки** → **Добавить вебхук**.
2. Заполните:
   - **URL-адрес полезной нагрузки:** `https://your-public-url.example.com/webhooks/github-pr-review`
   – **Тип контента:** `application/json`
   - **Секрет:** то же значение, которое вы установили для `secret` в конфигурации маршрута.
   - **Какие события?** → Выберите отдельные события → установите флажок **Запросы на включение**
3. Нажмите **Добавить вебхук**.

GitHub немедленно отправит событие `ping` для подтверждения соединения. Он безопасно игнорируется — `ping` нет в вашем списке `events` — и возвращает `{"status": "ignored", "event": "ping"}`. Он регистрируется только на уровне DEBUG, поэтому не отображается в консоли на уровне журнала по умолчанию.

---

## Шаг 4 — Откройте тестовый PR

Создайте ветку, внесите изменения и откройте PR. В течение 30–90 секунд (в зависимости от размера и модели PR) Hermes должен опубликовать комментарий к обзору.

Чтобы следить за прогрессом агента в режиме реального времени:

```bash
tail -f "${HERMES_HOME:-$HOME/.hermes}/logs/gateway.log"
```

---

## Локальное тестирование с помощью ngrok

Если Hermes работает на вашем ноутбуке, используйте [ngrok](https://ngrok.com/), чтобы открыть его:

```bash
ngrok http 8644
```

Скопируйте URL-адрес `https://...ngrok-free.app` и используйте его в качестве URL-адреса полезной нагрузки GitHub. На бесплатном уровне ngrok URL-адрес меняется каждый раз при перезапуске ngrok — обновляйте веб-хук GitHub при каждом сеансе. Платные аккаунты ngrok получают статический домен.

Вы можете протестировать статический маршрут напрямую с помощью `curl` — не требуется учетная запись GitHub или настоящий пиар.

:::tip Используйте `deliver: log` при локальном тестировании
Измените `deliver: github_comment` на `deliver: log` в вашей конфигурации во время тестирования. В противном случае агент попытается опубликовать комментарий к поддельному репозиторию `org/repo#99` в тестовых полезных данных, но это не удастся. Вернитесь к `deliver: github_comment`, как только вы будете удовлетворены быстрым выводом.
:::

```bash
SECRET="your-webhook-secret-here"
BODY='{"action":"opened","number":99,"pull_request":{"title":"Test PR","body":"Adds a feature.","user":{"login":"testuser"},"head":{"ref":"feat/x"},"base":{"ref":"main"},"html_url":"https://github.com/org/repo/pull/99"},"repository":{"full_name":"org/repo"}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print "sha256="$2}')

curl -s -X POST http://localhost:8644/webhooks/github-pr-review \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"
# Expected: {"status":"accepted","route":"github-pr-review","event":"pull_request","delivery_id":"..."}
```

Затем посмотрите, как работает агент:
```bash
tail -f "${HERMES_HOME:-$HOME/.hermes}/logs/gateway.log"
```

:::примечание
`hermes webhook test <name>` работает только для **динамических подписок**, созданных с помощью `hermes webhook subscribe`. Он не читает маршруты от `config.yaml`.
:::

---

## Фильтрация по конкретным действиям

GitHub отправляет события `pull_request` для многих действий: `opened`, `synchronize`, `reopened`, `closed`, `labeled` и т. д. Список `events` фильтруется по значению заголовка `X-GitHub-Event`, а `filters` на уровне маршрута может сужаться по полям полезной нагрузки, например `action`.

Подсказка на шаге 1 уже обрабатывает эту проблему, предписывая агенту досрочно остановиться для событий `closed` и `labeled`.

:::warning Агент все еще работает и потребляет токены
Инструкция «остановиться здесь» препятствует значимому просмотру, но агент по-прежнему работает до завершения для каждого события `pull_request` независимо от действия. Предпочитать фильтрацию до пробуждения агента:

```yaml
filters:
  - field: "action"
    in: ["opened", "synchronize", "reopened"]
```

Для репозиториев большого объема вы все равно можете фильтровать исходящие данные с помощью рабочего процесса GitHub Actions, который условно вызывает URL-адрес вашего веб-перехватчика.
:::

> Не существует синтаксиса Jinja2 или условного шаблона. Поддерживаются только `{field}` и `{nested.field}`. Все остальное передается агенту дословно.

---

## Использование навыков для последовательного стиля обзора

Загрузите [навык Hermes](/user-guide/features/skills), чтобы дать агенту возможность последовательного обзора. Добавьте `skills` к своему маршруту внутри `platforms.webhook.extra.routes` в `config.yaml`:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      routes:
        github-pr-review:
          secret: "your-webhook-secret-here"
          events: [pull_request]
          prompt: |
            A pull request event was received (action: {action}).
            PR #{number}: {pull_request.title} by {pull_request.user.login}
            URL: {pull_request.html_url}

            If the action is "closed" or "labeled", stop here and do not post a comment.

            Otherwise:
            1. Run: gh pr diff {number} --repo {repository.full_name}
            2. Review the diff using your review guidelines.
            3. Write a concise, actionable review comment and post it.
          skills:
            - review
          deliver: github_comment
          deliver_extra:
            repo: "{repository.full_name}"
            pr_number: "{number}"
```

> **Примечание.** Загружается только первый найденный навык в списке. Гермес не объединяет несколько навыков — последующие записи игнорируются.

---

## Вместо этого отправка ответов в Slack или Discord

Замените поля `deliver` и `deliver_extra` внутри вашего маршрута целевой платформой:

```yaml
# Inside platforms.webhook.extra.routes.<route-name>:

# Slack
deliver: slack
deliver_extra:
  chat_id: "C0123456789"   # Slack channel ID (omit to use the configured home channel)

# Discord
deliver: discord
deliver_extra:
  chat_id: "987654321012345678"  # Discord channel ID (omit to use home channel)
```

Целевая платформа также должна быть включена и подключена к шлюзу. Если `chat_id` опущен, ответ отправляется на настроенный домашний канал этой платформы.

Допустимые значения `deliver`: `log` · `github_comment` · `telegram` · `discord` · `slack` · `signal` · `sms`

---

## Поддержка GitLab

Этот же адаптер работает с GitLab. GitLab использует `X-Gitlab-Token` для аутентификации (соответствие простой строки, а не HMAC) — Hermes обрабатывает и то, и другое автоматически.

Для фильтрации событий GitLab устанавливает для `X-GitLab-Event` такие значения, как `Merge Request Hook`, `Push Hook`, `Pipeline Hook`. Используйте точное значение заголовка в `events`:

```yaml
events:
  - Merge Request Hook
```

Поля полезной нагрузки GitLab отличаются от полей GitHub — например. `{object_attributes.title}` для названия MR и `{object_attributes.iid}` для номера MR. Самый простой способ узнать полную структуру полезной нагрузки — это нажать кнопку **Тест** в GitLab в настройках веб-перехватчика в сочетании с журналом **Последние поставки**. Альтернативно, опустите `prompt` в конфигурации маршрута — тогда Hermes передаст полную полезную нагрузку в формате JSON непосредственно агенту, а ответ агента (видимый в журнале шлюза с `deliver: log`) будет описывать его структуру.

---

## Примечания по безопасности

- **Никогда не используйте `INSECURE_NO_AUTH`** в производстве — он полностью отключает проверку подписи. Это только для местного развития.
- **Периодически меняйте секрет веб-перехватчика** и обновляйте его как на GitHub (настройки веб-перехватчика), так и на вашем `config.yaml`.
- **Ограничение скорости** по умолчанию составляет 30 запросов/мин на маршрут (настраивается через `extra.rate_limit`). При его превышении возвращается `429`.
- **Дубликаты поставок** (повторные попытки веб-перехватчика) дедуплицируются с помощью 1-часового идемпотентного кеша. Ключ кэша — `X-GitHub-Delivery`, если он присутствует, затем `X-Request-ID`, а затем миллисекундная метка времени. Если ни один из заголовков идентификатора доставки не задан, повторные попытки **не** дедуплицируются.
- **Быстрое внедрение:** PR-заголовки, описания и сообщения о фиксации контролируются злоумышленниками. Злонамеренные пиарщики могут попытаться манипулировать действиями агента. Запускайте шлюз в изолированной среде (Docker, виртуальная машина) при наличии доступа к общедоступному Интернету.

---

## Устранение неполадок

| Симптом | Проверить |
|---|---|
| `401 Invalid signature` | Секрет в config.yaml не соответствует секрету веб-перехватчика GitHub |
| `404 Unknown route` | Имя маршрута в URL-адресе не соответствует ключу в `routes:` |
| `429 Rate limit exceeded` | Превышено 30 запросов в минуту на каждый маршрут — обычно при повторной доставке тестовых событий из пользовательского интерфейса GitHub; подождите или поднимите `extra.rate_limit` |
| Комментариев не оставлено | `gh` не установлен, не находится в PATH или не прошел проверку подлинности (`gh auth login`) |
| Агент запускается, но комментариев нет | Проверьте журнал шлюза — если выходные данные агента были пустыми или просто «ПРОПУСТИТЬ», попытка доставки все равно будет |
| Порт уже используется | Измените `extra.port` в config.yaml |
| Агент работает, но просматривает только описание PR | Приглашение не включает инструкцию `gh pr diff` — разница отсутствует в полезных данных веб-перехватчика |
| Не могу увидеть событие ping | Игнорируемые события возвращают `{"status":"ignored","event":"ping"}` только на уровне журнала DEBUG — проверьте журнал доставки GitHub (репозиторий → Настройки → Веб-перехватчики → ваш веб-перехватчик → Недавние поставки) |

**Вкладка «Последние поставки» GitHub** (репозиторий → Настройки → Веб-перехватчики → ваш веб-перехватчик) показывает точные заголовки запроса, полезные данные, статус HTTP и текст ответа для каждой доставки. Это самый быстрый способ диагностировать сбои, не затрагивая журналы сервера.

---

## Полная ссылка на конфигурацию

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644               # listen port (default: 8644)
      secret: ""               # optional global fallback secret
      rate_limit: 30           # requests per minute per route
      max_body_bytes: 1048576  # payload size limit in bytes (default: 1 MB)

      routes:
        <route-name>:
          secret: "required-per-route"
          events: []            # [] = accept all; otherwise list X-GitHub-Event values
          prompt: ""            # {field} / {nested.field} resolved from payload
          skills: []            # first matching skill is loaded (only one)
          deliver: "log"        # log | github_comment | telegram | discord | slack | signal | sms
          deliver_extra: {}     # repo + pr_number for github_comment; chat_id for others
```

---

## Что дальше?

- **[PR-обзоры на основе Cron](./github-pr-review-agent.md)** — опрос PR по расписанию, публичная конечная точка не требуется.
- **[Справочник по веб-перехватчикам](/user-guide/messaging/webhooks)** — полный справочник по конфигурации платформы веб-перехватчиков.
- **[Создать плагин](/developer-guide/plugins)** — упаковать логику проверки в общий плагин.
- **[Профили](/user-guide/profiles)** — запустить специальный профиль рецензента с собственной памятью и настройками.