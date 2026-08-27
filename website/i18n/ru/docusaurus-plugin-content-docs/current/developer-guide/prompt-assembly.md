---
sidebar_position: 5
title: Быстрая сборка
description: Как Hermes создает системное приглашение, сохраняет стабильность кэша
  и внедряет эфемерные слои
---

# Быстрая сборка

Гермес намеренно разделяет:

- **состояние кэшированного системного запроса**
- **эфемерные дополнения во время вызова API**

Это один из наиболее важных дизайнерских решений в проекте, поскольку он влияет на:

- использование токена
- быстрая эффективность кэширования
- непрерывность сеанса
- корректность памяти

Первичные файлы:

- `run_agent.py`
- `agent/prompt_builder.py`
- `tools/memory_tool.py`

## Слои кэшированных системных подсказок

Кэшированное системное приглашение состоит из трех упорядоченных уровней (см. `agent/system_prompt.py`):

1. **стабильный** — идентификатор (`SOUL.md` или резервный вариант), рекомендации по инструменту/модели, подсказка о навыках, подсказки по среде, подсказки по платформе.
2. **context** — предоставленный вызывающей стороной `system_message` плюс файлы контекста проекта (`.hermes.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules`)
3. ** Летучий** — снимок встроенной памяти (`MEMORY.md`), снимок профиля пользователя (`USER.md`), блок внешнего поставщика памяти, метка времени/сессия/модель/строка поставщика

Последнее системное приглашение затем объединяется как: `stable` → `context` → `volatile`.

Этот порядок имеет значение для обсуждений приоритета:
- навыки относятся к **стабильному** уровню
- снимки памяти/профиля являются частью **летучего** уровня.
- оба все еще находятся в кэшированной системной подсказке (они не вводятся как специальные наложения в середине хода)

Если установлен `skip_context_files` (например, делегирование субагента), SOUL.md не загружается и вместо него используется жестко закодированный `DEFAULT_AGENT_IDENTITY`.

### Конкретный пример: собранная системная подсказка

Вот упрощенное представление того, как выглядит окончательное системное приглашение, когда присутствуют все слои (в комментариях указан источник каждого раздела):

```
# Layer 1: Agent Identity (from ~/.hermes/SOUL.md)
You are Hermes, an AI assistant created by Nous Research.
You are an expert software engineer and researcher.
You value correctness, clarity, and efficiency.
...

# Layer 2: Tool-aware behavior guidance
You have persistent memory across sessions. Save durable facts using
the memory tool: user preferences, environment details, tool quirks,
and stable conventions. Memory is injected into every turn, so keep
it compact and focused on facts that will still matter later.
...
When the user references something from a past conversation or you
suspect relevant cross-session context exists, use session_search
to recall it before asking them to repeat themselves.

# Tool-use enforcement (for GPT/Codex models only)
You MUST use your tools to take action — do not describe what you
would do or plan to do without actually doing it.
...

# Layer 3: Honcho static block (when active)
[Honcho personality/context data]

# Layer 4: Optional system message (from config or API)
[User-configured system message override]

# Layer 5: Frozen MEMORY snapshot
## Persistent Memory
- User prefers Python 3.12, uses pyproject.toml
- Default editor is nvim
- Working on project "atlas" in ~/code/atlas
- Timezone: US/Pacific

# Layer 6: Frozen USER profile snapshot
## User Profile
- Name: Alice
- GitHub: alice-dev

# Layer 7: Skills index
## Skills (mandatory)
Before replying, scan the skills below. If one clearly matches
your task, load it with skill_view(name) and follow its instructions.
...
<available_skills>
  software-development:
    - code-review: Structured code review workflow
    - test-driven-development: TDD methodology
  research:
    - arxiv: Search and summarize arXiv papers
</available_skills>

# Layer 8: Context files (from project directory)
# Project Context
The following project context files have been loaded and should be followed:

## AGENTS.md
This is the atlas project. Use pytest for testing. The main
entry point is src/atlas/main.py. Always run `make lint` before
committing.

# Layer 9: Timestamp + session
Current time: 2026-03-30T14:30:00-07:00
Session: abc123

# Layer 10: Platform hint
You are a CLI AI Agent. Try not to use markdown but simple text
renderable inside a terminal.
```

## Настройка подсказок платформы

Подсказка платформы (уровень 10 выше) — это руководство Hermes для каждой поверхности.
инжекты для Telegram, WhatsApp, Slack, CLI и других платформ — для
пример «вы используете терминал, избегайте Markdown». Встроенные настройки по умолчанию
живу в `PLATFORM_HINTS` (`agent/system_prompt.py`); предоставляемый плагином
платформы предоставляют свои данные через реестр платформ.

Администратор может добавить или заменить подсказку для одной платформы из
`config.yaml` с помощью клавиши `platform_hints` верхнего уровня, не касаясь
любая другая платформа:

