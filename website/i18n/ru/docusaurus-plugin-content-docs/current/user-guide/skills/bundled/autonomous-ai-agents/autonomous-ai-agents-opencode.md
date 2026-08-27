---
title: Opencode — делегирование кодирования в OpenCode CLI (функции, PR-обзор)
sidebar_label: Opencode
description: Делегирование кодирования в OpenCode CLI (функции, PR-обзор)
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Открытый код

Делегирование кодирования в OpenCode CLI (функции, PR-обзор).

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/autonomous-ai-agents/opencode` |
| Версия | `1.2.0` |
| Автор | Агент Гермес |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Coding-Agent`, `OpenCode`, `Autonomous`, `Refactoring`, `Code-Review` |
| Сопутствующие навыки | [`claude-code`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code), [`codex`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-codex), [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Интерфейс командной строки OpenCode

Используйте [OpenCode](https://opencode.ai) в качестве автономного средства кодирования, управляемого инструментами терминала/процесса Hermes. OpenCode — это независимый от поставщика агент кодирования искусственного интеллекта с открытым исходным кодом, оснащенный TUI и CLI.

## Когда использовать

- Пользователь явно просит использовать OpenCode.
- Вы хотите, чтобы внешний агент кодирования реализовал/рефакторил/проверил код.
- Вам нужны длительные сеансы кодирования с проверкой прогресса.
- Вы хотите параллельное выполнение задач в изолированных рабочих каталогах/рабочих деревьях.

## Предварительные условия

- Установлен OpenCode: `npm i -g opencode-ai@latest` или `brew install anomalyco/tap/opencode`.
– Настроена аутентификация: `opencode auth login` или установите переменные среды поставщика (OPENROUTER_API_KEY и т. д.).
– Проверьте: `opencode auth list` должен показывать хотя бы одного поставщика.
- Репозиторий Git для задач кода (рекомендуется)
- `pty=true` для интерактивных сеансов TUI

## Двоичное разрешение (важно)

Среды оболочки могут разрешать разные двоичные файлы OpenCode. Если поведение вашего терминала и Hermes отличается, проверьте:

```
terminal(command="which -a opencode")
terminal(command="opencode --version")
```

При необходимости закрепите явный двоичный путь:

```
terminal(command="$HOME/.opencode/bin/opencode run '...'", workdir="~/project", pty=true)
```

## Одноразовые задачи

Используйте `opencode run` для ограниченных неинтерактивных задач:

```
terminal(command="opencode run 'Add retry logic to API calls and update tests'", workdir="~/project")
```

Прикрепите файлы контекста с помощью `-f`:

```
terminal(command="opencode run 'Review this config for security issues' -f config.yaml -f .env.example", workdir="~/project")
```

Покажите модель мышления с помощью `--thinking`:

```
terminal(command="opencode run 'Debug why tests fail in CI' --thinking", workdir="~/project")
```

Принудительно использовать конкретную модель:

```
terminal(command="opencode run 'Refactor auth module' --model openrouter/anthropic/claude-sonnet-4", workdir="~/project")
```

## Интерактивные сеансы (фон)

Для итеративной работы, требующей нескольких обменов, запустите TUI в фоновом режиме:

```
terminal(command="opencode", workdir="~/project", background=true, pty=true)
# Returns session_id

# Send a prompt
process(action="submit", session_id="<id>", data="Implement OAuth refresh flow and add tests")

# Monitor progress
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Send follow-up input
process(action="submit", session_id="<id>", data="Now add error handling for token expiry")

# Exit cleanly — Ctrl+C
process(action="write", session_id="<id>", data="\x03")
# Or just kill the process
process(action="kill", session_id="<id>")
```

**Важно!** НЕ используйте `/exit` — это недопустимая команда OpenCode и вместо этого откроет диалоговое окно выбора агента. Для выхода используйте Ctrl+C (`\x03`) или `process(action="kill")`.

### Сочетания клавиш TUI

| Ключ | Действие |
|-----|--------|
| `Enter` | Отправить сообщение (при необходимости нажмите дважды) |
| `Tab` | Переключение между агентами (сборка/планирование) |
| `Ctrl+P` | Открыть палитру команд |
| `Ctrl+X L` | Переключить сеанс |
| `Ctrl+X M` | Модель переключателя |
| `Ctrl+X N` | Новая сессия |
| `Ctrl+X E` | Открыть редактор |
| `Ctrl+C` | Выйти из OpenCode |

### Возобновление сеансов

После выхода OpenCode печатает идентификатор сеанса. Возобновить с:

```
terminal(command="opencode -c", workdir="~/project", background=true, pty=true)  # Continue last session
terminal(command="opencode -s ses_abc123", workdir="~/project", background=true, pty=true)  # Specific session
```

## Общие флаги

| Флаг | Использование |
|------|-----|
| `run 'prompt'` | Одноразовая казнь и выход |
| `--continue` / `-c` | Продолжить последнюю сессию OpenCode |
| `--session <id>` / `-s` | Продолжить конкретный сеанс |
| `--agent <name>` | Выберите агент OpenCode (сборка или план) |
| `--model provider/model` | Силовая конкретная модель |
| `--format json` | Машиночитаемый вывод/события |
| `--file <path>` / `-f` | Прикрепить файл(ы) к сообщению |
| `--thinking` | Показать модели блоков мышления |
| `--variant <level>` | Усилие рассуждения (высокое, максимальное, минимальное) |
| `--title <name>` | Назовите сессию |
| `--attach <url>` | Подключиться к работающему серверу открытого кода |

## Процедура

1. Проверьте готовность инструмента:
   - `terminal(command="opencode --version")`
   - `terminal(command="opencode auth list")`
2. Для ограниченных задач используйте `opencode run '...'` (pty не требуется).
3. Для итеративных задач начните `opencode` с `background=true, pty=true`.
4. Отслеживайте длинные задачи с помощью `process(action="poll"|"log")`.
5. Если OpenCode запросит ввод, ответьте через `process(action="submit", ...)`.
6. Выйдите с помощью `process(action="write", data="\x03")` или `process(action="kill")`.
7. Обобщите изменения в файлах, результаты тестирования и последующие действия для пользователя.

## Рабочий процесс PR-анализа

OpenCode имеет встроенную PR-команду:

```
terminal(command="opencode pr 42", workdir="~/project", pty=true)
```

Или просмотрите временный клон для изоляции:

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && opencode run 'Review this PR vs main. Report bugs, security risks, test gaps, and style issues.' -f $(git diff origin/main --name-only | head -20 | tr '\n' ' ')", pty=true)
```

## Шаблон параллельной работы

Используйте отдельные рабочие каталоги/рабочие деревья, чтобы избежать коллизий:

```
terminal(command="opencode run 'Fix issue #101 and commit'", workdir="/tmp/issue-101", background=true, pty=true)
terminal(command="opencode run 'Add parser regression tests and commit'", workdir="/tmp/issue-102", background=true, pty=true)
process(action="list")
```

## Управление сеансами и расходами

Список прошедших сессий:

```
terminal(command="opencode session list")
```

Проверьте использование и стоимость токенов:

```
terminal(command="opencode stats")
terminal(command="opencode stats --days 7 --models anthropic/claude-sonnet-4")
```

## Подводные камни

– Для интерактивных сеансов `opencode` (TUI) требуется `pty=true`. Команде `opencode run` НЕ требуется pty.
- `/exit` НЕ является допустимой командой — она открывает селектор агента. Используйте Ctrl+C, чтобы выйти из TUI.
- Несоответствие PATH может привести к выбору неправильной конфигурации двоичного файла/модели OpenCode.
- Если OpenCode кажется зависшим, проверьте журналы перед завершением:
  - `process(action="log", session_id="<id>")`
- Избегайте совместного использования одного рабочего каталога в параллельных сеансах OpenCode.
- Для отправки в TUI может потребоваться дважды нажать Enter (один раз для завершения текста, один раз для отправки).

## Проверка

Тест на дым:

```
terminal(command="opencode run 'Respond with exactly: OPENCODE_SMOKE_OK'")
```

Критерии успеха:
- Вывод включает `OPENCODE_SMOKE_OK`
- Команда завершается без ошибок поставщика/модели.
- Для задач кода: ожидаемые файлы изменены и тесты пройдены.

## Правила

1. Для одноразовой автоматизации отдайте предпочтение `opencode run` — это проще и не требует pty.
2. Используйте интерактивный фоновый режим только тогда, когда необходима итерация.
3. Всегда ограничивайте сеансы OpenCode одним репозиторием/рабочим каталогом.
4. Для длительных задач предоставляйте обновления о ходе выполнения из журналов `process`.
5. Сообщайте о конкретных результатах (изменения файлов, тесты, оставшиеся риски).
6. Выйдите из интерактивного сеанса с помощью Ctrl+C или уничтожьте, но не `/exit`.