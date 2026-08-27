---
sidebar_position: 4
title: 'Учебное пособие: Team Telegram Assistant'
description: Пошаговое руководство по настройке бота Telegram, который вся ваша команда
  может использовать для помощи по коду, исследований, системного администрирования
  и многого другого.
---

# Настройте командного помощника Telegram

В этом руководстве вы узнаете, как настроить бота Telegram на базе агента Hermes, которого могут использовать несколько членов команды. В конце концов, у вашей команды будет общий ИИ-помощник, к которому они смогут обращаться за помощью с кодом, исследованиями, системным администрированием и всем остальным, защищенным авторизацией для каждого пользователя.

## Что мы строим

Telegram-бот, который:

- **Любой авторизованный член команды** может обратиться за помощью в личные сообщения — обзоры кода, исследования, команды оболочки, отладка.
- **Запускается на вашем сервере** с полным доступом к инструментам — терминалу, редактированию файлов, веб-поиску, выполнению кода.
- **Сессии для каждого пользователя** — каждый человек получает свой собственный контекст разговора.
- **Безопасно по умолчанию** — взаимодействовать могут только одобренные пользователи, есть два метода авторизации.
- **Запланированные задачи** – ежедневные отчеты, проверки работоспособности и напоминания, отправляемые в командный канал.

---

## Предварительные условия

Прежде чем начать, убедитесь, что у вас есть:

- **Агент Hermes установлен** на сервере или VPS (не на вашем ноутбуке — бот должен продолжать работать). Следуйте [руководству по установке](/getting-started/installation), если вы еще этого не сделали.
- **Аккаунт Telegram** для себя (владельца бота)
- **Настроенный поставщик LLM** — как минимум ключ API для OpenAI, Anthropic или другого поддерживаемого поставщика в `~/.hermes/.env`.

:::совет
VPS за 5 долларов в месяц вполне достаточно для работы шлюза. Сам Hermes легкий — вызовы LLM API стоят денег, и они происходят удаленно.
:::

---

## Шаг 1: Создайте бота Telegram

Каждый бот Telegram начинается с **@BotFather** — официального бота Telegram для создания ботов.

