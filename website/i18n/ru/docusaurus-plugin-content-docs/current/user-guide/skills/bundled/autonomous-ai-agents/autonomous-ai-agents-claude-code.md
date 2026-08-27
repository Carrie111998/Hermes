---
title: Claude Code — делегирование кодирования Claude Code CLI (функции, PR)
sidebar_label: Claude Code
description: Делегирование кодирования в Claude Code CLI (функции, PR)
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

#Клод Код

Делегируйте кодирование Claude Code CLI (функции, PR).

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/autonomous-ai-agents/claude-code` |
| Версия | `2.2.1` |
| Автор | Гермес Агент + Текниум |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Coding-Agent`, `Claude`, `Anthropic`, `Code-Review`, `Refactoring`, `PTY`, `Automation` |
| Сопутствующие навыки | [`codex`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-codex), [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent), [`opencode`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-opencode) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Claude Code — Руководство по оркестровке Hermes

Делегируйте задачи кодирования [Claude Code](https://code.claude.com/docs/en/cli-reference) (автономный интерфейс командной строки агента кодирования Anthropic) через терминал Hermes. Claude Code v2.x может читать файлы, писать код, запускать команды оболочки, создавать подагенты и автономно управлять рабочими процессами git.

## Предварительные условия

- **Установить:** `npm install -g @anthropic-ai/claude-code`
- **Аутентификация:** для входа в систему выполните `claude` один раз (OAuth браузера для Pro/Max или установите `ANTHROPIC_API_KEY`).
– **Консольная аутентификация:** `claude auth login --console` для выставления счетов за ключи API.
- **Аутентификация системы единого входа:** `claude auth login --sso` для Enterprise
- **Проверить статус:** `claude auth status` (JSON) или `claude auth status --text` (читабельный)
- **Проверка работоспособности:** `claude doctor` — проверяет работоспособность автоматического обновления и установки.
- **Проверка версии:** `claude --version` (требуется v2.x+)
- **Обновление:** `claude update` или `claude upgrade`.

## Два режима оркестровки

Гермес взаимодействует с Клодом Кодом двумя принципиально разными способами. Выбирайте исходя из задачи.

### Режим 1: Режим печати (`-p`) — неинтерактивный (ПРЕДПОЧТИТЕЛЬНЫЙ для большинства задач)

В режиме печати выполняется одноразовая задача, возвращается результат и завершается работа. PTY не требуется. Никаких интерактивных подсказок. Это самый чистый путь интеграции.

```
terminal(command="claude -p 'Add error handling to all API calls in src/' --allowedTools 'Read,Edit' --max-turns 10", workdir="/path/to/project", timeout=120)
```

**Когда использовать режим печати:**
- Одноразовые задачи по кодированию (исправить ошибку, добавить функцию, провести рефакторинг)
- Автоматизация CI/CD и создание сценариев.
- Извлечение структурированных данных с помощью `--json-schema`
- Конвейерная обработка ввода (`cat file | claude -p "analyze this"`)
- Любая задача, где не нужен многоходовой разговор

**В режиме печати пропускаются ВСЕ интерактивные диалоги** — нет запроса на доверие к рабочему пространству, нет подтверждений разрешений. Это делает его идеальным для автоматизации.

### Режим 2: Интерактивный PTY через tmux — многооборотные сеансы

Интерактивный режим предоставляет вам полноценный диалоговый REPL, в котором вы можете отправлять последующие запросы, использовать команды с косой чертой и наблюдать за работой Клода в реальном времени. **Требуется оркестровка tmux.**

```
# Start a tmux session
terminal(command="tmux new-session -d -s claude-work -x 140 -y 40")

# Launch Claude Code inside it
terminal(command="tmux send-keys -t claude-work 'cd /path/to/project && claude' Enter")

# Wait for startup, then send your task
# (after ~3-5 seconds for the welcome screen)
terminal(command="sleep 5 && tmux send-keys -t claude-work 'Refactor the auth module to use JWT tokens' Enter")

# Monitor progress by capturing the pane
terminal(command="sleep 15 && tmux capture-pane -t claude-work -p -S -50")

# Send follow-up tasks
terminal(command="tmux send-keys -t claude-work 'Now add unit tests for the new JWT code' Enter")

# Exit when done
terminal(command="tmux send-keys -t claude-work '/exit' Enter")
```

**Когда использовать интерактивный режим:**
- Многоходовая итеративная работа (рефакторинг → проверка → исправление → цикл тестирования)
- Задачи, требующие оперативного принятия решений человеком.
- Пробные сеансы кодирования.
- Когда вам нужно использовать слэш-команды Клода (`/compact`, `/review`, `/model`)

## Обработка диалога PTY (КРИТИЧНО для интерактивного режима)

Claude Code отображает до двух диалоговых окон подтверждения при первом запуске. Вы ДОЛЖНЫ обрабатывать их с помощью ключей отправки tmux:

### Диалог 1: Доверие к рабочей области (первое посещение каталога)
```
❯ 1. Yes, I trust this folder    ← DEFAULT (just press Enter)
  2. No, exit
```
**Обработка:** `tmux send-keys -t <session> Enter` — выбор по умолчанию правильный.

### Диалог 2: Предупреждение об обходе разрешений (только с --dangerous-skip-permissions)
```
❯ 1. No, exit                    ← DEFAULT (WRONG choice!)
  2. Yes, I accept
```
**Обработка:** Сначала необходимо перейти ВНИЗ, а затем Enter:
```
tmux send-keys -t <session> Down && sleep 0.3 && tmux send-keys -t <session> Enter
```

### Надежный шаблон обработки диалогов
```
# Launch with permissions bypass
terminal(command="tmux send-keys -t claude-work 'claude --dangerously-skip-permissions \"your task\"' Enter")

# Handle trust dialog (Enter for default "Yes")
terminal(command="sleep 4 && tmux send-keys -t claude-work Enter")

# Handle permissions dialog (Down then Enter for "Yes, I accept")
terminal(command="sleep 3 && tmux send-keys -t claude-work Down && sleep 0.3 && tmux send-keys -t claude-work Enter")

# Now wait for Claude to work
terminal(command="sleep 15 && tmux capture-pane -t claude-work -p -S -60")
```

**Примечание.** После первого принятия доверия для каталога диалоговое окно доверия больше не появится. Каждый раз, когда вы используете `--dangerously-skip-permissions`, появляется только диалоговое окно разрешений.

## Подкоманды CLI

| Подкоманда | Цель |
|------------|---------|
| `claude` | Запустить интерактивный REPL |
| `claude "query"` | Запустите REPL с начальной подсказкой |
| `claude -p "query"` | Режим печати (неинтерактивный, закрывается по завершении) |
| `cat file \| claude -p "query"` | Содержимое канала как контекст стандартного ввода |
| `claude -c` | Продолжить самый последний разговор в этом каталоге |
| `claude -r "id"` | Возобновить определенный сеанс по идентификатору или имени |
| `claude auth login` | Войдите (добавьте `--console` для выставления счетов API, `--sso` для Enterprise) |
| `claude auth status` | Проверить статус входа (возвращает JSON; `--text` для удобочитаемого состояния) |
| `claude mcp add <name> -- <cmd>` | Добавить сервер MCP |
| `claude mcp list` | Список настроенных серверов MCP |
| `claude mcp remove <name>` | Удаление сервера MCP |
| `claude agents` | Список настроенных агентов |
| `claude doctor` | Запуск проверки работоспособности при установке и автоматическое обновление |
| `claude update` / `claude upgrade` | Обновите Claude Code до последней версии |
| `claude remote-control` | Запустите сервер для управления Клодом из claude.ai или мобильного приложения |
| `claude install [target]` | Установить собственную сборку (стабильную, последнюю или конкретную версию) |
| `claude setup-token` | Настройте долгосрочный токен аутентификации (требуется подписка) |
| `claude plugin` / `claude plugins` | Управление плагинами Claude Code |
| `claude auto-mode` | Проверка конфигурации классификатора автоматического режима |

## Подробное описание режима печати

### Структурированный вывод JSON
```
terminal(command="claude -p 'Analyze auth.py for security issues' --output-format json --max-turns 5", workdir="/project", timeout=120)
```

Возвращает объект JSON с:
```json
{
  "type": "result",
  "subtype": "success",
  "result": "The analysis text...",
  "session_id": "75e2167f-...",
  "num_turns": 3,
  "total_cost_usd": 0.0787,
  "duration_ms": 10276,
  "stop_reason": "end_turn",
  "terminal_reason": "completed",
  "usage": { "input_tokens": 5, "output_tokens": 603, ... },
  "modelUsage": { "claude-sonnet-4-6": { "costUSD": 0.078, "contextWindow": 200000 } }
}
```

**Ключевые поля:** `session_id` для возобновления, `num_turns` для количества агентских циклов, `total_cost_usd` для отслеживания расходов, `subtype` для обнаружения успеха/ошибки (`success`, `error_max_turns`, `error_budget`).

### Потоковая передача вывода JSON
Для потоковой передачи токенов в реальном времени используйте `stream-json` с `--verbose`:
```
terminal(command="claude -p 'Write a summary' --output-format stream-json --verbose --include-partial-messages", timeout=60)
```

Возвращает события JSON, разделенные новой строкой. Фильтрация с помощью jq для живого текста:
```
claude -p "Explain X" --output-format stream-json --verbose --include-partial-messages | \
  jq -rj 'select(.type == "stream_event" and .event.delta.type? == "text_delta") | .event.delta.text'
```

События потока включают `system/api_retry` с полями `attempt`, `max_retries` и `error` (например, `rate_limit`, `billing_error`).

### Двунаправленная потоковая передача
Для потоковой передачи ввода и вывода в реальном времени:
```
claude -p "task" --input-format stream-json --output-format stream-json --replay-user-messages
```
`--replay-user-messages` повторно отправляет пользовательские сообщения на стандартный вывод для подтверждения.

### Конвейерный ввод
```
# Pipe a file for analysis
terminal(command="cat src/auth.py | claude -p 'Review this code for bugs' --max-turns 1", timeout=60)

# Pipe multiple files
terminal(command="cat src/*.py | claude -p 'Find all TODO comments' --max-turns 1", timeout=60)

# Pipe command output
terminal(command="git diff HEAD~3 | claude -p 'Summarize these changes' --max-turns 1", timeout=60)
```

### Схема JSON для структурированного извлечения
```
terminal(command="claude -p 'List all functions in src/' --output-format json --json-schema '{\"type\":\"object\",\"properties\":{\"functions\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}},\"required\":[\"functions\"]}' --max-turns 5", workdir="/project", timeout=90)
```

Выполните синтаксический анализ `structured_output` из результата JSON. Перед возвратом Клод проверяет вывод на соответствие схеме.

### Продолжение сеанса
```
# Start a task
terminal(command="claude -p 'Start refactoring the database layer' --output-format json --max-turns 10 > /tmp/session.json", workdir="/project", timeout=180)

# Resume with session ID
terminal(command="claude -p 'Continue and add connection pooling' --resume $(cat /tmp/session.json | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"session_id\"])') --max-turns 5", workdir="/project", timeout=120)

# Or resume the most recent session in the same directory
terminal(command="claude -p 'What did you do last time?' --continue --max-turns 1", workdir="/project", timeout=30)

# Fork a session (new ID, keeps history)
terminal(command="claude -p 'Try a different approach' --resume <id> --fork-session --max-turns 10", workdir="/project", timeout=120)
```

### Простой режим для CI/сценариев
```
terminal(command="claude --bare -p 'Run all tests and report failures' --allowedTools 'Read,Bash' --max-turns 10", workdir="/project", timeout=180)
```

`--bare` пропускает перехватчики, плагины, обнаружение MCP и загрузку CLAUDE.md. Самый быстрый запуск. Требуется `ANTHROPIC_API_KEY` (пропускает OAuth).

Чтобы выборочно загрузить контекст в простом режиме:
| Загрузить | Флаг |
|---------|------|
| Дополнения к системным подсказкам | `--append-system-prompt "text"` или `--append-system-prompt-file path` |
| Настройки | `--settings <file-or-json>` |
| MCP-серверы | `--mcp-config <file-or-json>` |
| Пользовательские агенты | `--agents '<json>'` |

### Резервная модель для перегрузки
```
terminal(command="claude -p 'task' --fallback-model haiku --max-turns 5", timeout=90)
```
Автоматически возвращается к указанной модели при перегрузке модели по умолчанию (только режим печати).

## Полное руководство по флагам CLI

### Сессия и среда
| Флаг | Эффект |
|------|--------|
| `-p, --print` | Неинтерактивный одноразовый режим (закрывается по завершении) |
| `-c, --continue` | Возобновить последний разговор в текущем каталоге |
| `-r, --resume <id>` | Возобновить определенный сеанс по идентификатору или имени (интерактивный выбор, если идентификатор отсутствует) |
| `--fork-session` | При возобновлении создайте новый идентификатор сеанса вместо повторного использования исходного |
| `--session-id <uuid>` | Используйте для разговора определенный UUID |
| `--no-session-persistence` | Не сохранять сеанс на диск (только в режиме печати) |
| `--add-dir <paths...>` | Предоставить Клоду доступ к дополнительным рабочим каталогам |
| `-w, --worktree [name]` | Запуск в изолированном рабочем дереве git по адресу `.claude/worktrees/<name>` |
| `--tmux` | Создайте сеанс tmux для рабочего дерева (требуется `--worktree`) |
| `--ide` | Автоматическое подключение к действующей IDE при запуске |
| `--chrome` / `--no-chrome` | Включить/отключить интеграцию браузера Chrome для веб-тестирования |
| `--from-pr [number]` | Возобновить сеанс, связанный с конкретным PR GitHub |
| `--file <specs...>` | Файловые ресурсы для загрузки при запуске (формат: `file_id:relative_path`) |

### Модель и производительность
| Флаг | Эффект |
|------|--------|
| `--model <alias>` | Выбор модели: `sonnet`, `opus`, `haiku` или полное имя, например `claude-sonnet-4-6` |
| `--effort <level>` | Глубина рассуждений: `low`, `medium`, `high`, `xhigh`, `max` |
| `--max-turns <n>` | Ограничить агентские циклы (только режим печати; предотвращает выход из-под контроля) |
| `--max-budget-usd <n>` | Ограничение расходов API в долларах (только в режиме печати) |
| `--fallback-model <model>` | Автоматический возврат при перегрузке модели по умолчанию (только режим печати) |
| `--betas <betas...>` | Бета-заголовки для включения в запросы API (только для пользователей ключа API) |

### Разрешение и безопасность
| Флаг | Эффект |
|------|--------|
| `--dangerously-skip-permissions` | Автоматически одобрять использование ВСЕХ инструментов (запись файлов, bash, сеть и т. д.) |
| `--allow-dangerously-skip-permissions` | Включите обход как *опцию*, не включая его по умолчанию |
| `--permission-mode <mode>` | `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions` |
| `--allowedTools <tools...>` | Специальные инструменты в белый список (через запятую или пробел) |
| `--disallowedTools <tools...>` | Специальные инструменты для черного списка |
| `--tools <tools...>` | Переопределить встроенный набор инструментов (`""` = нет, `"default"` = все или имена инструментов) |

### Формат вывода и ввода
| Флаг | Эффект |
|------|--------|
| `--output-format <fmt>` | `text` (по умолчанию), `json` (одиночный объект результата), `stream-json` (разделитель новой строки) |
| `--input-format <fmt>` | `text` (по умолчанию) или `stream-json` (потоковая передача в реальном времени) |
| `--json-schema <schema>` | Принудительно структурировать вывод JSON, соответствующий схеме |
| `--verbose` | Полный пошаговый вывод |
| `--include-partial-messages` | Включать частичные фрагменты сообщений по мере их поступления (stream-json + print) |
| `--replay-user-messages` | Повторно отправлять пользовательские сообщения на стандартный вывод (двунаправленный поток-json) |

### Системная подсказка и контекст
| Флаг | Эффект |
|------|--------|
| `--append-system-prompt <text>` | **Добавить** в системную подсказку по умолчанию (сохраняет встроенные возможности) |
| `--append-system-prompt-file <path>` | **Добавить** содержимое файла в системную подсказку по умолчанию |
| `--system-prompt <text>` | **Замените** всю системную подсказку (обычно используйте --append) |
| `--system-prompt-file <path>` | **Замените** системное приглашение содержимым файла |
| `--bare` | Пропустить перехватчики, плагины, обнаружение MCP, CLAUDE.md, OAuth (самый быстрый запуск) |
| `--agents '<json>'` | Динамическое определение пользовательских субагентов в формате JSON |
| `--mcp-config <path>` | Загрузить серверы MCP из файла JSON (повторяемо) |
| `--strict-mcp-config` | Используйте только серверы MCP от `--mcp-config`, игнорируя все остальные конфигурации MCP |
| `--settings <file-or-json>` | Загрузите дополнительные настройки из файла JSON или встроенного JSON |
| `--setting-sources <sources>` | Источники для загрузки, разделенные запятыми: `user`, `project`, `local` |
| `--plugin-dir <paths...>` | Загружать плагины из каталогов только для этого сеанса |
| `--disable-slash-commands` | Отключить все навыки/команды слэша |

### Отладка
| Флаг | Эффект |
|------|--------|
| `-d, --debug [filter]` | Включите ведение журнала отладки с помощью дополнительного фильтра категорий (например, `"api,hooks"`, `"!1p,!file"`) |
| `--debug-file <path>` | Записывать журналы отладки в файл (неявно включает режим отладки) |

### Команды агентов
| Флаг | Эффект |
|------|--------|
| `--teammate-mode <mode>` | Как отображаются команды агентов: `auto`, `in-process` или `tmux` |
| `--brief` | Включить инструмент `SendUserMessage` для связи агента с пользователем |

### Синтаксис имени инструмента для --allowedTools / --disallowedTools
```
Read                    # All file reading
Edit                    # File editing (existing files)
Write                   # File creation (new files)
Bash                    # All shell commands
Bash(git *)             # Only git commands
Bash(git commit *)      # Only git commit commands
Bash(npm run lint:*)    # Pattern matching with wildcards
WebSearch               # Web search capability
WebFetch                # Web page fetching
mcp__<server>__<tool>   # Specific MCP tool
```

## Настройки и конфигурация

### Иерархия настроек (от самого высокого до самого низкого приоритета)
1. **Флаги CLI** — переопределить все
2. **Локальный проект:** `.claude/settings.local.json` (личный, gitignored)
3. **Проект:** `.claude/settings.json` (общий, отслеживается в git)
4. **Пользователь:** `~/.claude/settings.json` (глобальный)

### Разрешения в настройках
```json
{
  "permissions": {
    "allow": ["Bash(npm run lint:*)", "WebSearch", "Read"],
    "ask": ["Write(*.ts)", "Bash(git push*)"],
    "deny": ["Read(.env)", "Bash(rm -rf *)"]
  }
}
```

### Файлы памяти (CLAUDE.md) Иерархия
1. **Глобально:** `~/.claude/CLAUDE.md` — применяется ко всем проектам.
2. **Проект:** `./CLAUDE.md` — контекст, специфичный для проекта (отслеживается git).
3. **Local:** `.claude/CLAUDE.local.md` — переопределения личного проекта (gitignored)

Используйте префикс `#` в интерактивном режиме, чтобы быстро добавить в память: `# Always use 2-space indentation`.

## Интерактивный сеанс: слэш-команды

### Сеанс и контекст
| Команда | Цель |
|---------|---------|
| `/help` | Показать все команды (включая пользовательские и MCP-команды) |
| `/compact [focus]` | Сжимать контекст для сохранения токенов; CLAUDE.md выдерживает сжатие. Например, `/compact focus on auth logic` |
| `/clear` | Очистите историю разговоров, чтобы начать все сначала |
| `/context` | Визуализируйте использование контекста в виде цветной сетки с советами по оптимизации |
| `/cost` | Просмотр использования токенов с разбивкой по моделям и попаданиям в кеш |
| `/resume` | Переключиться на другой сеанс или возобновить его |
| `/rewind` | Вернуться к предыдущей контрольной точке в разговоре или коде |
| `/btw <question>` | Задайте дополнительный вопрос, не увеличивая стоимость контекста |
| `/status` | Показать версию, подключение и информацию о сеансе |
| `/todos` | Список отслеживаемых действий из беседы |
| `/exit` или `Ctrl+D` | Завершить сеанс |

### Разработка и обзор
| Команда | Цель |
|---------|---------|
| `/review` | Запросить проверку кода на предмет текущих изменений |
| `/security-review` | Выполнение анализа безопасности текущих изменений |
| `/plan [description]` | Войдите в режим планирования с автоматическим запуском планирования задач |
| `/loop [interval]` | Запланируйте повторяющиеся задачи в рамках сеанса |
| `/batch` | Автоматическое создание рабочих деревьев для больших параллельных изменений (5–30 рабочих деревьев) |

### Конфигурация и инструменты
| Команда | Цель |
|---------|---------|
| `/model [model]` | Переключайте модели в середине сеанса (используйте клавиши со стрелками, чтобы отрегулировать усилие) |
| `/effort [level]` | Установите усилие рассуждения: `low`, `medium`, `high`, `xhigh` или `max` |
| `/init` | Создайте файл CLAUDE.md для памяти проекта |
| `/memory` | Открыть CLAUDE.md для редактирования |
| `/config` | Открыть конфигурацию интерактивных настроек |
| `/permissions` | Просмотр/обновление разрешений инструмента |
| `/agents` | Управление специализированными субагентами |
| `/mcp` | Интерактивный пользовательский интерфейс для управления серверами MCP |
| `/add-dir` | Добавить дополнительные рабочие каталоги (полезно для монорепозиториев) |
| `/usage` | Показать лимиты плана и статус ограничения тарифа |
| `/voice` | Включить голосовой режим «нажми и говори» (20 языков; удерживайте пробел для записи и отпустите для отправки) |
| `/release-notes` | Интерактивное средство выбора примечаний к выпуску версии |

### Пользовательские команды слэша
Создайте `.claude/commands/<name>.md` (общий для проекта) или `~/.claude/commands/<name>.md` (личный):

```markdown
# .claude/commands/deploy.md
Run the deploy pipeline:
1. Run all tests
2. Build the Docker image
3. Push to registry
4. Update the $ARGUMENTS environment (default: staging)
```

Использование: `/deploy production` — `$ARGUMENTS` заменяется вводом пользователя.

### Навыки (вызов естественного языка)
В отличие от косой черты (вызываемых вручную), навыки в `.claude/skills/` представляют собой руководства по уценке, которые Клод автоматически вызывает на естественном языке при совпадении задачи:

```markdown
# .claude/skills/database-migration.md
When asked to create or modify database migrations:
1. Use Alembic for migration generation
2. Always create a rollback function
3. Test migrations against a local database copy
```

## Интерактивный сеанс: сочетания клавиш

### Общие элементы управления
| Ключ | Действие |
|-----|--------|
| `Ctrl+C` | Отменить текущий ввод или генерацию |
| `Ctrl+D` | Выйти из сеанса |
| `Ctrl+R` | История команд обратного поиска |
| `Ctrl+B` | Фон выполняемой задачи |
| `Ctrl+V` | Вставить изображение в разговор |
| `Ctrl+O` | Режим транскрипта — см. мыслительный процесс Клода |
| `Ctrl+G` или `Ctrl+X Ctrl+E` | Открыть приглашение во внешнем редакторе |
| `Esc Esc` | Перемотка разговора или состояние кода/подведение итогов |

### Переключение режимов
| Ключ | Действие |
|-----|--------|
| `Shift+Tab` | Циклические режимы разрешений (Обычный → Автопринятие → План) |
| `Alt+P` | Модель переключателя |
| `Alt+T` | Переключить режим мышления |
| `Alt+O` | Переключить быстрый режим |

### Многострочный ввод
| Ключ | Действие |
|-----|--------|
| `\` + `Enter` | Быстрый перевод строки |
| `Shift+Enter` | Новая строка (альтернативный вариант) |
| `Ctrl+J` | Новая строка (альтернативный вариант) |

### Входные префиксы
| Префикс | Действие |
|--------|--------|
| `!` | Выполните bash напрямую, минуя AI (например, `!npm test`). Используйте только `!` для переключения режима оболочки. |
| `@` | Ссылка на файлы/каталоги с автозаполнением (например, `@./src/api/`) |
| `#` | Быстрое добавление в память CLAUDE.md (например, `# Use 2-space indentation`) |
| `/` | Слэш-команды |

### Совет профессионала: «ультрамыслие»
Используйте ключевое слово «ультрамыслие» в подсказке, чтобы максимально усилить рассуждения в конкретном ходу. Это активирует режим самого глубокого мышления независимо от текущей настройки `/effort`.

## Шаблон PR-обзора

### Быстрый обзор (режим печати)
```
terminal(command="cd /path/to/repo && git diff main...feature-branch | claude -p 'Review this diff for bugs, security issues, and style problems. Be thorough.' --max-turns 1", timeout=60)
```

### Глубокий обзор (интерактив + рабочее дерево)
```
terminal(command="tmux new-session -d -s review -x 140 -y 40")
terminal(command="tmux send-keys -t review 'cd /path/to/repo && claude -w pr-review' Enter")
terminal(command="sleep 5 && tmux send-keys -t review Enter")  # Trust dialog
terminal(command="sleep 2 && tmux send-keys -t review 'Review all changes vs main. Check for bugs, security issues, race conditions, and missing tests.' Enter")
terminal(command="sleep 30 && tmux capture-pane -t review -p -S -60")
```

### Пиар-обзор от номера
```
terminal(command="claude -p 'Review this PR thoroughly' --from-pr 42 --max-turns 10", workdir="/path/to/repo", timeout=120)
```

### Клод Ворктри с tmux
```
terminal(command="claude -w feature-x --tmux", workdir="/path/to/repo")
```
Создает изолированное рабочее дерево git по адресу `.claude/worktrees/feature-x` И сеанс tmux для него. Использует собственные панели iTerm2, если они доступны; добавьте `--tmux=classic` для традиционного tmux.

## Параллельные экземпляры Claude

Запускайте несколько независимых задач Claude одновременно:

```
# Task 1: Fix backend
terminal(command="tmux new-session -d -s task1 -x 140 -y 40 && tmux send-keys -t task1 'cd ~/project && claude -p \"Fix the auth bug in src/auth.py\" --allowedTools \"Read,Edit\" --max-turns 10' Enter")

# Task 2: Write tests
terminal(command="tmux new-session -d -s task2 -x 140 -y 40 && tmux send-keys -t task2 'cd ~/project && claude -p \"Write integration tests for the API endpoints\" --allowedTools \"Read,Write,Bash\" --max-turns 15' Enter")

# Task 3: Update docs
terminal(command="tmux new-session -d -s task3 -x 140 -y 40 && tmux send-keys -t task3 'cd ~/project && claude -p \"Update README.md with the new API endpoints\" --allowedTools \"Read,Edit\" --max-turns 5' Enter")

# Monitor all
terminal(command="sleep 30 && for s in task1 task2 task3; do echo '=== '$s' ==='; tmux capture-pane -t $s -p -S -5 2>/dev/null; done")
```

## CLAUDE.md — Файл контекста проекта

Claude Code автоматически загружает `CLAUDE.md` из корня проекта. Используйте его для сохранения контекста проекта:

```markdown
# Project: My API

## Architecture
- FastAPI backend with SQLAlchemy ORM
- PostgreSQL database, Redis cache
- pytest for testing with 90% coverage target

## Key Commands
- `make test` — run full test suite
- `make lint` — ruff + mypy
- `make dev` — start dev server on :8000

## Code Standards
- Type hints on all public functions
- Docstrings in Google style
- 2-space indentation for YAML, 4-space for Python
- No wildcard imports
```

**Будьте конкретны.** Вместо «Пишите хороший код» используйте «Использовать отступы в два пробела для JS» или «Называйте тестовые файлы с суффиксом `.test.ts`». Специальные инструкции позволяют сэкономить циклы коррекции.

### Каталог правил (Модульный CLAUDE.md)
Для проектов с большим количеством правил используйте каталог правил вместо одного огромного CLAUDE.md:
- **Правила проекта:** `.claude/rules/*.md` — общедоступны, отслеживаются git.
- **Правила пользователя:** `~/.claude/rules/*.md` — персональные, глобальные.

Каждый файл `.md` в каталоге правил загружается как дополнительный контекст. Это чище, чем запихивать все в один CLAUDE.md.

### Автозапоминание
Клод автоматически сохраняет изученный контекст проекта в `~/.claude/projects/<project>/memory/`.
– **Ограничение:** 25 КБ или 200 строк на проект.
- Это отдельно от CLAUDE.md — это собственные заметки Клода о проекте, накопленные за время сессий.

## Пользовательские субагенты

Определите специализированные агенты в `.claude/agents/` (проект), `~/.claude/agents/` (личный) или с помощью флага CLI `--agents` (сеанс):

### Приоритет местоположения агента
1. `.claude/agents/` — уровень проекта, общий доступ для всей команды
2. `--agents` Флаг CLI — зависящий от сеанса, динамический.
3. `~/.claude/agents/` — уровень пользователя, личный

### Создание агента
```markdown
# .claude/agents/security-reviewer.md
---
name: security-reviewer
description: Security-focused code review
model: opus
tools: [Read, Bash]
---
You are a senior security engineer. Review code for:
- Injection vulnerabilities (SQL, XSS, command injection)
- Authentication/authorization flaws
- Secrets in code
- Unsafe deserialization
```

Вызов через: `@security-reviewer review the auth module`

### Динамические агенты через CLI
```
terminal(command="claude --agents '{\"reviewer\": {\"description\": \"Reviews code\", \"prompt\": \"You are a code reviewer focused on performance\"}}' -p 'Use @reviewer to check auth.py'", timeout=120)
```

Клод может управлять несколькими агентами: «Используйте @db-expert для оптимизации запросов, затем @security для аудита изменений».

## Хуки — автоматизация событий

Настройте в `.claude/settings.json` (проект) или `~/.claude/settings.json` (глобально):

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Write(*.py)",
      "hooks": [{"type": "command", "command": "ruff check --fix $CLAUDE_FILE_PATHS"}]
    }],
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{"type": "command", "command": "if echo \"$CLAUDE_TOOL_INPUT\" | grep -q 'rm -rf'; then echo 'Blocked!' && exit 2; fi"}]
    }],
    "Stop": [{
      "hooks": [{"type": "command", "command": "echo 'Claude finished a response' >> /tmp/claude-activity.log"}]
    }]
  }
}
```

### Все 8 типов крючков
| Крюк | Когда он срабатывает | Общее использование |
|------|--------------|------------|
| `UserPromptSubmit` | Прежде чем Клод обработает приглашение пользователя | Проверка ввода, журналирование |
| `PreToolUse` | Перед выполнением инструмента | Ворота безопасности, блокируют опасные команды (выход 2 = блокировать) |
| `PostToolUse` | После завершения работы инструмента | Автоформатирование кода, запуск линтеров |
| `Notification` | При запросах разрешения или ожидании ввода | Уведомления и оповещения на рабочем столе |
| `Stop` | Когда Клод заканчивает отвечать | Журнал завершения, обновления статуса |
| `SubagentStop` | Когда субагент завершает работу | Оркестровка агента |
| `PreCompact` | Перед очисткой контекстной памяти | Транскрипты сеансов резервного копирования |
| `SessionStart` | Когда начинается сеанс | Загрузить контекст разработки (например, `git status`) |

### Перехват переменных среды
| Переменная | Содержание |
|----------|---------|
| `CLAUDE_PROJECT_DIR` | Текущий путь проекта |
| `CLAUDE_FILE_PATHS` | Изменяемые файлы |
| `CLAUDE_TOOL_INPUT` | Параметры инструмента в формате JSON |

### Примеры перехватчиков безопасности
```json
{
  "PreToolUse": [{
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": "if echo \"$CLAUDE_TOOL_INPUT\" | grep -qE 'rm -rf|git push.*--force|:(){ :|:& };:'; then echo 'Dangerous command blocked!' && exit 2; fi"}]
  }]
}
```

## Интеграция MCP

Добавьте внешние серверы инструментов для баз данных, API и сервисов:

```
# GitHub integration
terminal(command="claude mcp add -s user github -- npx @modelcontextprotocol/server-github", timeout=30)

