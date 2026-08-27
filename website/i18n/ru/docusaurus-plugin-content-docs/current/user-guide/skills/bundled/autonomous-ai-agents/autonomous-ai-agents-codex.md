---
title: Codex — делегирование кодирования в интерфейс командной строки OpenAI Codex
  (функции, PR)
sidebar_label: Codex
description: Делегирование кодирования в OpenAI Codex CLI (функции, PR)
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Кодекс

Делегирование кодирования в OpenAI Codex CLI (функции, PR).

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/autonomous-ai-agents/codex` |
| Версия | `1.0.1` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Coding-Agent`, `Codex`, `OpenAI`, `Code-Review`, `Refactoring` |
| Сопутствующие навыки | [`claude-code`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code), [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Интерфейс командной строки Кодекса

Делегируйте задачи кодирования в [Кодекс](https://github.com/openai/codex) через терминал Hermes. Codex — это автономный CLI агента кодирования OpenAI.

## Когда использовать

- Особенности здания
- Рефакторинг
- PR-обзоры
- Пакетное исправление проблем

Требуется интерфейс командной строки кодекса и репозиторий git.

## Предварительные условия

- Установлен Кодекс: `npm install -g @openai/codex`
- Настроена аутентификация OpenAI: учетные данные `OPENAI_API_KEY` или Codex OAuth.
  из процесса входа в интерфейс командной строки Кодекса
- **Должен запускаться внутри репозитория git** — Кодекс отказывается запускаться вне репозитория.
- Используйте `pty=true` в вызовах терминала. Codex — это интерактивное приложение для терминала.

Что касается самого Гермеса, `model.provider: openai-codex` использует Кодекс, управляемый Гермесом.
OAuth от `~/.hermes/auth.json` после `hermes auth add openai-codex`. Для
автономный интерфейс командной строки Codex, действующий сеанс CLI OAuth может находиться под
`~/.codex/auth.json`; не рассматривайте отсутствующий `OPENAI_API_KEY` как доказательство
что авторизация Кодекса отсутствует.

## Одноразовые задачи

```
terminal(command="codex exec 'Add dark mode toggle to settings'", workdir="~/project", pty=true)
```

Для работы с нуля (Кодексу требуется репозиторий git):
```
terminal(command="cd $(mktemp -d) && git init && codex exec 'Build a snake game in Python'", pty=true)
```

## Фоновый режим (длинные задачи)

```
# Start in background with PTY
terminal(command="codex exec --sandbox workspace-write 'Refactor the auth module'", workdir="~/project", background=true, pty=true)
# Returns session_id

# Monitor progress
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Send input if Codex asks a question
process(action="submit", session_id="<id>", data="yes")

# Kill if needed
process(action="kill", session_id="<id>")
```

## Ключевые флаги

| Флаг | Эффект |
|------|--------|
| `exec "prompt"` | Одноразовое выполнение, выход по завершении |
| `--sandbox workspace-write` (`-s`) | В изолированной среде, но автоматически одобряет изменения файлов в рабочей области (рекомендуемый режим автоматической сборки) |
| `--dangerously-bypass-approvals-and-sandbox` | Никакой песочницы, никаких утверждений (самый быстрый и самый опасный; `--yolo` по-прежнему работает как скрытый псевдоним) |
| `--sandbox danger-full-access` | Нет песочницы Кодекса; полезно, когда контекст хост-службы нарушает пузырьковую пленку |

> **Устарело:** `--full-auto` по-прежнему работает, но интерактивный интерфейс командной строки предупреждает о необходимости использовать вместо него `--sandbox workspace-write`.

## Предостережение по поводу шлюза Гермеса

При вызове интерфейса командной строки Codex из контекста шлюза/службы Hermes (например,
Сеансы агентов, управляемые Telegram), песочница Codex `workspace-write` может дать сбой даже
когда та же команда работает в интерактивной оболочке пользователя. Типичным симптомом является
ошибки пузырьковой оболочки/пространства имен пользователя, такие как `setting up uid map: Permission denied`
или `loopback: Failed RTM_NEWADDR: Operation not permitted`.

В этом контексте отдайте предпочтение:

```
codex exec --sandbox danger-full-access "<task>"
```

Вместо этого используйте границы процесса в качестве уровня безопасности: явно `workdir`, очистите git
статус перед запуском, узкие подсказки по задачам, `git diff` проверка, целевые тесты и
подтверждение человека/агента перед фиксацией широких изменений.

## PR-обзоры

Клонируйте во временный каталог для безопасного просмотра:

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && gh pr checkout 42 && codex review --base origin/main", pty=true)
```

## Параллельное устранение проблем с помощью рабочих деревьев

```
# Create worktrees
terminal(command="git worktree add -b fix/issue-78 /tmp/issue-78 main", workdir="~/project")
terminal(command="git worktree add -b fix/issue-99 /tmp/issue-99 main", workdir="~/project")

# Launch Codex in each
terminal(command="codex --sandbox workspace-write exec 'Fix issue #78: <description>. Commit when done.'", workdir="/tmp/issue-78", background=true, pty=true)
terminal(command="codex --sandbox workspace-write exec 'Fix issue #99: <description>. Commit when done.'", workdir="/tmp/issue-99", background=true, pty=true)

# Monitor
process(action="list")

# After completion, push and create PRs
terminal(command="cd /tmp/issue-78 && git push -u origin fix/issue-78")
terminal(command="gh pr create --repo user/repo --head fix/issue-78 --title 'fix: ...' --body '...'")

# Cleanup
terminal(command="git worktree remove /tmp/issue-78", workdir="~/project")
```

## Пакетные PR-обзоры

```
# Fetch all PR refs
terminal(command="git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*'", workdir="~/project")

# Review multiple PRs in parallel
terminal(command="codex exec 'Review PR #86. git diff origin/main...origin/pr/86'", workdir="~/project", background=true, pty=true)
terminal(command="codex exec 'Review PR #87. git diff origin/main...origin/pr/87'", workdir="~/project", background=true, pty=true)

# Post results
terminal(command="gh pr comment 86 --body '<review>'", workdir="~/project")
```

## Правила

1. **Всегда используйте `pty=true`** — Codex — это интерактивное терминальное приложение, которое зависает без PTY.
2. **Требуется репозиторий Git** — Codex не запускается вне каталога git. Используйте `mktemp -d && git init` для нуля.
3. **Используйте `exec` для одиночных выстрелов** — `codex exec "prompt"` запускается и завершает работу без ошибок
4. **`--sandbox workspace-write` для сборки** — автоматически одобряет изменения в песочнице (`--full-auto` в этом случае устарел)
5. **Фон для длительных задач** — используйте `background=true` и отслеживайте с помощью инструмента `process`.
6. **Не вмешивайтесь** — контролируйте с помощью `poll`/`log`, будьте терпеливы при выполнении длительных задач.
7. **Параллельная работа подойдет** — запускайте несколько процессов Кодекса одновременно для пакетной работы.