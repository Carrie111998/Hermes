---
title: Агент Hermes — использование, настройка, тематика, расширение и оркестрация
  агента Hermes.
sidebar_label: Hermes Agent
description: Использование, настройка, тематика, расширение и оркестрация агента Hermes
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Агент Гермеса

Используйте, настраивайте, тематизируйте, расширяйте и координируйте агент Hermes.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/autonomous-ai-agents/hermes-agent` |
| Версия | `3.1.0` |
| Автор | Гермес Агент + Текниум |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `hermes`, `setup`, `configuration`, `multi-agent`, `spawning`, `cli`, `gateway`, `themes`, `skins`, `desktop-plugins`, `tui-widgets`, `petdex`, `development` |
| Сопутствующие навыки | [`claude-code`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code), [`codex`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-codex), [`opencode`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-opencode) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Агент Гермеса

Hermes Agent — это среда агентов искусственного интеллекта с открытым исходным кодом от Nous Research, которая работает на вашем терминале, собственном настольном приложении, платформах обмена сообщениями и IDE. Он находится в той же категории, что и Claude Code (Anthropic), Codex (OpenAI) и OpenClaw — автономные агенты кодирования и выполнения задач, которые используют вызов инструментов для взаимодействия с вашей системой. Hermes работает с любым поставщиком LLM (OpenRouter, Anthropic, OpenAI, Google, DeepSeek, xAI, локальные модели и более 20 других) и работает на Linux, macOS, Windows и WSL.

Чем отличается Гермес:

- **Самосовершенствование через навыки**. Компания Hermes учится на собственном опыте, сохраняя многократно используемые процедуры в качестве навыков, которые можно загрузить в будущие сеансы.
- **Постоянная память на протяжении всего сеанса** — запоминает, кто вы, ваши предпочтения, детали окружающей среды и извлеченные уроки. Подключаемые модули памяти.
- **Мультиплатформенный шлюз** — один и тот же агент работает на Telegram, Discord, Slack, WhatsApp, iMessage, Signal, Matrix, Teams, Email и еще десятке платформ с полным доступом к инструментам, а не только к чату.
- **Множество поверхностей** — одно и то же ядро ​​агента управляет CLI, Ink TUI, собственным настольным приложением Electron, веб-панелью и сервером ACP для IDE (VS Code/Zed/JetBrains).
- **Независимость от поставщика** — замена моделей и поставщиков в ходе рабочего процесса; пулы учетных данных автоматически чередуются между несколькими ключами API.
- **Профили** — запуск нескольких независимых экземпляров Hermes с изолированными конфигурациями, сеансами, навыками и памятью.
- **Расширяемость и возможность создания тем** — плагины, серверы MCP, специальные инструменты, триггеры веб-перехватчиков, планирование cron, темы оформления для каждой поверхности, плагины пользовательского интерфейса рабочего стола, виджеты TUI и талисманы домашних животных.

**Этот навык является центром.** Тело охватывает идентификацию, быстрый старт, создание/организацию и жесткие инварианты. Все остальное находится в справочных файлах — **перед ответом загрузите соответствующую ссылку (ниже)**; не отвечайте на подробные вопросы, исходящие только от тела.

**Документация:** https://hermes-agent.nousresearch.com/docs/

## Область применения и проверка

Этот навык представляет собой краткое руководство по эксплуатации, а не полный источник достоверной информации о каждой функции Hermes. Если функция, команда или настройка Hermes не упоминается здесь или в ссылке, не рассматривайте это отсутствие как свидетельство того, что они не существуют. Прежде чем дать отрицательный ответ, проверьте действующий репозиторий и официальную документацию.

Хорошие цели проверки:

- Команды CLI: `hermes --help`, `hermes <command> --help` и `hermes_cli/main.py`.
- Пользовательская документация: https://hermes-agent.nousresearch.com/docs/
- Дерево исходного кода: https://github.com/NousResearch/hermes-agent.

## Быстрый старт

```bash
# Install (shell installer — sets up uv, Python, the venv, and the launcher)
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# Interactive chat (default surface; set display.interface: tui to launch the Ink TUI instead)
hermes

# Single query
hermes chat -q "What is the capital of France?"

# Setup wizard  /  pick model+provider  /  health check
hermes setup
hermes model
hermes doctor