# PostgreSQL queries
terminal(command="claude mcp add -s local postgres -- npx @anthropic-ai/server-postgres --connection-string postgresql://localhost/mydb", timeout=30)

# Puppeteer for web testing
terminal(command="claude mcp add puppeteer -- npx @anthropic-ai/server-puppeteer", timeout=30)
```

### Области MCP
| Флаг | Область применения | Хранение |
|------|-------|---------|
| `-s user` | Глобальный (все проекты) | `~/.claude.json` |
| `-s local` | Этот проект (личный) | `.claude/settings.local.json` (gitignored) |
| `-s project` | Этот проект (совместно с командой) | `.claude/settings.json` (отслеживается git) |

### MCP в режиме печати/CI
```
terminal(command="claude --bare -p 'Query database' --mcp-config mcp-servers.json --strict-mcp-config", timeout=60)
```
`--strict-mcp-config` игнорирует все серверы MCP, кроме серверов `--mcp-config`.

Ссылка на ресурсы MCP в чате: `@github:issue://123`

### Ограничения и настройка MCP
- **Описания инструментов**: ограничение в 2 КБ на сервер для описаний инструментов и инструкций для сервера.
- **Размер результата:** Ограничено по умолчанию; используйте аннотацию `maxResultSizeChars`, чтобы разрешить до **500 КБ** символов для больших выходных данных
- **Токены вывода:** `export MAX_MCP_OUTPUT_TOKENS=50000` — ограничение вывода с серверов MCP, чтобы предотвратить переполнение контекста.
- **Транспорт:** `stdio` (локальный процесс), `http` (удаленный), `sse` (события, отправленные сервером)

