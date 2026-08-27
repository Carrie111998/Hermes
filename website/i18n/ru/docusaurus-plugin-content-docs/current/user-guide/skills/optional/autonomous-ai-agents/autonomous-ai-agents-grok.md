---
title: Grok — делегирование кодирования в интерфейс командной строки xAI Grok Build
  (функции, PR)
sidebar_label: Grok
description: Делегирование кодирования в интерфейсе командной строки xAI Grok Build
  (функции, PR)
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Грок

Делегируйте кодирование в интерфейс командной строки xAI Grok Build (функции, PR).

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/autonomous-ai-agents/grok` |
| Путь | `optional-skills/autonomous-ai-agents/grok` |
| Версия | `0.1.1` |
| Автор | Мэтт Максимо (MattMaximo), агент Hermes |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Coding-Agent`, `Grok`, `xAI`, `Code-Review`, `Refactoring`, `Automation` |
| Сопутствующие навыки | [`codex`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-codex), [`claude-code`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code), [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Grok Build CLI — Руководство по оркестрации Hermes

Делегируйте задачи по кодированию компании [Grok Build](https://docs.x.ai/build/overview) (xAI
CLI автономного агента кодирования, команда `grok`) через терминал Hermes. Грок
может читать файлы, писать код, запускать команды оболочки, создавать субагенты и управлять git
рабочие процессы. Он работает тремя способами: интерактивный TUI, **безголовый** (`-p`) и как
**Агент ACP** через JSON-RPC.

Это третий брат `codex` и `claude-code`. Оркестровка
шаблон практически идентичен — **предпочитайте безголовый `-p` для одиночных выстрелов**, используйте PTY
для интерактивных занятий.

## Когда использовать

- Особенности здания
- Рефакторинг
- PR-обзоры
- Пакетное исправление проблем
- Любая задача, где в противном случае вы бы использовали Кодекс / Код Клода, но хотели Грока.

## Предварительные условия

- **Установить (предпочтительно):** `npm install -g @xai-official/grok`
  - Также официальный установщик `curl -fsSL https://x.ai/cli/install.sh | bash`
    работает, но хост `x.ai` в некоторых средах изолирован от Cloudflare.
    Путь npm полностью позволяет избежать этой зависимости.
- **Аутентификация — подписка SuperGrok / X Premium+ (основной путь):**
  – Запустите `grok login` один раз → откроется браузер для OAuth → токен будет кэширован.
    `~/.grok/auth.json`. Для этого используется ваша подписка **SuperGrok или X Premium+**.
    (нет биллинга API за токен).
  – Проверьте состояние входа, найдя `~/.grok/auth.json`, или запустите дешевый
    тест на дым без головы: `grok --no-auto-update -p "Say ok."`
  – В TUI `/logout` выходит из системы, а `/login` (или перезапускается) снова входит в систему.
- **Репозиторий git не требуется** — в отличие от Codex, Grok прекрасно работает вне git.
  каталог (подходит для задач по очистке/выбрасыванию).
- **Claude Code / AGENTS.md совместим с нулевой конфигурацией** — Grok читается автоматически
  `CLAUDE.md`, `.claude/` (навыки, агенты, MCP, ловушки, правила) и
  Семья `AGENTS.md`. Существующий контекст проекта просто работает.

> **Резервный API-ключ (не по умолчанию для этого пользователя):** Grok также поддерживает
> установка переменной среды `XAI_API_KEY` для выставления счетов с оплатой по мере использования
> через `api.x.ai`. Используйте только
> это если `grok login`/аутентификация SuperGrok недоступна. Путь подписки
> (`grok login`) — это предполагаемая настройка.

## Два режима оркестровки

### Режим 1: Безголовый (`-p`) — Неинтерактивный (ПРЕПЯТСТВУЮЩИЙ)

Запускает одноразовую задачу, печатает результат и завершает работу. Нет PTY, нет интерактивности
диалоги для навигации. Это самый чистый путь интеграции — аналог
`claude -p` и `codex exec`.

```
terminal(command="grok --no-auto-update -p 'Add a dark mode toggle to settings'", workdir="/path/to/project", timeout=180)
```

Всегда передавайте `--no-auto-update` в автоматическом режиме, чтобы пропустить фоновые проверки обновлений.

**Когда использовать безголовый режим:**
- Одноразовые задачи по кодированию (исправить ошибку, добавить функцию, провести рефакторинг)
- Автоматизация CI/CD и создание сценариев.
— Анализ структурированного вывода с помощью `--output-format json`.
- Любая задача, не требующая многоходового разговора

### Режим 2: Интерактивный PTY — многооборотные сеансы TUI

TUI — это полноэкранное интерактивное приложение с мышью. Управляйте им с помощью `pty=true`. Для
надежный мониторинг/ввод с использованием tmux (тот же шаблон, что и для навыка `claude-code`).

```
# Launch in a tmux session for capture-pane monitoring
terminal(command="tmux new-session -d -s grok-work -x 140 -y 40")
terminal(command="tmux send-keys -t grok-work 'cd /path/to/project && grok' Enter")

# Wait for startup, then send a task
terminal(command="sleep 5 && tmux send-keys -t grok-work 'Refactor the auth module to use JWT' Enter")

# Monitor progress
terminal(command="sleep 15 && tmux capture-pane -t grok-work -p -S -50")

# Exit when done
terminal(command="tmux send-keys -t grok-work '/quit' Enter && sleep 1 && tmux kill-session -t grok-work")
```

**Совет для вывода без заголовка, но в строке:** если вам нужен вывод в стиле TUI без
полноэкранный альтернативный экран (например, для очистки журналов), добавьте `--no-alt-screen`.
Что касается чистой автоматизации, безголовый `-p` по-прежнему чище, чем TUI.

## Глубокое погружение без головы

### Общие флаги

| Флаг | Эффект |
|------|--------|
| `-p, --single <PROMPT>` | Отправить одно приглашение, запустить без головы, выйти |
| `-m, --model <MODEL>` | Выберите модель |
| `-s, --session-id <UUID>` | Назначьте **НОВЫЙ** действительный UUID новому разговору (он еще не должен существовать). **Не** возобновляется — используйте для этого `--resume`/`--continue`. Действует только с `--resume`/`--continue` в сочетании с `--fork-session` |
| `-r, --resume [<UUID>]` | Возобновить существующий сеанс по его UUID (или самому последнему, если он опущен) |
| `-c, --continue` | Продолжить последний сеанс в текущем каталоге |
| `--fork-session` | При возобновлении создайте новый идентификатор сеанса вместо повторного использования исходного |
| `--max-turns <N>` | Ограничить максимальное количество ходов агента |
| `--cwd <PATH>` | Установить рабочий каталог |
| `--output-format <FMT>` | `plain` (по умолчанию), `json` или `streaming-json` |
| `--always-approve` | Автоматически утверждать все исполнения инструмента (эквивалент `--full-auto` / `--yolo`) |
| `--no-alt-screen` | Запуск в режиме онлайн, без полноэкранного использования TUI |
| `--no-auto-update` | Пропустить фоновые проверки обновлений (используется во всей автоматизации; скрыто в `--help`, но все еще работает) |

### Выходные форматы

- `plain` — удобочитаемый текст (по умолчанию)
- `json` — один объект JSON в конце выполнения (чистый анализ результата)
- `streaming-json` — события JSON, разделенные новой строкой, по мере их поступления.

```
# Structured result for parsing
terminal(command="grok --no-auto-update -p 'List all TODO comments in src/' --output-format json", workdir="/project", timeout=120)

# Auto-approve for autonomous building
terminal(command="grok --no-auto-update --always-approve -p 'Refactor the database layer and run the tests'", workdir="/project", timeout=300)
```

### Фоновый режим (длинные задачи)

```
# Start headless in background
terminal(command="grok --no-auto-update --always-approve -p 'Refactor the auth module'", workdir="/project", background=true, notify_on_complete=true)
# Returns session_id

# Monitor
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Kill if needed
process(action="kill", session_id="<id>")
```

Для интерактивного (TUI) фонового сеанса используйте `pty=true` + tmux и монитор
с `tmux capture-pane`, точно так же, как навыки `claude-code` / `codex`.

### Продолжение сеанса

Сессии идентифицируются по **UUID**, а не по имени. `--session-id` назначает *новый* UUID
к новому запуску (он **не** возобновляется); `--resume` принимает данные существующего сеанса
UUID (или опустите значение, чтобы возобновить самое последнее).

```
# Start a session with a self-assigned UUID (must be a valid, unused UUID)
SID=$(uuidgen)
terminal(command="grok --no-auto-update -s $SID -p 'Start refactoring the database layer' --always-approve", workdir="/project", timeout=240)

# Resume that exact session later by its UUID
terminal(command="grok --no-auto-update -r $SID -p 'Now add connection pooling' --always-approve", workdir="/project", timeout=180)

# Or just continue the most recent session in this directory (no UUID needed)
terminal(command="grok --no-auto-update -c -p 'What did you change last time?'", workdir="/project", timeout=60)
```

## Аудит только для чтения → Шаблон примечания Markdown

Чтобы Грок проверил локальные артефакты и вернул чистую уценку (для
Obsidian или репозиторий), ничего не мутируя:

1. Сначала подготовьте стабильные входные файлы с помощью инструментов Hermes (`read_file`,
   `write_file`). Лучше сделать снимок только соответствующего контекста во временный файл.
   чем сбрасывать необработанные пути.
2. Запустите Grok без заголовка **без** `--always-approve`, чтобы он не мог выполнять автоматическую запись, и
   требуйте `markdown only, no preamble`.
3. Сохраните стандартный вывод Грока прямо в заметку назначения с помощью `write_file()`.

```
grok --no-auto-update -p "Read /tmp/current.md and /tmp/inventory.md. Produce markdown only, no preamble. Output a clean note titled 'Cleanup Review'." --output-format plain
```

**Подводный камень (так же, как и в коде Клода):** при переписывании документа можно использовать свободную команду «переписать это».
приглашение может вернуть сводку изменений вместо полного файла. Вместо этого:
файл и потребовать «Вернуть ТОЛЬКО полный пересмотренный документ уценки». Нет вступления,
никаких объяснений, никаких ограничений кода. Начните немедленно с «# Title». Проверьте
первые строки с `read_file()` перед перезаписью места назначения.

## Шаблоны PR-обзоров

### Быстрый обзор (без головы)

```
terminal(command="cd /path/to/repo && git diff main...feature-branch | grok --no-auto-update -p 'Review this diff for bugs, security issues, and style problems. Be thorough.'", timeout=120)
```

### Проверка клонирования во временную версию (безопасно, без мутаций репо)

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && gh pr checkout 42 && grok --no-auto-update -p 'Review the changes vs origin/main. Check bugs, security, race conditions, missing tests.'", pty=true, timeout=300)
```

### Опубликовать отзыв

```
terminal(command="gh pr comment 42 --body '<review text>'", workdir="/path/to/repo")
```

## Параллельное устранение проблем с помощью рабочих деревьев

```
# Create worktrees
terminal(command="git worktree add -b fix/issue-78 /tmp/issue-78 main", workdir="~/project")
terminal(command="git worktree add -b fix/issue-99 /tmp/issue-99 main", workdir="~/project")

# Launch Grok headless in each (background)
terminal(command="grok --no-auto-update --always-approve -p 'Fix issue #78: <description>. Commit when done.'", workdir="/tmp/issue-78", background=true, notify_on_complete=true)
terminal(command="grok --no-auto-update --always-approve -p 'Fix issue #99: <description>. Commit when done.'", workdir="/tmp/issue-99", background=true, notify_on_complete=true)

# Monitor
process(action="list")

# After completion: push and open PRs
terminal(command="cd /tmp/issue-78 && git push -u origin fix/issue-78")
terminal(command="gh pr create --repo user/repo --head fix/issue-78 --title 'fix: ...' --body '...'")

# Cleanup
terminal(command="git worktree remove /tmp/issue-78", workdir="~/project")
```

## Полезные подкоманды и команды TUI

| Команда | Цель |
|---------|---------|
| `grok` | Запустите интерактивный TUI |
| `grok -p "query"` | Безголовый одноразовый |
| `grok login` / `grok logout` | Вход/выход (SuperGrok/X Premium+ OAuth) |
| `grok inspect` | Покажите, что Грок обнаружил в cwd: исходники конфигурации, инструкции, навыки, плагины, хуки, серверы MCP |
| `grok agent stdio` | Запуск в качестве агента ACP через JSON-RPC (для интеграции IDE/инструментов) |
| `grok update` | Обновите CLI (требуется хост `x.ai`; пропустите автоматизацию) |

Слэш-команды TUI (только в интерактивном режиме): `/model <name>`, `/always-approve`,
`/plan`, `/context`, `/compact`, `/resume`, `/sessions`, `/fork`, `/usage`,
`/quit`. `Shift+Tab` переключает режимы сеанса (включая режим «План», который блокирует
средства записи, кроме файла плана сеанса).

## Конфигурация (`~/.grok/config.toml`)

```toml
[cli]
auto_update = false          # skip background update checks persistently

[ui]
permission_mode = "ask"      # or "always-approve" to skip tool prompts by default

[models]
default = "grok-build-0.1"
```

Поместите глобальные настройки в `~/.grok/config.toml` (не в рамках проекта).
`.grok/config.toml`). `permission_mode` заменяет устаревший `approval_mode` /
`yolo = true` ключей.

## Подводные камни и проблемы

1. **Аутентификация осуществляется по подписке.** `grok login` требует SuperGrok или X
   Подписка Премиум+. Если вход невозможен или `~/.grok/auth.json` отсутствует,
   убедитесь, что подписка активна, прежде чем вернуться к `XAI_API_KEY`.
2. **Не путайте аутентификацию xAI Hermes с аутентификацией `grok` CLI.** Hermes
   `x_search` работает на собственном xAI OAuth; автономный интерфейс командной строки `grok` имеет
   отдельный токен в `~/.grok/auth.json`. Рабочий `x_search` НЕ означает
   `grok` вошел в систему.
3. **Всегда передайте `--no-auto-update` в автоматическом режиме** — иначе Грок позвонит домой.
   для проверки обновлений (и `x.ai`/`storage.googleapis.com` может быть недоступен).
4. **Предпочитайте npm install установщику Curl** — `npm install -g
   Хозяин @xai-official/grok` avoids the Cloudflare-walled `x.ai`.
5. **`--always-approve` — переключатель автономной сборки.** Без него — безголовый
   запуск может застопориться в ожидании запросов на одобрение инструмента. Опустите это намеренно, потому что
   просмотр/аудит только для чтения, поэтому Грок не может изменять файлы.
6. **Headless `-p` пропускает диалоги TUI**; TUI требуется `pty=true` (+ tmux для
   мониторинг), как и Клод Код.
7. **Используйте `--no-alt-screen`**, если вы запускаете встроенный TUI и полноэкранный режим.
   Перехват альтернативного экрана искажает захваченный вывод.
8. **Репозиторий git не требуется**, но для рабочих процессов PR/фиксации он вам все равно нужен — используйте
   `mktemp -d && git init` для задач с нуля.
9. **Очистите сеансы tmux** с помощью `tmux kill-session -t <name>` после завершения.

## Правила для агентов Гермеса

1. **Предпочитайте headless `-p`** для отдельных задач — максимально чистая интеграция, структурированная
   вывод через `--output-format json`.
2. **Всегда устанавливайте `workdir`** (или `--cwd`), чтобы Grok выбрал правильный проект.
3. **Передавайте `--no-auto-update`** при каждом автоматическом вызове.
4. **Используйте `--always-approve` только тогда, когда Грок должен писать автономно**; опустить это
   для обзоров и аудитов, доступных только для чтения.
5. **Фоновые длительные задачи** с `background=true, notify_on_complete=true` и
   мониторить с помощью инструмента `process`.
6. **Используйте tmux для многооборотной интерактивной работы** и отслеживайте с помощью
   `tmux capture-pane -t <session> -p -S -50`.
7. **Проверьте аутентификацию, прежде чем полагаться на нее** — проверьте `~/.grok/auth.json` или запустите
   дешевый тест на дым `grok -p "Say ok."`; не думайте, что аутентификация xAI Гермеса поддерживает
   закончилось.
8. **Сообщить о результатах пользователю** — подведите итог, что Грок изменил и что осталось.