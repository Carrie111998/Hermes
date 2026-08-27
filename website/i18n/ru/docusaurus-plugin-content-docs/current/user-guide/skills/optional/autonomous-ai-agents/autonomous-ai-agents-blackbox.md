---
title: Blackbox — делегируйте задачи кодирования многомодельному интерфейсу командной
  строки Blackbox AI.
sidebar_label: Blackbox
description: Делегируйте задачи кодирования многомодельному интерфейсу командной строки
  Blackbox AI.
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Черный ящик

Делегируйте задачи кодирования многомодельному интерфейсу командной строки Blackbox AI.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/autonomous-ai-agents/blackbox` |
| Путь | `optional-skills/autonomous-ai-agents/blackbox` |
| Версия | `1.0.1` |
| Автор | Агент Гермеса (Nous Research) |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Coding-Agent`, `Blackbox`, `Multi-Agent`, `Judge`, `Multi-Model` |
| Сопутствующие навыки | [`claude-code`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code), [`codex`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-codex), [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Черный ящик CLI

Делегируйте задачи по кодированию [Blackbox AI](https://www.blackbox.ai/) через терминал Hermes. Blackbox — это многомодельный агент CLI для кодирования, который распределяет задачи по нескольким LLM (Claude, Codex, Gemini, Blackbox Pro) и использует оценку для выбора лучшей реализации.

CLI (npm `@blackbox_ai/blackbox-cli`, двоичный `blackbox`) — это агент кодирования TypeScript (развитый от Gemini CLI) и поддерживает интерактивные сеансы, неинтерактивные одноразовые сеансы, контрольные точки, MCP и переключение модели машинного зрения.

## Предварительные условия

- Установлен Node.js 20+.
- Установлен интерфейс командной строки Blackbox: `npm install -g @blackbox_ai/blackbox-cli` (двоичный файл: `blackbox`)
- Ключ API из [app.blackbox.ai/dashboard](https://app.blackbox.ai/dashboard)
- Настроено: запустите `blackbox configure` и введите свой ключ API.
- Используйте `pty=true` в терминальных вызовах — Blackbox CLI — это интерактивное терминальное приложение.

## Одноразовые задачи

```
terminal(command="blackbox --prompt 'Add JWT authentication with refresh tokens to the Express API'", workdir="/path/to/project", pty=true)
```

Для быстрой скретч-работы:
```
terminal(command="cd $(mktemp -d) && git init && blackbox --prompt 'Build a REST API for todos with SQLite'", pty=true)
```

## Фоновый режим (длинные задачи)

Для задач, которые занимают несколько минут, используйте фоновый режим, чтобы вы могли отслеживать ход выполнения:

```
# Start in background with PTY
terminal(command="blackbox --prompt 'Refactor the auth module to use OAuth 2.0'", workdir="~/project", background=true, pty=true)
# Returns session_id

# Monitor progress
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Send input if Blackbox asks a question
process(action="submit", session_id="<id>", data="yes")

# Kill if needed
process(action="kill", session_id="<id>")
```

## Контрольные точки и резюме

Blackbox CLI имеет встроенную поддержку контрольных точек для приостановки и возобновления задач:

```
# After a task completes, Blackbox shows a checkpoint tag
# Resume with a follow-up task:
terminal(command="blackbox --resume-checkpoint 'task-abc123-2026-03-06' --prompt 'Now add rate limiting to the endpoints'", workdir="~/project", pty=true)
```

## Команды сеанса

Во время интерактивного сеанса используйте следующие команды:

| Команда | Эффект |
|---------|--------|
| `/compress` | Сократите историю разговоров, чтобы сохранить токены |
| `/clear` | Очистить историю и начать все сначала |
| `/stats` | Просмотр текущего использования токенов |
| `Ctrl+C` | Отменить текущую операцию |

## PR-обзоры

Клонируйте во временный каталог, чтобы избежать изменения рабочего дерева:

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && gh pr checkout 42 && blackbox --prompt 'Review this PR against main. Check for bugs, security issues, and code quality.'", pty=true)
```

## Параллельная работа

Создайте несколько экземпляров Blackbox для независимых задач:

```
terminal(command="blackbox --prompt 'Fix the login bug'", workdir="/tmp/issue-1", background=true, pty=true)
terminal(command="blackbox --prompt 'Add unit tests for auth'", workdir="/tmp/issue-2", background=true, pty=true)

# Monitor all
process(action="list")
```

## Многомодельный режим

Уникальная функция Blackbox — выполнение одной и той же задачи на нескольких моделях и оценка результатов. Настройте, какие модели использовать, с помощью `blackbox configure` — выберите нескольких поставщиков, чтобы включить рабочий процесс председателя/судьи, в котором CLI оценивает результаты различных моделей и выбирает лучшую.

## Ключевые флаги

| Флаг | Эффект |
|------|--------|
| `--prompt "task"` (`-p`) | Неинтерактивное однократное исполнение |
| `--resume-checkpoint "tag"` | Возобновить работу с сохраненной контрольной точки |
| `--yolo` (`-y`) | Автоматическое одобрение всех действий и переключений моделей |
| `--vlm-switch-mode <mode>` | Обработка изображений: `once`, `session` или `persist` |
| `-c, --checkpointing` | Включить контрольную точку редактирования файлов |
| `blackbox configure` | Изменение настроек, провайдеров, моделей |
| `blackbox update` | Обновите CLI до последней версии |
| `blackbox mcp` | Управление серверами MCP |
| `blackbox extensions` | Управление расширениями CLI |
| `blackbox voice <action>` / `blackbox shortcut` | Настройка голосового ввода / ярлык `b` |

## Поддержка зрения

Blackbox автоматически обнаруживает изображения на входе и может переключиться на мультимодальный анализ. Режимы ВЛМ:
- `"once"` — Переключение модели только для текущего запроса.
- `"session"` — Переключиться на весь сеанс
- `"persist"` — Остаться на текущей модели (без переключателя)

## Лимиты токенов

Управляйте использованием токена через `.blackboxcli/settings.json`:
```json
{
  "sessionTokenLimit": 32000
}
```

## Правила

1. **Всегда используйте `pty=true`** — Blackbox CLI — это интерактивное терминальное приложение, которое зависает без PTY.
2. **Используйте `workdir`** — держите агента в правильном каталоге.
3. **Фон для длительных задач** — используйте `background=true` и отслеживайте с помощью инструмента `process`.
4. **Не вмешивайтесь** — отслеживайте с помощью `poll`/`log`, не закрывайте сеансы, потому что они медленные.
5. **Отчет о результатах** — после завершения проверьте, что изменилось, и подведите итоги для пользователя.
6. **Кредиты стоят денег** — Blackbox использует систему, основанную на кредитах; многомодельный режим потребляет кредиты быстрее
7. **Проверьте предварительные требования** — убедитесь, что `blackbox` CLI установлен, прежде чем пытаться делегировать.