## Мониторинг интерактивных сессий

### Чтение статуса TUI
```
# Periodic capture to check if Claude is still working or waiting for input
terminal(command="tmux capture-pane -t dev -p -S -10")
```

Ищите эти показатели:
- `❯` внизу = ожидание вашего ответа (Клод закончил или задает вопрос)
- `●` строк = Клод активно использует инструменты (чтение, запись, запуск команд)
- `⏵⏵ bypass permissions on` = строка состояния, показывающая режим разрешений.
- `◐ medium · /effort` = текущий уровень усилий в строке состояния.
- `ctrl+o to expand` = выходные данные инструмента были усечены (можно расширить в интерактивном режиме)

### Состояние контекстного окна
Используйте `/context` в интерактивном режиме, чтобы увидеть цветную сетку использования контекста. Ключевые пороговые значения:
- **&lt; 70%** — Нормальная работа, полная точность
- **70–85%** — Точность начинает падать, учитывайте `/compact`
- **> 85%** — риск галлюцинаций значительно возрастает, используйте `/compact` или `/clear`.

## Переменные среды

| Переменная | Эффект |
|----------|--------|
| `ANTHROPIC_API_KEY` | API-ключ для аутентификации (альтернатива OAuth) |
| `CLAUDE_CODE_EFFORT_LEVEL` | Усилие по умолчанию: `low`, `medium`, `high`, `max` или `auto` |
| `MAX_THINKING_TOKENS` | Ограничить количество жетонов мышления (установите значение `0`, чтобы полностью отключить мышление) |
| `MAX_MCP_OUTPUT_TOKENS` | Ограничить вывод с серверов MCP (по умолчанию варьируется; установите, например, `50000`) |
| `CLAUDE_CODE_NO_FLICKER=1` | Включите рендеринг на альтернативном экране, чтобы устранить мерцание терминала |
| `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` | Удаление учетных данных из подпроцессов в целях безопасности |