# Other surfaces
hermes desktop                 # launch the native desktop app (alias: hermes gui)
hermes dashboard               # web admin panel + embedded chat
hermes proxy                   # OpenAI-compatible local proxy backed by your OAuth provider
```

## Ключевые пути

```
~/.hermes/config.yaml       Main configuration (settings — never secrets)
~/.hermes/.env              API keys and secrets ONLY (under $HERMES_HOME if set)
$HERMES_HOME/skills/        Installed skills
~/.hermes/skins/            Custom themes (see references/themes.md)
~/.hermes/desktop-plugins/  Desktop app UI plugins (see references/desktop-plugins.md)
~/.hermes/tui-widgets/      TUI widget apps (see references/tui-widgets.md)
~/.hermes/pets/             Installed pet mascots (see references/petdex.md)
~/.hermes/state.db          Canonical session store (SQLite + FTS5)
~/.hermes/sessions/         Gateway routing index, request dumps, *.jsonl transcripts
~/.hermes/logs/             Gateway and error logs
~/.hermes/auth.json         OAuth tokens and credential pools
~/.hermes/hermes-agent/     Source code (if git-installed)
```

В профилях используется `~/.hermes/profiles/<name>/` с одинаковым макетом. Когда профиль активен, определите настоящий дом из `$HERMES_HOME` — никогда не закодируйте `~/.hermes` жестко.

## Таблица маршрутизации — загрузить ссылку на задачу

| Пользователь хочет... | Загрузить |
|---|---|
| Команды CLI, подкоманды, флаги, «как запустить X» | `references/cli-reference.md` |
| Команды слэша в сеансе | `references/slash-commands.md` |
| Настройка провайдера, ключи API, OAuth | `references/providers-and-models.md` |
| разделы config.yaml, наборы инструментов, voice/STT/TTS | `references/configuration.md` |
| Правила проекта AGENTS.md / .hermes.md / CLAUDE.md | `references/project-context-files.md` |
| Секретное редактирование, персональные данные, режимы утверждения, «сброс разрешений» | `references/security-privacy.md` |
| Делегирование, cron, куратор, канбан | `references/background-systems.md` |
| Серверы MCP (добавить, каталог, `hermes mcp`) | `references/native-mcp.md` |
| Маршруты Webhook и запуски, управляемые событиями | `references/webhooks.md` |
| Пользовательская тема/скин («тема синтезаторной волны», «изменить золото ●») | `references/themes.md` + `templates/skin.yaml` |
| Элемент пользовательского интерфейса настольного приложения (панель, виджет, команда ⌘K, страница) | `references/desktop-plugins.md` + `templates/plugin.js` |
| Живая панель TUI или модальный виджет (тикер, часы, панель мониторинга) | `references/tui-widgets.md` + `templates/clock.mjs` |
| Талисманы-питомцы — устанавливаем, выбираем, масштабируем, диагностируем | `references/petdex.md` |
| Проблемы, специфичные для Windows (сочетания клавиш, WinError 10106, BOM) | `references/windows-quirks.md` |
| Отладка: голос, инструменты отсутствуют, шлюз, модели вспомогательных устройств | `references/troubleshooting.md` |
| Содействие коду: добавление инструментов, косых команд, тестов | `references/contributor-guide.md` |
| Delegate_task сообщает «ограничено N» | `references/delegate-task-concurrency-diagnosis.md` |
| «Может ли приложение X использовать мою подписку на Nous Portal/OAuth?» | `references/portal-auth-for-third-party-apps.md` |

Два правила оформления тем, которые действуют даже без загрузки ссылки: **вы сами применяете скины** (`hermes config set display.skin <name>` — каждая поверхность перекрашивается в течение ~секунды; не говорите пользователю запускать `/skin`) и **чтобы настроить один цвет, отредактируйте АКТИВНЫЙ скин** (`hermes skin set <key> <hex>`) — никогда не разветвляйте `default`, что приведет к удалению палитры и сбросу фона.

## Создание дополнительных экземпляров Hermes

Запускайте дополнительные процессы Hermes как полностью независимые подпроцессы — отдельные сеансы, инструменты и среды.

### Когда это использовать, а не Delegate_task

| | `delegate_task` | Создание процесса `hermes` |
|-|-----------------|--------------------------|
| Изоляция | Отдельный разговор, общий процесс | Полностью независимый процесс |
| Продолжительность | Минуты (ограничены родительским циклом) | Часы/дни |
| Доступ к инструментам | Подмножество родительских инструментов | Полный доступ к инструментам |
| Интерактивный | Нет | Да (режим PTY) |
| Вариант использования | Быстрые параллельные подзадачи | Длительные автономные миссии |

### Режим одиночного выстрела

```
terminal(command="hermes chat -q 'Research GRPO papers and write summary to ~/research/grpo.md'", timeout=300)

# Background for long tasks:
terminal(command="hermes chat -q 'Set up CI/CD for ~/myapp'", background=true)
```

### Интерактивный режим PTY (через tmux)

Hermes использует Prompt_toolkit, для которого требуется настоящий терминал. Используйте tmux для интерактивного появления:

```
# Start
terminal(command="tmux new-session -d -s agent1 -x 120 -y 40 'hermes'", timeout=10)

