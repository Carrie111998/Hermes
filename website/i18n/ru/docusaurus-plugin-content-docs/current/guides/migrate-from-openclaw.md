---
sidebar_position: 10
title: Миграция с OpenClaw
description: Полное руководство по переносу настроек OpenClaw / Clawdbot в агент Hermes
  — что будет перенесено, как настроить карты и что после этого проверить.
---

# Миграция с OpenClaw

`hermes claw migrate` импортирует вашу установку OpenClaw (или устаревшую версию Clawdbot/Moldbot) в Hermes. В этом руководстве подробно описано, что именно будет перенесено, сопоставления ключей конфигурации и что нужно проверить после миграции.

:::примечание
Вместо этого вы используете **Claude Code** или **OpenAI Codex CLI**? Используйте [`hermes import-agent`](../user-guide/import-from-other-agents.md).
:::

:::совет
Если в вашей настройке OpenClaw используется несколько провайдеров, `hermes setup --portal` сворачивает ее до одного OAuth — более 300 моделей плюс Tool Gateway за один вход. См. [Nous Portal](/integrations/nous-portal).
:::

## Быстрый старт

```bash
# Preview then migrate (always shows a preview first, then asks to confirm)
hermes claw migrate

# Preview only, no changes
hermes claw migrate --dry-run

# Full migration including API keys, skip confirmation
hermes claw migrate --preset full --migrate-secrets --yes
```

При миграции всегда отображается полный предварительный просмотр того, что будет импортировано, прежде чем вносить какие-либо изменения. Просмотрите список, затем подтвердите, чтобы продолжить.

По умолчанию читает из `~/.openclaw/`. Устаревшие каталоги `~/.clawdbot/` или `~/.moltbot/` обнаруживаются автоматически. То же самое для устаревших имен файлов конфигурации (`clawdbot.json`, `moltbot.json`).

## Опции

| Вариант | Описание |
|--------|-------------|
| `--dry-run` | Только предварительный просмотр — остановитесь после показа того, что будет перенесено. |
| `--preset <name>` | `full` (все совместимые настройки) или `user-data` (исключая конфигурацию инфраструктуры). Ни одна из предустановок не импортирует секреты по умолчанию — передайте `--migrate-secrets` явно. |
| `--overwrite` | Перезаписать существующие файлы Hermes в случае конфликтов (по умолчанию: отказаться от применения, если в плане есть конфликты). |
| `--migrate-secrets` | Включите ключи API. Требуется даже под `--preset full` — нет предустановленного импорта секретов в автоматическом режиме. |
| `--no-backup` | Пропустите zip-снимок `~/.hermes/` перед переносом (по умолчанию перед применением записывается один архив с точкой восстановления в `~/.hermes/backups/pre-migration-*.zip`; возможность восстановления с помощью `hermes import`). |
| `--source <path>` | Пользовательский каталог OpenClaw. |
| `--workspace-target <path>` | Где разместить `AGENTS.md`. |
| `--skill-conflict <mode>` | `skip` (по умолчанию), `overwrite` или `rename`. |
| `--yes` | Пропустите запрос подтверждения после предварительного просмотра. |

## Что переносится

### Персона, память и инструкции

| Что | исходный код OpenClaw | Гермес пункт назначения | Заметки |
|------|----------------|-------------------|-------|
| Персона | `workspace/SOUL.md` | `~/.hermes/SOUL.md` | Прямая копия |
| Инструкции по рабочему пространству | `workspace/AGENTS.md` | `AGENTS.md` в `--workspace-target` | Требуется флаг `--workspace-target` |
| Долговременная память | `workspace/MEMORY.md` | `~/.hermes/memories/MEMORY.md` | Разбирается на записи, объединяется с существующими и дедуплицируется. Использует разделитель `§`. |
| Профиль пользователя | `workspace/USER.md` | `~/.hermes/memories/USER.md` | Та же логика слияния записей, что и в памяти. |
| Ежедневные файлы памяти | `workspace/memory/*.md` | `~/.hermes/memories/MEMORY.md` | Все ежедневные файлы объединены в основную память. |