## Советы по стоимости и производительности

1. **Используйте `--max-turns`** в режиме печати, чтобы предотвратить возникновение неконтролируемых циклов. Для большинства задач начните с 5–10.
2. **Используйте `--max-budget-usd`** для ограничения затрат. Примечание: минимум ~$0,05 для создания кэша системных подсказок.
3. **Используйте `--effort low`** для простых задач (быстрее, дешевле). `high` или `max` для сложных рассуждений.
4. **Используйте `--bare`** для CI/сценариев, чтобы пропустить накладные расходы на обнаружение плагинов/перехватчиков.
5. **Используйте `--allowedTools`**, чтобы ограничить только то, что необходимо (например, `Read` только для отзывов).
6. **Используйте `/compact`** в интерактивных сеансах, когда контекст становится большим.
7. **Конвейерный ввод** вместо чтения файлов Клодом, когда вам просто нужен анализ известного контента.
8. **Используйте `--model haiku`** для простых задач (дешевле) и `--model opus` для сложных многоэтапных работ.
9. **Используйте `--fallback-model haiku`** в режиме печати, чтобы корректно обрабатывать перегрузку модели.
10. **Начинайте новые сеансы для отдельных задач** — сеансы длятся 5 часов; Свежий контекст более эффективен.
11. **Используйте `--no-session-persistence`** в CI, чтобы избежать накопления сохраненных сеансов на диске.