# Wait for startup, then send a message
terminal(command="sleep 8 && tmux send-keys -t agent1 'Build a FastAPI auth service' Enter", timeout=15)

# Read output
terminal(command="sleep 20 && tmux capture-pane -t agent1 -p", timeout=5)

# Send follow-up
terminal(command="tmux send-keys -t agent1 'Add rate limiting middleware' Enter", timeout=5)

# Exit
terminal(command="tmux send-keys -t agent1 '/exit' Enter && sleep 2 && tmux kill-session -t agent1", timeout=10)
```

### Многоагентная координация

```
# Agent A: backend
terminal(command="tmux new-session -d -s backend -x 120 -y 40 'hermes -w'", timeout=10)
terminal(command="sleep 8 && tmux send-keys -t backend 'Build REST API for user management' Enter", timeout=15)

# Agent B: frontend
terminal(command="tmux new-session -d -s frontend -x 120 -y 40 'hermes -w'", timeout=10)
terminal(command="sleep 8 && tmux send-keys -t frontend 'Build React dashboard for user management' Enter", timeout=15)

# Check progress, relay context between them
terminal(command="tmux capture-pane -t backend -p | tail -30", timeout=5)
terminal(command="tmux send-keys -t frontend 'Here is the API schema from the backend agent: ...' Enter", timeout=5)
```

### Возобновление сеанса

```
# Resume most recent session
terminal(command="tmux new-session -d -s resumed 'hermes --continue'", timeout=10)

# Resume specific session
terminal(command="tmux new-session -d -s resumed 'hermes --resume 20260225_143052_a1b2c3'", timeout=10)
```

### Советы

- **Предпочитайте `delegate_task` для быстрых подзадач** — меньше накладных расходов, чем при создании полного процесса.
- **Используйте `-w` (режим рабочего дерева)** при создании агентов, редактирующих код — предотвращает конфликты git.
- **Установите таймауты** для одноразового режима — сложные задачи могут занять 5–10 минут.
- **Используйте `hermes chat -q` для метода «выстрелил и забыл»** — PTY не требуется.
- **Используйте tmux для интерактивных сеансов** — в режиме raw PTY есть проблемы `\r` и `\n` с Prompt_toolkit.
- **Для запланированных задач** используйте инструмент `cronjob` вместо создания — обрабатывает доставку и повторяет попытку.
- **Отчеты «delegate_task ограничены N»** — см. `references/delegate-task-concurrency-diagnosis.md`. Три настоящих дорожки для шапок в Гермесе; если ни один из них не сработал, модель является самоограничивающейся и рационализирует ее как «ограничения времени выполнения».
- **"Может ли $external_app использовать мою подписку на Nous Portal/OAuth?"** — см. `references/portal-auth-for-third-party-apps.md`. Проведите пользователя через три уровня (плагин-приложение, то, что на самом деле предоставляет портал, опция локального брокера-прокси).

## Поверхности (быстрая ориентация)

- **Настольное приложение** (`hermes desktop` / `hermes gui`) — собственное приложение Electron для macOS/Linux/Windows: потоковый чат, список сеансов, палитра Cmd+K, перетаскивание файлов, встроенные уведомления, вход в удаленный шлюз для каждого профиля. Расширьте его с помощью плагинов пользовательского интерфейса — `references/desktop-plugins.md`.
- **Веб-панель** (`hermes dashboard`) — полноценная панель администратора: каналы обмена сообщениями, каталог MCP, веб-перехватчики, память, конструктор профилей, а также встроенный чат `hermes --tui`. Защищено шлюзом OAuth/токена.
- **Ink TUI** (`hermes --tui` или `display.interface: tui`) — пользовательский интерфейс терминала с прикрепленными виджетами — `references/tui-widgets.md`.
- **Прокси-сервер, совместимый с OpenAI** (`hermes proxy`) — локальный API OpenAI, поддерживаемый любым провайдером OAuth, в который вы вошли. Point Codex CLI, Aider, Cline или любой другой скрипт — без ключа API.

## Жесткие инварианты (никогда не нарушаются, независимо от того, что вы загрузили)

- **Никогда не нарушайте кэширование подсказок** — не меняйте прошлый контекст, наборы инструментов или системные подсказки в середине разговора. Единственным исключением является сжатие контекста.
- **Чередование ролей сообщений** — никогда не два сообщения помощника или два пользователя подряд; только `tool` результатов могут повторяться.
- **Секреты в `.env`, настройки в `config.yaml`** — никогда не советуйте пользователю помещать настройки, не связанные с учетными данными, в `.env`.
- **Профильно-безопасные пути** — `get_hermes_home()` в коде, `$HERMES_HOME` при разрешении путей в сеансе.
- **Никогда не редактируйте `config.yaml` вручную** — используйте `hermes config set KEY VAL`; случайный отступ может повредить файл и сломать работающий шлюз.