Файлы рабочей области также проверяются по адресам `workspace.default/` и `workspace-main/` как резервные пути (в последних версиях OpenClaw переименовал `workspace/` в `workspace-main/` и использует `workspace-{agentId}` для многоагентных настроек).

### Навыки (4 источника)

| Источник | Местоположение OpenClaw | Гермес пункт назначения |
|--------|------------------|-------------------|
| Навыки рабочего пространства | `workspace/skills/` | `~/.hermes/skills/openclaw-imports/` |
| Управляемые/совместные навыки | `~/.openclaw/skills/` | `~/.hermes/skills/openclaw-imports/` |
| Персональный кросс-проект | `~/.agents/skills/` | `~/.hermes/skills/openclaw-imports/` |
| Общий доступ на уровне проекта | `workspace/.agents/skills/` | `~/.hermes/skills/openclaw-imports/` |

Конфликты навыков обрабатываются `--skill-conflict`: `skip` оставляет существующий навык Hermes, `overwrite` заменяет его, `rename` создает копию `-imported`.

### Конфигурация модели и поставщика

| Что | Путь конфигурации OpenClaw | Гермес пункт назначения | Заметки |
|------|---------------------|-------------------|-------|
| Модель по умолчанию | `agents.defaults.model` | `config.yaml` → `model` | Может быть строкой или объектом `{primary, fallbacks}` |
| Пользовательские поставщики | `models.providers.*` | `config.yaml` → `custom_providers` (автоматический переход на канонический `providers:` при следующем переносе конфигурации `hermes update`) | Карты `baseUrl`, `apiType`/`api` — обрабатывают как короткие («openai», «anthropic»), так и записанные через дефис («openai-completions», «anthropic-messages», «google-generative-ai») значения |
| Ключи API поставщика | `models.providers.*.apiKey` | `~/.hermes/.env` | Требуется `--migrate-secrets`. См. [Разрешение ключа API](#api-key-solve) ниже. |

### Поведение агента

| Что | Путь конфигурации OpenClaw | Путь конфигурации Гермеса | Картографирование |
|------|---------------------|-------------------|---------|
| Макс превращается | `agents.defaults.timeoutSeconds` | `agent.max_turns` | `timeoutSeconds / 10`, максимум 200 |
| Подробный режим | `agents.defaults.verboseDefault` | `agent.verbose` | «выкл.» / «вкл.» / «полный» |
| Рассуждающее усилие | `agents.defaults.thinkingDefault` | `agent.reasoning_effort` | «всегда»/«высокий»/«xhigh» → «высокий», «авто»/«средний»/«адаптивный» → «средний», «выкл.»/«низкий»/«нет»/«минимальный» → «низкий» |
| Сжатие | `agents.defaults.compaction.mode` | `compression.enabled` | «выкл.» → ложь, все остальное → правда |
| Модель сжатия | `agents.defaults.compaction.model` | `compression.summary_model` | Прямое копирование строки |
| Человеческая задержка | `agents.defaults.humanDelay.mode` | `human_delay.mode` | «натуральный» / «индивидуальный» / «выключенный» |
| Время задержки человека | `agents.defaults.humanDelay.minMs` / `.maxMs` | `human_delay.min_ms` / `.max_ms` | Прямая копия |
| Часовой пояс | `agents.defaults.userTimezone` | `timezone` | Прямое копирование строки |
| Тайм-аут исполнительного директора | `tools.exec.timeoutSec` | `terminal.timeout` | Прямое копирование (поле `timeoutSec`, а не `timeout`) |
| Песочница Docker | `agents.defaults.sandbox.backend` | `terminal.backend` | «докер» → «докер» |
| Изображение Docker | `agents.defaults.sandbox.docker.image` | `terminal.docker_image` | Прямая копия |

### Политики сброса сеанса

| Путь конфигурации OpenClaw | Путь конфигурации Гермеса | Заметки |
|-----|-----------------------------------|-------|
| `session.reset.mode` | `session_reset.mode` | «ежедневно», «в режиме ожидания» или и то, и другое |
| `session.reset.atHour` | `session_reset.at_hour` | Час (0–23) для ежедневного сброса |
| `session.reset.idleMinutes` | `session_reset.idle_minutes` | Минуты бездействия |

Примечание. В OpenClaw также имеется `session.resetTriggers` (простой массив строк, например `["daily", "idle"]`). Если структурированный `session.reset` отсутствует, миграция возвращается к выводу из `resetTriggers`.

### MCP-серверы

| Поле OpenClaw | Поле Гермеса | Заметки |
|----------------|-------------|-------|
| `mcp.servers.*.command` | `mcp_servers.*.command` | Студия транспорта |
| `mcp.servers.*.args` | `mcp_servers.*.args` | |
| `mcp.servers.*.env` | `mcp_servers.*.env` | |
| `mcp.servers.*.cwd` | `mcp_servers.*.cwd` | |
| `mcp.servers.*.url` | `mcp_servers.*.url` | Транспорт HTTP/SSE |
| `mcp.servers.*.tools.include` | `mcp_servers.*.tools.include` | Фильтрация инструментов |
| `mcp.servers.*.tools.exclude` | `mcp_servers.*.tools.exclude` | |

### TTS (преобразование текста в речь)

Настройки TTS считываются из **двух** местоположений конфигурации OpenClaw со следующим приоритетом:

1. `messages.tts.providers.{provider}.*` (каноническое местоположение)
2. Верхний уровень `talk.providers.{provider}.*` (резервный вариант)
3. Устаревшие плоские клавиши `messages.tts.{provider}.*` (самый старый формат)

| Что | Гермес пункт назначения |
|------|-------------------|
| Имя провайдера | `config.yaml` → `tts.provider` |
| Голосовой идентификатор ElevenLabs | `config.yaml` → `tts.elevenlabs.voice_id` |
| Идентификатор модели ElevenLabs | `config.yaml` → `tts.elevenlabs.model_id` |
| Модель OpenAI | `config.yaml` → `tts.openai.model` |
| Голос OpenAI | `config.yaml` → `tts.openai.voice` |
| Голосовой TTS Edge | `config.yaml` → `tts.edge.voice` (OpenClaw переименовал «edge» в «microsoft» — оба распознаются) |
| ТТС активы | `~/.hermes/tts/` (копия файла) |

### Платформы обмена сообщениями

| Платформа | Путь конфигурации OpenClaw | Гермес `.env` переменная | Заметки |
|----------|---------------------|----------------------|-------|
| Телеграмма | `channels.telegram.botToken` или `.accounts.default.botToken` | `TELEGRAM_BOT_TOKEN` | Токен может быть строкой или [SecretRef](#secretref-handling). Поддерживаются как плоские макеты, так и макеты счетов. |
| Телеграмма | `credentials/telegram-default-allowFrom.json` | `TELEGRAM_ALLOWED_USERS` | Соединение запятой из массива `allowFrom[]` |
| Раздор | `channels.discord.token` или `.accounts.default.token` | `DISCORD_BOT_TOKEN` | |
| Раздор | `channels.discord.allowFrom` или `.accounts.default.allowFrom` | `DISCORD_ALLOWED_USERS` | |
| слабый | `channels.slack.botToken` или `.accounts.default.botToken` | `SLACK_BOT_TOKEN` | |
| слабый | `channels.slack.appToken` или `.accounts.default.appToken` | `SLACK_APP_TOKEN` | |
| слабый | `channels.slack.allowFrom` или `.accounts.default.allowFrom` | `SLACK_ALLOWED_USERS` | |
| WhatsApp | `channels.whatsapp.allowFrom` или `.accounts.default.allowFrom` | `WHATSAPP_ALLOWED_USERS` | Аутентификация через QR-пару Baileys — после миграции требуется повторное сопряжение |
| Сигнал | `channels.signal.account` или `.accounts.default.account` | `SIGNAL_ACCOUNT` | |
| Сигнал | `channels.signal.httpUrl` или `.accounts.default.httpUrl` | `SIGNAL_HTTP_URL` | |
| Сигнал | `channels.signal.allowFrom` или `.accounts.default.allowFrom` | `SIGNAL_ALLOWED_USERS` | |
| Матрица | `channels.matrix.accessToken` или `.accounts.default.accessToken` | `MATRIX_ACCESS_TOKEN` | Использует `accessToken` (не `botToken`) |
| Самое важное | `channels.mattermost.botToken` или `.accounts.default.botToken` | `MATTERMOST_BOT_TOKEN` | |

### Другая конфигурация

| Что | Путь OpenClaw | Путь Гермеса | Заметки |
|------|-------------|-------------|-------|
| Режим одобрения | `approvals.exec.mode` | `config.yaml` → `approvals.mode` | «авто» → «выкл.», «всегда» → «ручной», «умный» → «умный» |
| Список разрешенных команд | `exec-approvals.json` | `config.yaml` → `command_allowlist` | Шаблоны объединены и дедуплицированы |
| URL-адрес CDP браузера | `browser.cdpUrl` | `config.yaml` → `browser.cdp_url` | |
| Браузер без головы | `browser.headless` | `config.yaml` → `browser.headless` | |
| Храбрый ключ поиска | `tools.web.search.brave.apiKey` | `.env` → `BRAVE_API_KEY` | Требуется `--migrate-secrets` |
| Токен аутентификации шлюза | `gateway.auth.token` | `.env` → `HERMES_GATEWAY_TOKEN` | Требуется `--migrate-secrets` |
| Рабочий каталог | `agents.defaults.workspace` | `config.yaml` → `terminal.cwd` | Устаревшие миграции могут по-прежнему выдавать `MESSAGING_CWD` в качестве резервного варианта совместимости |

### В архиве (нет прямого эквивалента Hermes)

Они сохраняются в `~/.hermes/migration/openclaw/<timestamp>/archive/` для проверки вручную:

| Что | Архивный файл | Как воссоздать в Гермесе |
|------|-------------|--------------------------|
| `IDENTITY.md` | `archive/workspace/IDENTITY.md` | Слияние с `SOUL.md` |
| `TOOLS.md` | `archive/workspace/TOOLS.md` | Hermes имеет встроенные инструкции по инструментам |
| `HEARTBEAT.md` | `archive/workspace/HEARTBEAT.md` | Используйте задания cron для периодических задач |
| `BOOTSTRAP.md` | `archive/workspace/BOOTSTRAP.md` | Используйте контекстные файлы или навыки |
| Вакансии Cron | `archive/cron-config.json` | Воссоздать с помощью `hermes cron create` |
| Плагины | `archive/plugins-config.json` | См. [руководство по плагинам](/user-guide/features/hooks) |
| Хуки/вебхуки | `archive/hooks-config.json` | Используйте `hermes webhook` или перехватчики шлюза |
| Серверная часть памяти | `archive/memory-backend-config.json` | Настроить через `hermes honcho` |
| Реестр навыков | `archive/skills-registry-config.json` | Используйте `hermes skills config` |
| Пользовательский интерфейс/идентичность | `archive/ui-identity-config.json` | Используйте команду `/skin` |
| Ведение журнала | `archive/logging-diagnostics-config.json` | Устанавливается в разделе журналов `config.yaml` |
| Список мультиагентов | `archive/agents-list.json` | Используйте профили Hermes |
| Привязки каналов | `archive/bindings.json` | Ручная настройка для каждой платформы |
| Комплексные каналы | `archive/channels-deep-config.json` | Ручная настройка платформы |

## Разрешение ключа API

Когда `--migrate-secrets` включен, ключи API собираются из **четырех источников** в порядке приоритета:

1. **Значения конфигурации** — `models.providers.*.apiKey` и ключи поставщика TTS в `openclaw.json`.
2. **Файл среды** — `~/.openclaw/.env` (ключи типа `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` и т. д.)
3. **Подобъект Config env** — `openclaw.json` → `"env"` или `"env"."vars"` (некоторые настройки хранят ключи здесь вместо отдельного файла `.env`)
4. **Профили аутентификации** — `~/.openclaw/agents/main/agent/auth-profiles.json` (учетные данные для каждого агента)

Значения конфигурации имеют приоритет. Каждый последующий источник заполняет оставшиеся пробелы.

### Поддерживаемые ключевые цели

`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `ZAI_API_KEY`, `MINIMAX_API_KEY`, `ELEVENLABS_API_KEY`, `TELEGRAM_BOT_TOKEN`, `VOICE_TOOLS_OPENAI_KEY`

Ключи, не входящие в этот список разрешений, никогда не копируются.

## Обработка секретной ссылки

Значения конфигурации OpenClaw для токенов и ключей API могут быть в трех форматах:

```json
// Plain string
"channels": { "telegram": { "botToken": "123456:ABC-DEF..." } }

// Environment template
"channels": { "telegram": { "botToken": "${TELEGRAM_BOT_TOKEN}" } }

// SecretRef object
"channels": { "telegram": { "botToken": { "source": "env", "id": "TELEGRAM_BOT_TOKEN" } } }
```

Миграция разрешает все три формата. Для шаблонов env и объектов SecretRef с `source: "env"` он ищет значение в `~/.openclaw/.env` и подобъекте env `openclaw.json`. Объекты SecretRef с `source: "file"` или `source: "exec"` не могут быть разрешены автоматически — миграция предупреждает об этом, и эти значения необходимо добавить в Hermes вручную через `hermes config set`.

## После миграции

1. **Проверьте отчет о переносе** — он распечатывается по завершении с указанием количества перенесенных, пропущенных и конфликтующих элементов.

2. **Просмотр заархивированных файлов** — все в `~/.hermes/migration/openclaw/<timestamp>/archive/` требует ручной обработки.

3. **Начать новый сеанс** — импортированные навыки и записи памяти вступают в силу в новых сеансах, а не в текущем.

4. **Проверка ключей API** — запустите `hermes status`, чтобы проверить аутентификацию поставщика.

5. **Проверка обмена сообщениями** — если вы перенесли токены платформы, перезапустите шлюз: `systemctl --user restart hermes-gateway`

6. **Проверьте политику сеанса** — запустите `hermes config show` и убедитесь, что значение `session_reset` соответствует вашим ожиданиям.

7. **Повторное сопряжение WhatsApp** — WhatsApp использует сопряжение QR-кодов (Baileys), а не миграцию токенов. Запустите `hermes whatsapp` для сопряжения.

8. **Очистка архива** — убедившись, что все работает, запустите `hermes claw cleanup`, чтобы переименовать оставшиеся каталоги OpenClaw в `.pre-migration/` (предотвращает путаницу состояний).

## Устранение неполадок

### "Каталог OpenClaw не найден"

При миграции проверяется `~/.openclaw/`, затем `~/.clawdbot/`, затем `~/.moltbot/`. Если ваша установка находится в другом месте, используйте `--source /path/to/your/openclaw`.

### "Ключи API поставщика не найдены"

Ключи могут храниться в нескольких местах в зависимости от вашей версии OpenClaw: встроенные в `openclaw.json` в `models.providers.*.apiKey`, в `~/.openclaw/.env`, в подобъекте `openclaw.json` `"env"` или в `agents/main/agent/auth-profiles.json`. Миграция проверяет все четыре. Если ключи используют `source: "file"` или `source: "exec"` SecretRefs, их нельзя разрешить автоматически — добавьте их через `hermes config set`.

### Навыки не появляются после миграции

Импортированные навыки появятся в `~/.hermes/skills/openclaw-imports/`. Начните новый сеанс, чтобы они вступили в силу, или запустите `/skills`, чтобы убедиться, что они загружены.

### Голос TTS не перенесен

OpenClaw хранит настройки TTS в двух местах: `messages.tts.providers.*` и конфигурации верхнего уровня `talk`. Миграция проверяет оба. Если ваш голосовой идентификатор был установлен через пользовательский интерфейс OpenClaw (сохранен по другому пути), вам может потребоваться установить его вручную: `hermes config set tts.elevenlabs.voice_id YOUR_VOICE_ID`.