1. **Откройте Telegram** и найдите `@BotFather` или перейдите по адресу [t.me/BotFather](https://t.me/BotFather).

2. **Отправьте `/newbot`** — BotFather спросит вас о двух вещах:
   – **Отображаемое имя** – то, что видят пользователи (например, `Team Hermes Assistant`).
   - **Имя пользователя** — должно заканчиваться на `bot` (например, `myteam_hermes_bot`).

3. **Скопируйте токен бота** — BotFather отвечает примерно так:
   ```
   Use this token to access the HTTP API:
   7123456789:AAH1bGciOiJSUzI1NiIsInR5cCI6Ikp...
   ```
   Сохраните этот токен — он понадобится вам на следующем шаге.

4. **Задайте описание** (необязательно, но рекомендуется):
   ```
   /setdescription
   ```
   Выберите своего бота и введите что-то вроде:
   ```
   Team AI assistant powered by Hermes Agent. DM me for help with code, research, debugging, and more.
   ```

5. **Задать команды боту** (необязательно — открывает пользователю командное меню):
   ```
   /setcommands
   ```
   Выберите своего бота и вставьте:
   ```
   new - Start a fresh conversation
   model - Show or change the AI model
   status - Show session info
   help - Show available commands
   stop - Stop the current task
   ```

:::предупреждение
Держите свой токен бота в секрете. Любой, у кого есть токен, может управлять ботом. В случае утечки используйте `/revoke` в BotFather для создания нового.
:::

---

## Шаг 2. Настройте шлюз

У вас есть два варианта: интерактивный мастер настройки (рекомендуется) или настройка вручную.

### Вариант A: Интерактивная настройка (рекомендуется)

```bash
hermes gateway setup
```

Это проведет вас через все с помощью клавиш со стрелками. Выберите **Telegram**, вставьте свой токен бота и введите свой идентификатор пользователя при появлении соответствующего запроса.

### Вариант Б: Настройка вручную

Добавьте эти строки в `~/.hermes/.env`:

```bash
# Telegram bot token from BotFather
TELEGRAM_BOT_TOKEN=7123456789:AAH1bGciOiJSUzI1NiIsInR5cCI6Ikp...

# Your Telegram user ID (numeric)
TELEGRAM_ALLOWED_USERS=123456789
```

### Как найти свой идентификатор пользователя

Ваш идентификатор пользователя Telegram представляет собой числовое значение (а не ваше имя пользователя). Чтобы найти его:

1. Сообщение [@userinfobot](https://t.me/userinfobot) в Telegram.
2. Он мгновенно отвечает вашим числовым идентификатором пользователя.
3. Скопируйте этот номер в `TELEGRAM_ALLOWED_USERS`.

:::информация
Идентификаторы пользователей Telegram — это постоянные номера, например `123456789`. Они отличаются от вашего `@username`, который может измениться. Всегда используйте числовой идентификатор для белых списков.
:::

---

## Шаг 3: Запустите шлюз

### Быстрый тест

Сначала запустите шлюз на переднем плане, чтобы убедиться, что все работает:

```bash
hermes gateway
```

Вы должны увидеть вывод вроде:

```
[Gateway] Starting Hermes Gateway...
[Gateway] Telegram adapter connected
[Gateway] Cron scheduler started (tick every 60s)
```

Откройте Telegram, найдите своего бота и отправьте ему сообщение. Если он ответит, вы в деле. Нажмите `Ctrl+C`, чтобы остановить.

### Производство: установка как услуга

Для постоянного развертывания, которое выдерживает перезагрузку:

```bash
hermes gateway install
sudo hermes gateway install --system   # Linux only: boot-time system service
```

При этом создается фоновая служба: служба **systemd** пользовательского уровня в Linux по умолчанию, служба **launchd** в macOS или системная служба Linux во время загрузки, если вы передадите `--system`.

```bash
# Linux — manage the default user service
hermes gateway start
hermes gateway stop
hermes gateway status

# View live logs
journalctl --user -u hermes-gateway -f

# Keep running after SSH logout
sudo loginctl enable-linger $USER

# Linux servers — explicit system-service commands
sudo hermes gateway start --system
sudo hermes gateway status --system
journalctl -u hermes-gateway -f
```

```bash
# macOS — manage the service
hermes gateway start
hermes gateway stop
tail -f ~/.hermes/logs/gateway.log
```

:::подсказка ПУТЬ macOS
Plist launchd фиксирует PATH вашей оболочки во время установки, чтобы подпроцессы шлюза могли находить такие инструменты, как Node.js и ffmpeg. Если вы позже установите новые инструменты, повторно запустите `hermes gateway install`, чтобы обновить список.
:::

### Убедитесь, что он работает

```bash
hermes gateway status
```

Затем отправьте тестовое сообщение своему боту в Telegram. Вы должны получить ответ в течение нескольких секунд.

---

## Шаг 4. Настройте групповой доступ

Теперь давайте предоставим доступ вашим товарищам по команде. Есть два подхода.

### Подход А: статический белый список

Соберите идентификаторы пользователей Telegram каждого члена команды (отправьте им сообщение [@userinfobot](https://t.me/userinfobot)) и добавьте их в виде списка, разделенного запятыми:

```bash
# In ~/.hermes/.env
TELEGRAM_ALLOWED_USERS=123456789,987654321,555555555
```

Перезапустите шлюз после изменений:

```bash
hermes gateway stop && hermes gateway start
```

### Подход Б: Объединение DM (рекомендуется для команд)

Сопряжение DM более гибкое — вам не нужно заранее собирать идентификаторы пользователей. Вот как это работает:

1. **Товарищ по команде отправляет боту личное сообщение** — поскольку его нет в белом списке, бот отвечает одноразовым кодом сопряжения:
   ```
   🔐 Pairing code: XKGH5N7P
   Send this code to the bot owner for approval.
   ```

2. **Командник отправляет вам код** (по любому каналу — Slack, по электронной почте, лично).

3. **Вы подтверждаете это** на сервере:
   ```bash
   hermes pairing approve telegram XKGH5N7P
   ```

4. **Они здесь** — бот сразу начинает отвечать на их сообщения.

**Управление парными пользователями:**

```bash
# See all pending and approved users
hermes pairing list

# Revoke someone's access
hermes pairing revoke telegram 987654321

# Clear expired pending codes
hermes pairing clear-pending
```

:::совет
Сопряжение DM идеально подходит для команд, поскольку вам не нужно перезапускать шлюз при добавлении новых пользователей. Одобрения вступают в силу немедленно.
:::

### Вопросы безопасности

- **Никогда не устанавливайте `GATEWAY_ALLOW_ALL_USERS=true`** для бота с терминальным доступом — любой, кто найдет вашего бота, сможет запускать команды на вашем сервере.
- Срок действия кодов сопряжения истекает через **1 час** и используется криптографическая случайность.
- Ограничение скорости предотвращает атаки методом перебора: 1 запрос на пользователя за 10 минут, максимум 3 ожидающих кода на платформу.
- После 5 неудачных попыток утверждения платформа блокируется на 1 час.
- Все данные сопряжения хранятся с разрешениями `chmod 0600`.

---

## Шаг 5: Настройте бота

### Установите домашний канал

**Домашний канал** — это место, где бот доставляет результаты заданий cron и упреждающие сообщения. Без него запланированным задачам некуда отправлять выходные данные.

**Вариант 1.** Используйте команду `/sethome` в любой группе или чате Telegram, участником которого является бот.

**Вариант 2.** Установите его вручную в `~/.hermes/.env`:

```bash
TELEGRAM_HOME_CHANNEL=-1001234567890
TELEGRAM_HOME_CHANNEL_NAME="Team Updates"
```

Чтобы узнать идентификатор канала, добавьте в группу [@userinfobot](https://t.me/userinfobot) — он сообщит идентификатор чата группы.

### Настройка отображения хода работы инструмента

Контролируйте, насколько подробно бот отображает при использовании инструментов. В `~/.hermes/config.yaml`:

```yaml
display:
  tool_progress: new    # off | new | all | verbose
```

| Режим | Что вы видите |
|------|-------------|
| `off` | Только чистые ответы — никаких действий с инструментом |
| `new` | Краткий статус для каждого нового вызова инструмента (рекомендуется для обмена сообщениями) |
| `all` | Каждый вызов инструмента с подробностями |
| `verbose` | Полный вывод инструмента, включая результаты команд |

Пользователи также могут изменить это значение для каждого сеанса с помощью команды `/verbose` в чате.

### Создайте индивидуальность с SOUL.md

Настройте способ общения бота, отредактировав `~/.hermes/SOUL.md`:

Полное руководство см. в разделе [Использование SOUL.md с Hermes](/guides/use-soul-with-hermes).

```markdown
# Soul
You are a helpful team assistant. Be concise and technical.
Use code blocks for any code. Skip pleasantries — the team
values directness. When debugging, always ask for error logs
before guessing at solutions.
```

### Добавить контекст проекта

Если ваша команда работает над конкретными проектами, создайте контекстные файлы, чтобы бот знал ваш стек:

```markdown
<!-- ~/.hermes/AGENTS.md -->
# Team Context
- We use Python 3.12 with FastAPI and SQLAlchemy
- Frontend is React with TypeScript
- CI/CD runs on GitHub Actions
- Production deploys to AWS ECS
- Always suggest writing tests for new code
```

:::информация
Файлы контекста вводятся в системное приглашение каждого сеанса. Будьте краткими — каждый персонаж учитывается в вашем бюджете токенов.
:::

---

## Шаг 6. Настройте запланированные задачи

При работающем шлюзе вы можете планировать повторяющиеся задачи, которые доставляют результаты в канал вашей команды.

### Ежедневная сводка стендапов

Напишите боту в Telegram:

```
Every weekday at 9am, check the GitHub repository at
github.com/myorg/myproject for:
1. Pull requests opened/merged in the last 24 hours
2. Issues created or closed
3. Any CI/CD failures on the main branch
Format as a brief standup-style summary.
```

Агент автоматически создает задание cron и отправляет результаты в чат, куда вы запросили (или на домашний канал).

### Проверка работоспособности сервера

```
Every 6 hours, check disk usage with 'df -h', memory with 'free -h',
and Docker container status with 'docker ps'. Report anything unusual —
partitions above 80%, containers that have restarted, or high memory usage.
```

### Управление запланированными задачами

```bash
# From the CLI
hermes cron list          # View all scheduled jobs
hermes cron status        # Check if scheduler is running

# From Telegram chat
/cron list                # View jobs
/cron remove <job_id>     # Remove a job
```

:::предупреждение
Запросы заданий Cron запускаются в совершенно новых сеансах, без памяти о предыдущих разговорах. Убедитесь, что каждое приглашение содержит **весь** контекст, необходимый агенту: пути к файлам, URL-адреса, адреса серверов и четкие инструкции.
:::

---

## Советы по производству

### Используйте Docker для безопасности

В общем командном боте используйте Docker в качестве серверной части терминала, чтобы команды агента выполнялись в контейнере, а не на вашем хосте:

```bash
# In ~/.hermes/.env
TERMINAL_ENV=docker
TERMINAL_DOCKER_IMAGE=nikolaik/python-nodejs:python3.11-nodejs20
```

Или в `~/.hermes/config.yaml`:

```yaml
terminal:
  backend: docker
  container_cpu: 1
  container_memory: 5120
  container_persistent: true
```

Таким образом, даже если кто-то попросит бота запустить что-то разрушительное, ваша хост-система будет защищена.

### Мониторинг шлюза

```bash
# Check if the gateway is running
hermes gateway status

# Watch live logs (Linux)
journalctl --user -u hermes-gateway -f

# Watch live logs (macOS)
tail -f ~/.hermes/logs/gateway.log
```

### Держите Гермес в курсе

Из Telegram отправьте боту `/update` — он скачает последнюю версию и перезагрузится. Или с сервера:

```bash
hermes update
hermes gateway stop && hermes gateway start
```

### Расположение журналов

| Что | Расположение |
|------|----------|
| Журналы шлюза | `journalctl --user -u hermes-gateway` (Linux) или `~/.hermes/logs/gateway.log` (macOS) |
| Вывод задания Cron | `~/.hermes/cron/output/{job_id}/{timestamp}.md` |
| Определения должностей Cron | `~/.hermes/cron/jobs.json` |
| Данные сопряжения | `~/.hermes/pairing/` |
| История сеансов | `~/.hermes/sessions/` |

---

## Идем дальше

У вас есть работающий командный помощник в Telegram. Вот несколько следующих шагов:

- **[Руководство по безопасности](/user-guide/security)** — подробное описание авторизации, изоляции контейнера и утверждения команд.
- **[Шлюз обмена сообщениями](/user-guide/messaging)** — полный справочник по архитектуре шлюза, управлению сеансами и командам чата.
- **[Настройка Telegram](/user-guide/messaging/telegram)** — сведения о конкретной платформе, включая голосовые сообщения и TTS.
- **[Запланированные задачи](/user-guide/features/cron)** — расширенное планирование cron с параметрами доставки и выражениями cron.
- **[Файлы контекста](/user-guide/features/context-files)** — AGENTS.md, SOUL.md и .cursorrules для знаний о проекте.
- **[Личность](/user-guide/features/personality)** — встроенные настройки личности и пользовательские определения личности.
- **Добавить больше платформ** — на одном шлюзе можно одновременно запускать [Discord](/user-guide/messaging/discord), [Slack](/user-guide/messaging/slack) и [WhatsApp](/user-guide/messaging/whatsapp)

---

*Вопросы или проблемы? Откройте вопрос на GitHub — вклад приветствуется.*