## Подводные камни и проблемы

1. **Интерактивный режим ТРЕБУЕТ tmux** — Claude Code — это полноценное приложение TUI. Использование только `pty=true` в терминале Hermes работает, но tmux дает вам `capture-pane` для мониторинга и `send-keys` для ввода, что важно для оркестровки.
2. В диалоговом окне **`--dangerously-skip-permissions` по умолчанию установлено значение «Нет, выход»** — для принятия необходимо нажать «Вниз», а затем «Ввод». Режим печати (`-p`) полностью пропускает это.
3. **`--max-budget-usd` минимум составляет ~0,05$** — столько же стоит создание системного кэша подсказок. Установка меньшего значения немедленно приведет к ошибке.
4. **`--max-turns` работает только в режиме печати** — игнорируется в интерактивных сеансах.
5. **Клод может использовать `python` вместо `python3`** — в системах без символической ссылки `python` команды bash Клода не сработают с первой попытки, но исправятся самостоятельно.
6. **Для возобновления сеанса требуется тот же каталог** — `--continue` находит самый последний сеанс для текущего рабочего каталога.
7. **`--json-schema` необходимо достаточное количество `--max-turns`** — Клод должен прочитать файлы перед созданием структурированного вывода, что занимает несколько ходов.
8. **Диалоговое окно «Доверие» появляется только один раз для каждого каталога** — только в первый раз, затем кэшируется.
9. **Фоновые сеансы tmux сохраняются** — по завершении всегда очищайте с помощью `tmux kill-session -t <name>`.
10. **Команды с косой чертой (например, `/commit`) работают только в интерактивном режиме** — в режиме `-p` вместо этого опишите задачу на естественном языке.
11. **`--bare` пропускает OAuth** — в настройках требуется `ANTHROPIC_API_KEY` env var или `apiKeyHelper`.
12. **Ухудшение контекста реально** — качество вывода ИИ заметно ухудшается при использовании контекстного окна более 70%. Контролируйте с помощью `/context` и активно `/compact`.

## Правила для агентов Гермеса

1. **Предпочитайте режим печати (`-p`) для отдельных задач** — более чистый, без обработки диалогов, структурированный вывод
2. **Используйте tmux для многооборотной интерактивной работы** — единственный надежный способ организовать TUI
3. **Всегда устанавливайте `workdir`** — Клод сосредоточится на нужном каталоге проекта.
4. **Установите `--max-turns` в режиме печати** — предотвращает бесконечные циклы и неконтролируемые затраты.
5. **Отслеживание сеансов tmux** — используйте `tmux capture-pane -t <session> -p -S -50` для проверки прогресса.
6. **Найдите приглашение `❯`** — указывает, что Клод ожидает ввода (выполнено или задает вопрос).
7. **Очистка сеансов tmux** — завершите их после завершения, чтобы избежать утечек ресурсов.
8. **Сообщить о результатах пользователю** — после завершения подведите итог того, что сделал Клод и что изменилось.
9. **Не уничтожайте медленные сеансы** — Клод, возможно, выполняет многоэтапную работу; вместо этого проверьте прогресс
10. **Используйте `--allowedTools`** — ограничьте возможности тем, что действительно необходимо задаче.