```yaml
platform_hints:
  whatsapp:
    append: >
      When tabular output would be useful, invoke the table_formatting
      skill instead of emitting a Markdown table.
  slack:
    replace: "You are on Slack. Keep responses tight and avoid wide tables."
  telegram: "Prefer short messages; split long answers."   # shorthand = append
```

- `append` — сохраните встроенную подсказку и добавьте после нее дополнительный текст.
- `replace` — полностью заменить встроенную подсказку.
- Голая строка — сокращение от `append`.
- `replace` побеждает `append`, когда присутствуют оба.
- Неверно сформированная запись игнорируется и возвращается к
  неизмененное значение по умолчанию, поэтому неправильное значение конфигурации никогда не может нарушить подсказку
  сборка или утечка через платформы.

Переопределение разрешается при построении системного приглашения (начало сеанса,
и снова при сжатии, так как это перестраивает подсказку). Он производит
подсказка о стабильности байтов для фиксированной конфигурации, поэтому она находится на уровне **stable**
наряду со встроенной подсказкой и не нарушает кэширование подсказок — это
это не живая мутация замороженного приглашения в середине сеанса.

## Как SOUL.md отображается в командной строке

`SOUL.md` находится по адресу `~/.hermes/SOUL.md` и служит идентификатором агента — самый первый раздел системного приглашения. Логика загрузки в `prompt_builder.py` работает следующим образом:

```python
# From agent/prompt_builder.py (simplified)
def load_soul_md() -> Optional[str]:
    soul_path = get_hermes_home() / "SOUL.md"
    if not soul_path.exists():
        return None
    content = soul_path.read_text(encoding="utf-8").strip()
    content = _scan_context_content(content, "SOUL.md")  # Security scan
    content = _truncate_content(content, "SOUL.md")       # Cap scales with model context window (20k floor); config override wins
    return content
```

Когда `load_soul_md()` возвращает содержимое, оно заменяет жестко запрограммированный `DEFAULT_AGENT_IDENTITY`. Затем с помощью `skip_soul=True` вызывается функция `build_context_files_prompt()`, чтобы предотвратить появление SOUL.md дважды (один раз как идентификатор, один раз как контекстный файл).

Если `SOUL.md` не существует, система возвращается к следующему варианту:

```
You are Hermes Agent, an intelligent AI assistant created by Nous Research.
You are helpful, knowledgeable, and direct. You assist users with a wide
range of tasks including answering questions, writing and editing code,
analyzing information, creative work, and executing actions via your tools.
You communicate clearly, admit uncertainty when appropriate, and prioritize
being genuinely useful over being verbose unless otherwise directed below.
Be targeted and efficient in your exploration and investigations.
```

## Как внедряются файлы контекста

`build_context_files_prompt()` использует **систему приоритетов** — загружается только один тип контекста проекта (выигрывает первое совпадение):

```python
# From agent/prompt_builder.py (simplified)
def build_context_files_prompt(cwd=None, skip_soul=False):
    cwd_path = Path(cwd).resolve()

    # Priority: first match wins — only ONE project context loaded
    project_context = (
        _load_hermes_md(cwd_path)       # 1. .hermes.md / HERMES.md (walks to git root)
        or _load_agents_md(cwd_path)    # 2. AGENTS.md (cwd only)
        or _load_claude_md(cwd_path)    # 3. CLAUDE.md (cwd only)
        or _load_cursorrules(cwd_path)  # 4. .cursorrules / .cursor/rules/*.mdc
    )

    sections = []
    if project_context:
        sections.append(project_context)

    # SOUL.md from HERMES_HOME (independent of project context)
    if not skip_soul:
        soul_content = load_soul_md()
        if soul_content:
            sections.append(soul_content)

    if not sections:
        return ""

    return (
        "# Project Context\n\n"
        "The following project context files have been loaded "
        "and should be followed:\n\n"
        + "\n".join(sections)
    )
```

### Подробности обнаружения файла контекста

| Приоритет | Файлы | Область поиска | Заметки |
|----------|-------|-------------|-------|
| 1 | `.hermes.md`, `HERMES.md` | CWD до git root | Конфигурация проекта Hermes |
| 2 | `AGENTS.md` | только CWD | Файл инструкций общего агента |
| 3 | `CLAUDE.md` | только CWD | Совместимость с кодом Клода |
| 4 | `.cursorrules`, `.cursor/rules/*.mdc` | только CWD | Совместимость курсоров |

Все контекстные файлы:
- **Проверка безопасности** — проверяется наличие шаблонов быстрого внедрения (невидимый юникод, «игнорировать предыдущие инструкции», попытки кражи учетных данных).
- **Усечено** — ограничено `context_file_max_chars` символами с разделением головы и хвоста 70/20 с помощью маркера усечения. Ограничение масштабируется в соответствии с контекстным окном модели (пол 20 000 символов, потолок 500 КБ); явный `context_file_max_chars` в `config.yaml` всегда побеждает.
- **Фронтовая часть YAML удалена** — `.hermes.md` фронтовая надпись удалена (зарезервирована для будущих переопределений конфигурации)

## Слои только для времени вызова API

Они намеренно *не* сохраняются как часть кэшированного системного приглашения:

- `ephemeral_system_prompt`
- предварительное заполнение сообщений
- наложения контекста сеанса на основе шлюза
- Honcho последующего хода/внешний отзыв, вставленный в сообщение пользователя текущего хода.

Контекст плагина `pre_llm_call` также попадает в этот путь времени вызова API: он добавляется к **пользовательскому сообщению** текущего хода, а не записывается в кэшированное системное приглашение. Когда несколько плагинов возвращают контекст, Hermes объединяет эти блоки контекста (см. [Hooks → `pre_llm_call`](../user-guide/features/hooks.md#pre_llm_call)).

Такое разделение сохраняет стабильный префикс стабильным для кэширования.

## Снимки памяти

Данные локальной памяти и профиля пользователя сохраняются на **изменчивом уровне** системной подсказки. В середине сеанса запись обновляет состояние диска, но не изменяет уже созданное кэшированное системное приглашение до тех пор, пока не будет запущен путь перестроения (новый сеанс или явный поток аннулирования/перестроения, такой как перестроение, запускаемое сжатием).

## Файлы контекста

`agent/prompt_builder.py` сканирует и очищает файлы контекста проекта, используя **систему приоритетов** — загружается только один тип (побеждает первое совпадение):

1. `.hermes.md` / `HERMES.md` (переходит к корневому каталогу git)
2. `AGENTS.md` (CWD при запуске; подкаталоги обнаруживаются постепенно во время сеанса через `agent/subdirectory_hints.py`)
3. `CLAUDE.md` (только CWD)
4. `.cursorrules` / `.cursor/rules/*.mdc` (только CWD)

`SOUL.md` загружается отдельно через `load_soul_md()` для идентификационного слота. При успешной загрузке `build_context_files_prompt(skip_soul=True)` предотвращает его двойное появление.

Длинные файлы усекаются перед внедрением.

## Индекс навыков

Система навыков добавляет компактный индекс навыков в подсказку, когда доступны инструменты для навыков.

## Поддерживаемые поверхности настройки подсказок

Большинству пользователей следует рассматривать `agent/prompt_builder.py` как код реализации, а не как поверхность конфигурации. Поддерживаемый путь настройки — это изменение входных данных, которые Hermes уже загружает, вместо редактирования шаблонов Python на месте.

### Сначала используйте эти поверхности

- `~/.hermes/SOUL.md` — замените встроенный блок идентификации по умолчанию на свою собственную личность агента и постоянное поведение.
- `~/.hermes/MEMORY.md` и `~/.hermes/USER.md` – предоставляют надежные данные о перекрестных сеансах и данные профиля пользователя, которые необходимо создавать в новых сеансах.
- Файлы контекста проекта, такие как `.hermes.md`, `HERMES.md`, `AGENTS.md`, `CLAUDE.md` или `.cursorrules`, внедряют рабочие правила, специфичные для репозитория.
- Навыки — упаковывайте многократно используемые рабочие процессы и ссылки без редактирования основного кода подсказки.
- Дополнительные переопределения конфигурации системных подсказок / API — добавьте текст инструкций для конкретного развертывания, не разветвляя Hermes.
– Эфемерные наложения, такие как `HERMES_EPHEMERAL_SYSTEM_PROMPT` или предварительные сообщения — добавляют указания на уровне хода, которые не должны становиться частью кэшированного префикса подсказки.

### Когда лучше редактировать код

Редактируйте `agent/prompt_builder.py` только в том случае, если вы намеренно поддерживаете вилку или вносите изменения в поведение исходной ветки. В этом файле собраны данные о подсказках, границах кэша и порядке внедрения для каждого сеанса. Прямые изменения — это глобальные изменения продукта, а не настройка подсказок для каждого пользователя.

Другими словами:

– если вам нужен другой идентификатор помощника, отредактируйте `SOUL.md`.
- если вам нужны другие правила репо, отредактируйте файлы контекста проекта.
- если вы хотите многократно использовать рабочие процедуры, добавьте или измените навыки
- если вы хотите изменить то, как Hermes собирает подсказки для всех, измените Python и рассматривайте это как вклад в код

## Почему сборка команд разделена таким образом

Архитектура намеренно оптимизирована для:

- сохранять кэширование подсказок на стороне провайдера
- избегайте мутаций истории без необходимости
- сохранять семантику памяти понятной
- позволить шлюзу/ACP/CLI добавлять контекст, не отравляя постоянное состояние приглашения

## Связанные документы

- [Сжатие контекста и кэширование подсказок](./context-compression-and-caching.md)
- [Хранилище сеансов](./session-storage.md)
- [Внутреннее устройство шлюза](./gateway-internals.md)